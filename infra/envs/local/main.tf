# Local environment on the kind cluster (infra/clusters/kind).
# Only wiring and local choices here; the services themselves are in infra/modules.

locals {
  # Platform services stay off the GPU pool, which is reserved for model steps.
  platform_node_selector = { "node-pool" = "cpu" }
}

# Postgres, SeaweedFS and ClickHouse are in infra/stores/local, with their
# own state: destroying this layer leaves their data. They come in as
# local.stores (versions.tf), the connection data of managed services.

module "clickhouse_migrate" {
  source = "../../modules/clickhouse-migrate"

  namespace      = local.stores.clickhouse.namespace
  migrations_dir = "${path.root}/../../../db/clickhouse/migrations"
  clickhouse = {
    host        = local.stores.clickhouse.host
    native_port = local.stores.clickhouse.native_port
    user        = local.stores.clickhouse.user
  }
  clickhouse_password = local.stores.clickhouse_password
  node_selector       = local.platform_node_selector
}

# MLflow gets its own database and user on the shared server, never the
# admin account and never Temporal's databases.
module "mlflow_db" {
  source = "../../modules/postgres-database"

  name      = "mlflow"
  namespace = local.stores.postgres.namespace
  postgres = {
    host = local.stores.postgres.host
    port = local.stores.postgres.port
    user = local.stores.postgres.user
  }
  postgres_password = local.stores.postgres_password
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
    endpoint   = local.stores.seaweedfs.endpoint
    bucket     = "mlflow"
    access_key = local.stores.seaweedfs.access_key
  }
  s3_secret_key = local.stores.seaweedfs_secret_key

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
    host = local.stores.postgres.host
    port = local.stores.postgres.port
    user = local.stores.postgres.user
  }
  db_password = local.stores.postgres_password

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
    host      = local.stores.clickhouse.host
    http_port = local.stores.clickhouse.http_port
    user      = local.stores.clickhouse.user
  }
  clickhouse_password = local.stores.clickhouse_password

  s3 = {
    endpoint   = local.stores.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = local.stores.seaweedfs.access_key
  }
  s3_secret_key       = local.stores.seaweedfs_secret_key
  mlflow_tracking_uri = module.mlflow.tracking_uri
  node_selector       = local.platform_node_selector

  depends_on = [module.clickhouse_migrate]
}

module "llm_worker" {
  source = "../../modules/llm-worker"

  image            = var.images.llm_worker
  temporal_address = module.temporal.frontend_address

  clickhouse = {
    host      = local.stores.clickhouse.host
    http_port = local.stores.clickhouse.http_port
    user      = local.stores.clickhouse.user
  }
  clickhouse_password = local.stores.clickhouse_password

  # Uploaded files, written there by be_service.
  s3 = {
    endpoint   = local.stores.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = local.stores.seaweedfs.access_key
  }
  s3_secret_key = local.stores.seaweedfs_secret_key
  node_selector = local.platform_node_selector

  llm = {
    base_url        = module.llm.base_url
    chat_model      = var.llm_chat_model
    embed_model     = var.llm_embed_model
    default_profile = var.llm_default_profile
  }

  depends_on = [module.clickhouse_migrate]
}

# The key pair of the platform token: chat_service signs with the private
# key, be_service checks with the public one and cannot make a token itself.
# An identity provider later replaces the private half; be_service then takes
# public keys from its JWKS.
resource "tls_private_key" "platform_token" {
  algorithm = "ED25519"
}

module "be_service" {
  source = "../../modules/be-service"

  image            = var.images.be_service
  temporal_address = module.temporal.frontend_address

  clickhouse = {
    host      = local.stores.clickhouse.host
    http_port = local.stores.clickhouse.http_port
    user      = local.stores.clickhouse.user
  }
  clickhouse_password = local.stores.clickhouse_password
  mlflow_tracking_uri = module.mlflow.tracking_uri

  # Uploaded files, next to the results of pipeline Jobs.
  s3 = {
    endpoint   = local.stores.seaweedfs.endpoint
    bucket     = "pipeline"
    access_key = local.stores.seaweedfs.access_key
  }
  s3_secret_key = local.stores.seaweedfs_secret_key

  # Exposed on the host through the kind port mapping `app`.
  service = {
    type      = "NodePort"
    node_port = var.app_node_port
  }
  node_selector    = local.platform_node_selector
  token_public_key = tls_private_key.platform_token.public_key_pem
  # The same default as the llm worker's: the page shows it chosen on a new chat.
  profiles = {
    names   = var.llm_profiles
    default = var.llm_default_profile
  }

  depends_on = [module.clickhouse_migrate]
}

# The product: the chat page, on the platform API. Reached on the host with
# `make chat-ui` (a port-forward): the kind cluster has no free port mapping.
# The chat product keeps its users and sessions in a database of its own
# on the shared server (principle 9), migrated before it starts.
module "chat_db" {
  source = "../../modules/postgres-database"

  name      = "chat"
  namespace = local.stores.postgres.namespace
  postgres = {
    host = local.stores.postgres.host
    port = local.stores.postgres.port
    user = local.stores.postgres.user
  }
  postgres_password = local.stores.postgres_password
  node_selector     = local.platform_node_selector
}

module "chat_db_migrate" {
  source = "../../modules/postgres-migrate"

  name           = "chat-db-migrate"
  namespace      = local.stores.postgres.namespace
  migrations_dir = "${path.root}/../../../db/postgres/chat/migrations"
  db = {
    host     = module.chat_db.host
    port     = module.chat_db.port
    database = module.chat_db.database
    user     = module.chat_db.user
  }
  db_password   = module.chat_db.password
  node_selector = local.platform_node_selector
}

module "chat_service" {
  source = "../../modules/chat-service"

  image            = var.images.chat_service
  platform_api_url = module.be_service.url
  # The page through make chat-ui (a port-forward to 127.0.0.1:9002).
  public_origins = ["http://127.0.0.1:9002", "http://localhost:9002"]
  db = {
    host     = module.chat_db.host
    port     = module.chat_db.port
    database = module.chat_db.database
    user     = module.chat_db.user
  }
  db_password       = module.chat_db.password
  token_private_key = tls_private_key.platform_token.private_key_pem
  node_selector     = local.platform_node_selector

  depends_on = [module.chat_db_migrate]
}
