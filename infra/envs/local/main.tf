# Local environment on the kind cluster (infra/clusters/kind).
# Only wiring and local choices here; the services themselves are in infra/modules.

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
# (database ml_data); its schema comes from db/clickhouse/migrations.
module "clickhouse" {
  source = "../../modules/clickhouse-local"

  node_selector = local.platform_node_selector
}

module "clickhouse_migrate" {
  source = "../../modules/clickhouse-migrate"

  namespace      = module.clickhouse.namespace
  migrations_dir = "${path.root}/../../../db/clickhouse/migrations"
  clickhouse = {
    host        = module.clickhouse.host
    native_port = module.clickhouse.native_port
    user        = module.clickhouse.user
  }
  clickhouse_password = module.clickhouse.password
  node_selector       = local.platform_node_selector
}

# MLflow gets its own database and user on the shared server, never the
# admin account and never Temporal's databases.
module "mlflow_db" {
  source = "../../modules/postgres-database"

  name      = "mlflow"
  namespace = module.postgres.namespace
  postgres = {
    host = module.postgres.host
    port = module.postgres.port
    user = module.postgres.user
  }
  postgres_password = module.postgres.password
  node_selector     = local.platform_node_selector
}

module "mlflow" {
  source = "../../modules/mlflow"

  db = {
    host     = module.mlflow_db.host
    port     = module.mlflow_db.port
    database = module.mlflow_db.database
    user     = module.mlflow_db.user
  }
  db_password = module.mlflow_db.password

  s3 = {
    endpoint   = module.seaweedfs.endpoint
    bucket     = "mlflow"
    access_key = module.seaweedfs.access_key
  }
  s3_secret_key = module.seaweedfs.secret_key

  # Exposed on the host through the kind port mapping `mlflow`.
  service = {
    type      = "NodePort"
    node_port = var.mlflow_ui_node_port
  }
  node_selector = local.platform_node_selector
}

module "temporal" {
  source = "../../modules/temporal"

  db = {
    host = module.postgres.host
    port = module.postgres.port
    user = module.postgres.user
  }
  db_password = module.postgres.password

  # Small to keep the footprint low; cannot be changed later without
  # recreating the database.
  num_history_shards = 4
  node_selector      = local.platform_node_selector

  # Exposed on the host through the kind port mapping `temporal-ui`.
  web_service = {
    type     = "NodePort"
    nodePort = var.temporal_ui_node_port
  }
}

# In the cloud this is a model server in the cluster (vLLM) or a managed
# endpoint instead. Locally Ollama runs on the host, weights on its GPU; the
# cluster reaches it as http://llm.llm.svc.
module "llm" {
  source = "../../modules/llm-local"
}

# Applications. They start only after the schema they use is migrated.

module "batch_worker" {
  source = "../../modules/batch-worker"

  image            = var.images.batch_worker
  job_image        = var.images.mle_models
  temporal_address = module.temporal.frontend_address

  clickhouse = {
    host      = module.clickhouse.host
    http_port = module.clickhouse.http_port
    user      = module.clickhouse.user
  }
  clickhouse_password = module.clickhouse.password

  s3 = {
    endpoint   = module.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = module.seaweedfs.access_key
  }
  s3_secret_key       = module.seaweedfs.secret_key
  mlflow_tracking_uri = module.mlflow.tracking_uri
  node_selector       = local.platform_node_selector

  depends_on = [module.clickhouse_migrate]
}

module "llm_worker" {
  source = "../../modules/llm-worker"

  image            = var.images.llm_worker
  temporal_address = module.temporal.frontend_address

  clickhouse = {
    host      = module.clickhouse.host
    http_port = module.clickhouse.http_port
    user      = module.clickhouse.user
  }
  clickhouse_password = module.clickhouse.password

  # Uploaded files, written there by be_service.
  s3 = {
    endpoint   = module.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = module.seaweedfs.access_key
  }
  s3_secret_key = module.seaweedfs.secret_key
  node_selector = local.platform_node_selector

  llm = {
    base_url    = module.llm.base_url
    chat_model  = var.llm_chat_model
    embed_model = var.llm_embed_model
  }

  depends_on = [module.clickhouse_migrate]
}

module "be_service" {
  source = "../../modules/be-service"

  image            = var.images.be_service
  temporal_address = module.temporal.frontend_address

  clickhouse = {
    host      = module.clickhouse.host
    http_port = module.clickhouse.http_port
    user      = module.clickhouse.user
  }
  clickhouse_password = module.clickhouse.password
  mlflow_tracking_uri = module.mlflow.tracking_uri

  # Uploaded files, next to the results of pipeline Jobs.
  s3 = {
    endpoint   = module.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = module.seaweedfs.access_key
  }
  s3_secret_key = module.seaweedfs.secret_key

  # Exposed on the host through the kind port mapping `app`.
  service = {
    type      = "NodePort"
    node_port = var.app_node_port
  }
  node_selector = local.platform_node_selector

  depends_on = [module.clickhouse_migrate]
}
