# Data stores of the local environment on the kind cluster (infra/clusters/kind).
# Own state, so `make platform-down` removes services and applications and
# leaves the data; only `make stores-down` deletes it. In the cloud this layer
# is the managed services themselves, created and kept apart from releases.

locals {
  # Platform services stay off the GPU pool, which is reserved for model steps.
  platform_node_selector = { "node-pool" = "cpu" }
}

# In the cloud this is a managed database (RDS, Cloud SQL) instead.
module "postgres" {
  source = "../../modules/postgres-local"

  node_selector = local.platform_node_selector
}

# In the cloud this is S3 / GCS instead. `mlflow` holds model artifacts,
# `pipeline` holds the MNIST cache and results of pipeline Jobs.
module "seaweedfs" {
  source = "../../modules/seaweedfs-local"

  buckets       = ["mlflow", "pipeline"]
  node_selector = local.platform_node_selector
}

# In the cloud this is a managed ClickHouse instead. Holds the pipeline data
# (database ml_data); its schema comes from db/clickhouse/migrations, applied
# by the environment layer.
module "clickhouse" {
  source = "../../modules/clickhouse-local"

  node_selector = local.platform_node_selector
}
