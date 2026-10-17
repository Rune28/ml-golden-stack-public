# infra

OpenTofu and Helm. Local today (kind), designed for EKS or GKE next.

| Directory | Holds | State |
|---|---|---|
| `clusters/<name>` | the cluster | its own |
| `envs/<name>` | one environment: wires the modules onto a cluster | its own |
| `modules/*` | one service each, cloud-agnostic | — |
| `charts/*` | Helm charts of our applications (be-service, batch-worker, llm-worker) | — |

## Keeping the cloud move cheap (priority 4)

- Cloud- or kind-specific choices live in `clusters/<name>` and `envs/<name>`. A module takes what differs between clouds as inputs (database endpoint, bucket, credentials) and does not know where it runs; the `*-local` modules stand in for managed services.
- Placement goes through the cluster contract (README, "Cluster contract"): `node-pool=cpu|gpu`, the `nvidia.com/gpu` taint and resource, a default StorageClass.

## Release

- `make platform-up` builds the images and applies `envs/local`: infrastructure and applications (`helm_release`) in one graph, migrations before applications.
- Values the infrastructure owns (hosts, buckets, passwords) reach applications through named ConfigMaps and Secrets (`pipeline-job` in `modules/batch-worker`, shared with its Jobs; `llm-worker` in `modules/llm-worker`). Other OpenTofu values stay out of `helm_release` values: this seam is what lets Argo CD take over the applications later in about a week.
- Pods roll on a settings change through the `checksum/settings` annotation; a new setting goes into the data it hashes.

## State

State is local (`terraform.tfstate` in `clusters/kind` and `envs/local`, gitignored). Change it through `tofu`, not by editing the files.
