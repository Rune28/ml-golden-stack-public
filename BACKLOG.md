# Backlog

## Next: document graph

Documents, authors and concepts as Data Vault hubs with links, so a question about a topic finds every document on it. Plan, decisions and slices: [docs/documents-plan.md](docs/documents-plan.md).

- [x] S5 Documents: PDF, `sat_document`, `link_document_author`, hub views, activity `read_document` in `Index`
- [x] S6a Keywords at upload: activity `extract_mentions` (TF-IDF within the document + bge-m3, no LLM), `chunk_mentions`, `document_themes`
- [x] S7 Questions about a topic in `Chat`: `route`, `gather`, `summarize` (map → themes → a paragraph per theme)
- [ ] S6b Concept graph: Job `build_concepts` over the corpus: canonical terms, `broader` hierarchy, aliases, `concept_ancestors`, one run per graph
- [ ] Index runs replaced by a newer one are never deleted: every `reindex-all` keeps another copy of the chunks (readers do not see them)

## Done: LLM conveyor

Upload files, index them for retrieval, chat about them with a memory of the conversation. Plan, decisions and slices: [docs/llm-plan.md](docs/llm-plan.md).

- [x] S1 LLM endpoint: Ollama on the host, `modules/llm-local`, the adapter in llm_worker, `make llm-check`
- [x] S2 Files and index: `files`, `doc_chunks`, workflow `Index`, activity `index_file` in the worker
- [x] S3 Conversation: entity workflow `Chat` with Update, `chat_messages`
- [x] S4 chat_service: the page for upload and chat on the platform API

Before S1: Ollama installed on the host (no sudo needed). 32 GB of RAM is welcome but not required (see the memory budget in the plan).

## Local platform

### Highly available Postgres (CloudNativePG)

Postgres is a single instance (`modules/postgres-local`, one replica on one node). If its node goes down, Temporal loses its database. The three `cpu` nodes are already there to spread instances over.

- [ ] CloudNativePG operator installed by its helm chart (CRDs handled as in [Helm charts and CRDs](#helm-charts-and-crds))
- [ ] New module `postgres-cnpg` replaces `postgres-local` in `stores/local`: a `Cluster` with 3 instances, one primary and two replicas
- [ ] Instances spread one per `cpu` node (pod anti-affinity on `kubernetes.io/hostname`), PodDisruptionBudget
- [ ] Automatic failover: when the primary goes down, a replica is promoted within seconds
- [ ] Temporal connects to the `<cluster>-rw` service, which always points to the current primary. `modules/temporal` and `envs/local` do not change, only the `postgres` outputs of `stores/local`
- [ ] Failover drill: delete the primary pod, check that a replica is promoted and Temporal keeps serving (`temporal operator cluster health`, `temporal workflow list`)
- [ ] Backups and WAL archive to SeaweedFS (S3 API, `modules/seaweedfs-local`). HA does not protect from `DROP TABLE`: it is replicated instantly

### Kueue: queueing pipeline Jobs

`run_job` in batch_worker creates Jobs directly, so the scheduler decides alone: with one (simulated) GPU, a second train Job sits `Pending` with no visible queue, priority or quota. Kueue makes the queue explicit and is the same on kind and in the cloud.

- [ ] Kueue installed by its helm chart (CRDs handled as in [Helm charts and CRDs](#helm-charts-and-crds))
- [ ] `ResourceFlavor` per pool from the [cluster contract](README.md#cluster-contract): `cpu` (`node-pool=cpu`) and `gpu` (`node-pool=gpu` + toleration `nvidia.com/gpu`)
- [ ] `ClusterQueue` with quotas for `cpu`, `memory`, `nvidia.com/gpu`; `LocalQueue` in the `batch-worker` namespace
- [ ] `run_job` creates Jobs with the label `kueue.x-k8s.io/queue-name` and `suspend: true`, without its own nodeSelector and tolerations: the flavor sets them
- [ ] Priorities: `run` (inference) above `train`
- [ ] Check: two train workflows started together, the second Job waits admitted-but-suspended in the queue, not `Pending` on the scheduler
- [ ] Time in the queue is separate from run time. Today `start_to_close = deadline + 120 s` counts from the activity start, while a suspended Job does not run its `activeDeadlineSeconds`: three attempts time out and the workflow fails with the Job still queued. Waiting for admission gets its own timeout (a separate activity or a workflow timer), `start_to_close` covers only the running Job
- [ ] Check: a train Job waits in the queue longer than its deadline, the workflow still finishes

### Highly available ClickHouse and SeaweedFS

ClickHouse (`modules/clickhouse-local`) and SeaweedFS (`modules/seaweedfs-local`) are single instances, like Postgres before CloudNativePG. Losing their node loses the dataset, predictions and model artifacts.

- [ ] ClickHouse: ClickHouse Keeper (3 instances) + 2 replicas, tables on `ReplicatedMergeTree`, one replica per `cpu` node
- [ ] Migrations in `db/clickhouse/migrations` updated for `ON CLUSTER` and replicated engines
- [ ] SeaweedFS: master (3 for raft), volume servers and filer split into separate StatefulSets over the `cpu` nodes, replication `001` so a volume survives the loss of one node
- [ ] Failover drill for both: delete a pod during `ingest` and `train`, the workflows finish

### Real GPU on the local gpu node

The `gpu` pool only simulates a GPU: a fake `nvidia.com/gpu` resource, pods run on CPU. Needed once a model step has to run on CUDA locally. Optional per machine: `simulated` stays the default for hosts without an NVIDIA GPU.

A first version was written and never run end to end, then removed. It is in commit `8eaa66d` (`gpu_mode`, `setup-gpu-nodes.sh`, `modules/nvidia-device-plugin`, preflight checks, README section) and can be restored from there.

- [ ] `gpu_mode` variable in `clusters/kind`: `simulated` (default) or `nvidia`, set per machine in the gitignored `local.auto.tfvars`
- [ ] Host prerequisites documented, not automated (they need `sudo`): NVIDIA driver, nvidia-container-toolkit, nvidia as docker's default runtime, `accept-nvidia-visible-devices-as-volume-mounts = true`
- [ ] `make preflight` checks the host prerequisites in `nvidia` mode
- [ ] GPU node gets the host GPUs through the `/dev/null` → `/var/run/nvidia-container-devices/all` mount, the toolkit inside the node, nvidia as the default containerd runtime (steps of NVIDIA's [nvkind](https://github.com/NVIDIA/nvkind))
- [ ] NVIDIA device plugin installed in `envs/local` in `nvidia` mode; GPU nodes labelled `nvidia.com/gpu.present=true`
- [ ] End-to-end check: a pod requesting `nvidia.com/gpu: 1` on the `gpu` pool runs `nvidia-smi` and sees the host GPU
- [ ] Model step manifests unchanged between `simulated` and `nvidia`
- [ ] LLM server in a pod on the GPU: a module `llm-serving` (Ollama or vLLM) replaces `llm-local` in `envs/local` and keeps its contract: Service `llm` in namespace `llm`, port 80 named `http`, an OpenAI-compatible API under `/v1`, output `base_url`. Callers do not change. With one GPU shared by the LLM and train Jobs, NVIDIA time-slicing; a server per model (vLLM) needs a separate `LLM_EMBED_BASE_URL` in the adapter

### Memory of the LLM server

The memory budget of [docs/llm-plan.md](docs/llm-plan.md#memory-budget) gives Ollama about 1 GB, the weights being in VRAM. On 2026-10-19, after a day of indexing, keyword extraction and topic answers, `ollama.service` held 8.5 GB of host memory (1.4 GB in RAM, 7.1 GB in swap) while `ollama ps` showed `qwen3:4b` at 100% GPU. The host ran out of swap (load average up to 144); Temporal heartbeats, DNS in the cluster and the API server timed out; a restart of the service gave the memory back (0.7 GB right after).

A first measurement, right after the restart: one topic answer (14 calls of qwen3) took `ollama.service` from 0.6 GB to 4.6 GB, of which 4.1 GB anonymous memory of a single `llama-server` (RSS 4.7 GB) with the model at 100% GPU: a host copy of the weights, it seems (read without mmap), that a model loaded again after the other one may not give back.

- [ ] Measure what grows: memory of `ollama.service` (`memory.current`, `memory.swap.current` of its cgroup) over a run of `extract_mentions` on a book and over a few topic answers
- [ ] Check the suspects one by one: the two models loaded in turn (bge-m3 for embeddings, qwen3 for chat) and their host buffers; the 8192-token context of every runner; requests queued while a long answer runs
- [ ] Settings that bound it, if they help: `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_NUM_PARALLEL`, `OLLAMA_KEEP_ALIVE`; or a memory limit on the systemd user unit (`MemoryMax`, `MemorySwapMax`) so the server is restarted instead of the host swapping
- [ ] The memory budget of the plan written again from what is measured
- [ ] `make llm-check` reports the memory of the server next to the answer, so a leak is seen before the host swaps

## Pipeline correctness

Retries are safe today in the common case (see the contracts in [README](README.md#pipeline)), but some guarantees hold only by accident: a time window, the fixed workflow ID, or "nobody deletes a batch".

### Idempotency of pipeline steps

- [ ] `run_job` reads `result.json` before creating the Job and returns it when present. Today idempotency lasts as long as the Job exists (`ttlSecondsAfterFinished: 3600`): an activity retried later (Temporal down for 2 hours) finds no Job, creates a new one and trains again. With the check, the window is the retention of the `pipeline` bucket
- [ ] Test: Job deleted, `result.json` present → no new Job, the stored result is returned
- [ ] `train` registers at most one model version per `run_id`. Today the version is registered in `log_model`, `result.json` is written later: a pod that dies in between is restarted by `backoffLimit: 1` and registers a second version. Look up an MLflow run tagged `pipeline_run_id` first and reuse its version
- [ ] Add data: offset from `max(idx) + 1` of the train split (or an explicit range per batch), not from `count()`. Today a deleted batch, or a retry of a partial batch after another batch was added, makes `idx` ranges overlap and duplicates examples
- [ ] Invariant written down next to `samples`: append-only, batches are never deleted outside the partial-batch rewrite

### Promotion safety

Promotion is correct only because the workflow ID `train` lets one Train run at a time. Two Trains with different IDs (a sweep, a second model) both read champion v3, both promote, and the last one wins even if it is worse.

- [ ] `promote` is compare-and-set: the Job gets the champion version the workflow compared against and refuses when the alias has moved since
- [ ] The workflow handles the refusal: compares against the new champion again or reports `promoted: false`
- [ ] Check: two Train workflows with different IDs started together, the better model ends up `champion`

### Lineage

- [ ] Train reads a snapshot: first the list of `batch_id`s, then `samples WHERE batch_id IN (...)`, and the same list goes to `train_batches.json`. Today the samples and the batch list are two separate queries, and an Add data between them puts a batch into lineage that the model never saw
- [ ] Test: Add data during training does not change `train_batches.json` of the running Train
- [ ] Code version in MLflow: the `mle_models` image (tag and digest) as a run tag; batch_worker passes `JOB_IMAGE` to the Job. Today "which code trained v7" has no answer
- [ ] `predictions` inherits the source `batch_id` of each example (README, rule 5), so lineage from a prediction back to its batch is a join

### Run registry

`ml_data.runs` (README, rule 11) holds markers of `predict` and `report` runs, and be_service reads reports through `digit_report_latest`. The rest of the platform still reads tables directly.

- [ ] be_service commits every batch (`kind = 'batch'`) after its insert; backfill markers for the batches already in `samples`
- [ ] Train reads committed batches only: the batch list from `runs`, then `samples WHERE batch_id IN (...)`. This is also the snapshot from [Lineage](#lineage): `train_batches.json` is the same list
- [ ] Views for every read of be_service (`stats`, `batches`, `prediction_runs`, `examples`, `confusion`, `digit_accuracy`) and of the Jobs (`read_split`, `read_predictions`), each over committed runs only
- [ ] Grants enforce it: readers get `SELECT` on views only (with [a ClickHouse user per component](#access-and-security))
- [ ] Cleanup of rows whose run was never committed (the step failed after writing): a scheduled job deletes rows of runs older than a day with no marker
- [ ] `run_id` is checked at the entry (be_service, workflow input): a UUID made by the platform, since the registry assumes it unique across all kinds
- [ ] Rollout note for 0006–0009: a report written by the previous image after the migration has no marker and stays hidden until reported again. On a long-lived cluster the view switch (0009) goes in the release after every writer commits

### Contracts between be_service and the workers

- [ ] One schema source (JSON Schema) for workflow inputs and results. Today be_service sends untyped `json!` and a drift shows up only at run time
- [ ] Contract test in CI on both sides: be_service payloads decode into the `contracts.py` dataclasses, workflow results decode in be_service

### run_job at scale

- [ ] Today one thread per running Job and a status poll every 2 s: fine for 8 Jobs, 500 requests/s to the API server for 1000. Watch Jobs instead of polling, or poll from the workflow with timers and short activities

## Infrastructure: long-lived cluster

Needed once the stack runs on a cluster that is updated in place (EKS) instead of being recreated like the local kind cluster. Goal: every change reaches a running cluster without downtime or data loss.

### Change management

#### Separate state per layer

Split OpenTofu state into `infra` (VPC, EKS, node groups) and `platform` (helm releases), so a failing helm release cannot touch the cluster itself.

- [ ] `infra` and `platform` are separate root modules with separate state
- [ ] `platform` reads cluster connection data from `infra` outputs (remote state or data sources), not from shared variables
- [ ] Destroying or re-applying `platform` leaves `infra` untouched

#### Remote state

- [ ] State stored in S3 with locking (`use_lockfile = true`)
- [ ] OpenTofu state encryption enabled, key in KMS
- [ ] Bucket versioning on, public access blocked, access limited to CI and admins

#### Protection against deletion

Mark resources holding the cluster or data with `lifecycle { prevent_destroy = true }`.

- [ ] EKS cluster
- [ ] S3 buckets with models and results
- [ ] RDS instances for Temporal and MLflow
- [ ] A plan that would destroy any of them fails

#### Plan review before apply

No `-auto-approve` outside the local environment.

- [ ] `make` targets for cloud environments run `plan` and `apply` as separate steps
- [ ] Apply uses a saved plan file (`tofu plan -out`, then `tofu apply <file>`), so what was reviewed is what gets applied
- [ ] Review checklist in the PR template: look for `must be replaced` and `destroy`

#### Apply from CI

Changes reach cloud environments only through CI, which also gives an audit log.

- [ ] On pull request: `fmt -check`, `validate`, `plan`, plan posted as a PR comment
- [ ] On merge to main: `apply` of the reviewed plan
- [ ] Cloud credentials available only to CI (OIDC role), not to laptops
- [ ] Applies are serialized (one at a time per environment)

### Data and backups

The cluster is disposable only when data lives outside it.

- [ ] Models and results in S3 instead of in-cluster SeaweedFS
- [ ] Temporal and MLflow databases on RDS Postgres (or Temporal Cloud), Multi-AZ, automated snapshots
- [ ] Secrets in AWS Secrets Manager, synced into the cluster by External Secrets Operator
- [ ] ClickHouse backed up with `BACKUP DATABASE app TO S3(...)` on a schedule (incremental, `base_backup`), retention configured
- [ ] SeaweedFS buckets (`mlflow`, `pipeline`) versioned and replicated to another bucket or S3; once in the cloud, S3 versioning and lifecycle rules instead
- [ ] Kubernetes objects and volumes backed up with Velero on a schedule
- [ ] Restore is tested: a scheduled drill restores RDS, ClickHouse and Velero backups into a scratch environment, and `run` on the restored data gives the same accuracy

### Kubernetes upgrades

- [ ] Upgrade procedure: control plane first, then nodes, one minor version at a time
- [ ] Deprecated API check (`pluto` or `kubent`) runs in CI against rendered manifests and charts, and before every upgrade
- [ ] Nodes replaced gradually: Karpenter drift for Karpenter-managed nodes, rolling update for managed node groups
- [ ] Upgrade rehearsed on a staging cluster before production

### Disruption tolerance

Node drains during upgrades and spot interruptions must not take services down.

- [ ] Temporal (frontend, history, matching, worker), MLflow and pipeline workers run at least 2 replicas
- [ ] PodDisruptionBudget for each of them (`maxUnavailable: 1`)
- [ ] be_service Add data serialized across replicas: today an in-process mutex keeps two batches from reading the same offset, which holds only with one replica. Options: a Postgres advisory lock, or Add data as a Temporal workflow with a fixed ID
- [ ] Topology spread constraints across availability zones
- [ ] Readiness and liveness probes on every service
- [ ] Resource requests and limits on every container
- [ ] Pipeline Jobs on spot nodes (the `gpu` pool is `capacity-type=spot`): `podFailurePolicy` with `DisruptionTarget → Ignore`, so an eviction does not count against `backoffLimit`. Today two evictions in a row fail the Job, and `run_job` marks that non-retryable like a bug in the code
- [ ] Long trainings checkpoint to the object store and resume after an eviction

### Helm charts and CRDs

- [ ] Every chart version pinned, upgrades go through a PR with the changelog linked
- [ ] For each chart, document how it ships CRDs. `helm upgrade` does not update CRDs placed in the chart's `crds/` directory, so those are applied as a separate step before the chart upgrade
- [ ] CRDs for Kueue and any Temporal operator covered by the step above

### Stack-specific upgrades

#### Temporal workflow versioning

Long-running workflows outlive worker deployments. Changed workflow code replaying an old history fails with a non-determinism error.

- [ ] Workflow changes guarded with `workflow.patched()` or Temporal Worker Versioning
- [ ] batch_worker and llm_worker (Python SDK): workflow code stays deterministic under the SDK sandbox; `workflow.patched("<change-id>")` around changed steps of `Ingest`/`Train`/`Run`, `workflow.deprecate_patch()` once old runs are gone
- [ ] Inputs and results of workflows and `run_job` are versioned dataclasses: new fields only with defaults, so be_service (Rust) and old histories keep decoding
- [ ] CI replay test: recorded workflow histories replayed against the new worker code
- [ ] Deployment procedure keeps old workers until workflows started on old code finish (or are migrated)

#### Database schema migrations

- [ ] Temporal server upgrades run schema migrations (`temporal-sql-tool` or the chart's schema update job) before new server pods start
- [ ] MLflow upgrades run `mlflow db upgrade` as a Job before the new server starts
- [ ] Database snapshot taken before every migration
- [ ] ClickHouse migrations follow expand/contract. Adding (a column with `DEFAULT` or `Nullable`): migration first, then code; `depends_on = [module.clickhouse_migrate]` already enforces this order. Removing or renaming: code first, migration in a later release; a rename takes three releases (add the new column, write both and read the new one, drop the old one)
- [ ] A schema change stays compatible with the previous `mle_models` image for at least the longest Job deadline: Jobs started before the rollout keep running the old code
- [ ] CI fails when an already applied migration file is changed. Today the hash in `clickhouse-migrate` gives a new Job name, the Job runs, `migrate` sees the version as applied and silently skips the edit
- [ ] Runbook for a migration left `dirty` by golang-migrate (`migrate force <version>` after fixing the schema by hand)
- [ ] Contract step of migration 0005 (`digit_report.report_run_id` → `run_id`, README rule 10), in a release after every running image writes `run_id`: three migrations, one statement each, `MATERIALIZE COLUMN run_id` (with `mutations_sync = 2`), `MODIFY COLUMN run_id REMOVE DEFAULT`, `DROP COLUMN report_run_id`; then `write_digit_report` stops writing `report_run_id`

#### Kueue

- [ ] Before upgrading, check release notes for API version changes and deprecations of Kueue resources
- [ ] Kueue manifests in the repo use the current API version

### Drift detection

On a long-lived cluster someone will run `kubectl edit` "for a minute".

- [ ] GitOps: OpenTofu creates the infrastructure and installs Argo CD (or Flux), Argo CD deploys platform and applications from the repo and reverts manual changes (README rule 13). The move:
  - [ ] Argo CD Applications for `infra/charts/batch-worker`, `infra/charts/llm-worker` and `infra/charts/be-service`, with the same release names and resource names, so Argo CD adopts the running resources instead of recreating them
  - [ ] `tofu state rm` of the `helm_release`s, so the next apply does not delete them
  - [ ] `depends_on = [module.clickhouse_migrate]` becomes a sync wave or a PreSync hook
  - [ ] Settings the application owns (model names) move from OpenTofu variables to a ConfigMap of the chart, with `checksum/config` from the chart template; `pipeline-job` keeps only what the infrastructure owns
- [ ] Scheduled `tofu plan` in CI, alert when the plan is not empty

### Access and security

- [ ] No shared admin kubeconfig: access through IAM / SSO (EKS access entries) and RBAC roles
- [ ] Authentication on be_service: OIDC login (or a session behind an auth proxy such as oauth2-proxy) before any button; Temporal, ClickHouse and MLflow are reachable only from inside the cluster, never through be_service as a proxy
- [ ] A ClickHouse user per component instead of the one `platform` user (access management, effectively admin) in every Secret: migrations get DDL; be_service gets `INSERT` and `DELETE` on `samples` and `SELECT`; mle_models Jobs get `SELECT` on `samples` and `INSERT`/`DELETE` on `predictions` and `digit_report`; llm_worker gets `SELECT` on `files` and `INSERT`/`DELETE` on `doc_chunks`; batch_worker gets none. Today any model step can `DROP DATABASE ml_data`
- [ ] batch_worker's own settings split from the Jobs' `pipeline-job` ConfigMap and Secret: the worker needs only S3 to read `result.json`, not ClickHouse
- [ ] NetworkPolicy: deny by default between namespaces, explicit allows
- [ ] Node images (AMI) updated on a schedule
- [ ] Container images scanned in CI, builds fail on critical vulnerabilities
- [ ] Workloads run as non-root, Pod Security Standards `restricted` where possible
- [ ] Audit logs of the Kubernetes API enabled

### Observability

- [ ] Metrics: Prometheus, Grafana dashboards for cluster, Temporal, Kueue queues, GPU (DCGM)
- [ ] Alerts in Alertmanager with runbook links: node not ready, pending pods, Kueue backlog, Temporal task queue backlog, failed workflows, disk and memory pressure
- [ ] Pipeline metrics: duration and outcome of every `run_job` by step (ingest, train, predict), time a Job waits before running, accuracy of each model version, rows in `samples` and `predictions`
- [ ] ClickHouse metrics (built-in Prometheus endpoint) and MLflow server metrics (`--expose-prometheus`) on the same dashboards
- [ ] Centralized logs with retention
- [ ] Tracing through workflows (OpenTelemetry)

### Cost

- [ ] OpenCost (or Kubecost) with cost per namespace and per pipeline
- [ ] Cost per processed match tracked as a metric
- [ ] Budget alerts in the cloud account

### Runbooks

Stored in `docs/runbooks/`, each with steps, checks and rollback.

- [ ] Upgrade Kubernetes
- [ ] Upgrade a helm chart with CRDs
- [ ] Restore from backup (RDS, Velero)
- [ ] Temporal is down
- [ ] Spot capacity exhausted
- [ ] Rotate credentials and secrets
