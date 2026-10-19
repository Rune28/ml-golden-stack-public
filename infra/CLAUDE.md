# infra

OpenTofu and Helm. Local today (kind), designed for EKS or GKE next.

| Directory | Holds | State |
|---|---|---|
| `clusters/<name>` | the cluster | its own |
| `stores/<name>` | the data stores of one environment (Postgres, object store, ClickHouse), so their data outlives the environment | its own |
| `envs/<name>` | one environment: wires the modules onto a cluster and its stores | its own |
| `modules/*` | one service each, cloud-agnostic | — |
| `charts/*` | Helm charts of our applications (be-service, chat-service, batch-worker, llm-worker) | — |

## Keeping the cloud move cheap (priority 4)

- Cloud- or kind-specific choices live in `clusters/<name>`, `stores/<name>` and `envs/<name>`. A module takes what differs between clouds as inputs (database endpoint, bucket, credentials) and does not know where it runs; the `*-local` modules stand in for managed services.
- Placement goes through the cluster contract (README, "Cluster contract"): `node-pool=cpu|gpu`, the `nvidia.com/gpu` taint and resource, a default StorageClass.

## Release

- `make platform-up` applies `stores/local`, builds the images and applies `envs/local`: services and applications (`helm_release`) in one graph, migrations before applications.
- What holds data lives in `stores/<name>`, so `make platform-down` keeps it. `envs/<name>` gets the stores as connection data through `terraform_remote_state`, as it would get managed services. A job that creates a database or schema on a store (`postgres-database`, `clickhouse-migrate`) belongs to `envs/<name>` and skips what already exists.
- Values the infrastructure owns (hosts, buckets, passwords) reach applications through named ConfigMaps and Secrets (`pipeline-job` in `modules/batch-worker`, shared with its Jobs; `llm-worker` in `modules/llm-worker`). Other OpenTofu values stay out of `helm_release` values: this seam is what lets Argo CD take over the applications later in about a week.
- Pods roll on a settings change through the `checksum/settings` annotation; a new setting goes into the data it hashes.

## State

State is local (`terraform.tfstate` in `clusters/kind`, `stores/local` and `envs/local`, gitignored). Change it through `tofu`, not by editing the files.
