import io
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from botocore.exceptions import ClientError
from kubernetes.client.exceptions import ApiException
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from workflow.activities.run_job import JobConfig, JobRunner, job_name, result_key
from workflow.contracts import JobRequest


def status(succeeded=0, failed=0, active=0, failed_reason=None):
    conditions = [NS(type="Failed", status="True", reason=failed_reason, message="too many retries")] if failed_reason else []
    return NS(status=NS(succeeded=succeeded, failed=failed, active=active, conditions=conditions))


class FakeBatch:
    def __init__(self, statuses, exists=False):
        self.statuses, self.exists, self.created, self.deleted = list(statuses), exists, [], []

    def create_namespaced_job(self, namespace, job):
        if self.exists:
            raise ApiException(status=409)
        self.created.append((namespace, job))

    def read_namespaced_job_status(self, name, namespace):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def delete_namespaced_job(self, name, namespace, propagation_policy):
        self.deleted.append(name)


class FakeCore:
    def list_namespaced_pod(self, namespace, label_selector):
        return NS(items=[NS(metadata=NS(name="train-r1-abc", creation_timestamp=datetime.now(timezone.utc)))])

    def read_namespaced_pod_log(self, name, namespace, tail_lines):
        return "Traceback ...\nValueError: no train examples in ClickHouse"


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[(Bucket, Key)]).encode())}


CONFIG = JobConfig(namespace="batch-worker", image="ghcr.io/rune28/mle-models:dev-abc")
TRAIN = JobRequest(step="train", run_id="r1", args=["--run-id", "r1"], pool="gpu", cpu="2", memory="1536Mi")
RESULT = {("pipeline", "runs/r1/train/result.json"): {"version": "3", "accuracy": 0.95}}
SCHEDULED = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)


def runner(batch, s3=None):
    return JobRunner(batch, FakeCore(), s3 or FakeS3(RESULT), CONFIG, poll_seconds=0)


def test_job_names_are_valid_dns_labels():
    assert job_name("train", "01A13417_222a") == "train-01a13417-222a"
    assert len(job_name("predict", "x" * 100)) == 63


def test_creates_a_gpu_job_and_returns_its_result():
    batch = FakeBatch([status(active=1), status(active=1), status(succeeded=1)])
    env = ActivityEnvironment()
    # A retry: this attempt began later, the step was scheduled at SCHEDULED.
    env.info = replace(
        env.info, attempt=2, scheduled_time=SCHEDULED, current_attempt_scheduled_time=datetime.now(timezone.utc)
    )

    result = env.run(runner(batch).run_job, TRAIN)

    assert result == {"version": "3", "accuracy": 0.95}
    namespace, job = batch.created[0]
    pod = job.spec.template.spec
    container = pod.containers[0]
    assert (namespace, job.metadata.name) == ("batch-worker", "train-r1")
    assert container.args == [
        "--result-key", result_key("train", "r1"),
        "--started-at", "2026-10-15T10:00:00+00:00",
        "train", "--run-id", "r1",
    ]
    assert container.resources.limits == {"cpu": "2", "memory": "1536Mi", "nvidia.com/gpu": "1"}
    assert pod.node_selector == {"node-pool": "gpu"}
    assert pod.tolerations[0].key == "nvidia.com/gpu"
    assert job.spec.backoff_limit == 1


def test_cpu_job_has_no_gpu_and_no_toleration():
    batch = FakeBatch([status(succeeded=1)])
    predict = JobRequest(step="train", run_id="r1", pool="cpu")

    ActivityEnvironment().run(runner(batch).run_job, predict)

    pod = batch.created[0][1].spec.template.spec
    assert pod.node_selector == {"node-pool": "cpu"} and pod.tolerations is None
    assert "nvidia.com/gpu" not in pod.containers[0].resources.limits


def test_retry_picks_up_the_existing_job():
    batch = FakeBatch([status(succeeded=1)], exists=True)

    result = ActivityEnvironment().run(runner(batch).run_job, TRAIN)

    assert result["version"] == "3" and batch.created == []


def test_failed_job_is_not_retried_and_carries_the_pod_log():
    batch = FakeBatch([status(failed=2, failed_reason="BackoffLimitExceeded")])

    with pytest.raises(ApplicationError) as err:
        ActivityEnvironment().run(runner(batch).run_job, TRAIN)

    assert err.value.non_retryable and err.value.type == "JobFailed"
    assert "BackoffLimitExceeded" in str(err.value) and "no train examples" in str(err.value)


def test_success_without_result_is_an_error():
    batch = FakeBatch([status(succeeded=1)])

    with pytest.raises(ApplicationError, match="no result"):
        ActivityEnvironment().run(runner(batch, FakeS3({})).run_job, TRAIN)
