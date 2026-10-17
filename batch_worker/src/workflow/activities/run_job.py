"""The only activity: run one pipeline step as a Kubernetes Job and wait for it.

Sync code on a thread pool: the Kubernetes client and boto3 are blocking.

Idempotent: the Job name comes from the step and run_id, so a retried
activity (the worker died, the pod was rescheduled) finds the Job it created
before and keeps waiting for it instead of starting the step twice.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import boto3
from botocore.exceptions import ClientError
from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from workflow.contracts import JobRequest

SERVICE_ACCOUNT_NAMESPACE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


@dataclass(frozen=True)
class JobConfig:
    namespace: str
    image: str
    # Connection settings of the Job (ClickHouse, MLflow, S3), as envFrom.
    env_config_map: str = "pipeline-job"
    env_secret: str = "pipeline-job"
    result_bucket: str = "pipeline"
    ttl_seconds: int = 3600
    labels: dict = field(default_factory=lambda: {"app.kubernetes.io/name": "pipeline-job"})

    @classmethod
    def from_env(cls) -> "JobConfig":
        namespace = os.environ.get("JOB_NAMESPACE")
        if not namespace and os.path.exists(SERVICE_ACCOUNT_NAMESPACE):
            with open(SERVICE_ACCOUNT_NAMESPACE) as f:
                namespace = f.read().strip()
        return cls(
            namespace=namespace or "default",
            image=os.environ["JOB_IMAGE"],
            env_config_map=os.environ.get("JOB_ENV_CONFIG_MAP", "pipeline-job"),
            env_secret=os.environ.get("JOB_ENV_SECRET", "pipeline-job"),
            result_bucket=os.environ.get("S3_BUCKET", "pipeline"),
        )


def job_name(step: str, run_id: str) -> str:
    """DNS-1123 label: lowercase alphanumerics and '-', at most 63 characters."""
    name = re.sub(r"[^a-z0-9-]+", "-", f"{step}-{run_id}".lower())
    return name[:63].strip("-")


def result_key(step: str, run_id: str) -> str:
    return f"runs/{run_id}/{step}/result.json"


class JobRunner:
    def __init__(self, batch: k8s.BatchV1Api, core: k8s.CoreV1Api, s3, config: JobConfig, poll_seconds: float = 2.0):
        self.batch, self.core, self.s3, self.config = batch, core, s3, config
        self.poll_seconds = poll_seconds

    @classmethod
    def from_env(cls) -> "JobRunner":
        # In the pod: the ServiceAccount token. On a laptop: ~/.kube/config.
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        return cls(k8s.BatchV1Api(), k8s.CoreV1Api(), boto3.client("s3"), JobConfig.from_env())

    @activity.defn(name="run_job")
    def run_job(self, req: JobRequest) -> dict:
        name = job_name(req.step, req.run_id)
        # When the workflow scheduled the step, not when this attempt began:
        # the logical start of the run in the run registry, same on retries.
        self._create(name, req, activity.info().scheduled_time)
        try:
            self._wait(name)
        except CancelledError:
            # The workflow was cancelled: do not leave the step running.
            self._delete(name)
            raise
        return self._result(req)

    def manifest(self, name: str, req: JobRequest, started_at: datetime) -> k8s.V1Job:
        cfg = self.config
        labels = {**cfg.labels, "pipeline/step": req.step, "pipeline/run-id": job_name("", req.run_id)}
        requests = {"cpu": req.cpu, "memory": req.memory}
        limits = {"cpu": req.cpu, "memory": req.memory}
        node_selector = {"node-pool": req.pool}
        tolerations = None
        if req.pool == "gpu":
            # Same as on a cloud GPU node group: the taint keeps everything
            # else off, the resource reserves the (here simulated) GPU.
            limits["nvidia.com/gpu"] = "1"
            tolerations = [k8s.V1Toleration(key="nvidia.com/gpu", operator="Exists", effect="NoSchedule")]

        container = k8s.V1Container(
            name="step",
            image=cfg.image,
            image_pull_policy="IfNotPresent",
            args=[
                "--result-key",
                result_key(req.step, req.run_id),
                "--started-at",
                started_at.isoformat(),
                req.step,
                *req.args,
            ],
            env=[k8s.V1EnvVar(name="TORCH_THREADS", value=req.cpu)],
            env_from=[
                k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name=cfg.env_config_map)),
                k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name=cfg.env_secret)),
            ],
            resources=k8s.V1ResourceRequirements(requests=requests, limits=limits),
        )
        return k8s.V1Job(
            metadata=k8s.V1ObjectMeta(name=name, labels=labels),
            spec=k8s.V1JobSpec(
                # One retry of the pod; the activity has its own retries, and
                # the two must not multiply into a dozen trainings.
                backoff_limit=1,
                active_deadline_seconds=req.deadline_seconds,
                ttl_seconds_after_finished=cfg.ttl_seconds,
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels=labels),
                    spec=k8s.V1PodSpec(
                        restart_policy="Never",
                        node_selector=node_selector,
                        tolerations=tolerations,
                        containers=[container],
                    ),
                ),
            ),
        )

    def _create(self, name: str, req: JobRequest, started_at: datetime) -> None:
        try:
            self.batch.create_namespaced_job(self.config.namespace, self.manifest(name, req, started_at))
            activity.logger.info("created job %s", name)
        except ApiException as e:
            if e.status != 409:
                raise
            activity.logger.info("job %s already exists, waiting for it", name)

    def _wait(self, name: str) -> None:
        while True:
            status = self.batch.read_namespaced_job_status(name, self.config.namespace).status
            if status.succeeded:
                return
            if status.failed and _is_failed(status):
                raise ApplicationError(
                    f"job {name} failed: {_reason(status)}\n{self._logs(name)}",
                    type="JobFailed",
                    # The Job already retried its pod; retrying the activity
                    # would find the same failed Job.
                    non_retryable=True,
                )
            activity.heartbeat({"job": name, "active": status.active or 0, "failed": status.failed or 0})
            if activity.is_cancelled():
                raise CancelledError()
            time.sleep(self.poll_seconds)

    def _result(self, req: JobRequest) -> dict:
        try:
            body = self.s3.get_object(Bucket=self.config.result_bucket, Key=result_key(req.step, req.run_id))["Body"]
        except ClientError as e:
            raise ApplicationError(f"job succeeded but wrote no result: {e}", type="NoResult", non_retryable=True)
        return json.loads(body.read())

    def _delete(self, name: str) -> None:
        try:
            self.batch.delete_namespaced_job(name, self.config.namespace, propagation_policy="Background")
            activity.logger.info("deleted job %s after cancellation", name)
        except ApiException as e:
            if e.status != 404:
                raise

    def _logs(self, name: str, lines: int = 20) -> str:
        """Tail of the last pod of the Job, so the reason is in the workflow history."""
        try:
            pods = self.core.list_namespaced_pod(self.config.namespace, label_selector=f"job-name={name}").items
            if not pods:
                return "(no pods)"
            last = max(pods, key=lambda p: p.metadata.creation_timestamp)
            return self.core.read_namespaced_pod_log(last.metadata.name, self.config.namespace, tail_lines=lines)
        except ApiException as e:
            return f"(logs unavailable: {e.status})"


def _is_failed(status) -> bool:
    return any(c.type == "Failed" and c.status == "True" for c in status.conditions or [])


def _reason(status) -> str:
    for c in status.conditions or []:
        if c.type == "Failed" and c.status == "True":
            return f"{c.reason}: {c.message}"
    return "unknown"
