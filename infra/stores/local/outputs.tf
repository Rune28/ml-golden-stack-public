# Read by the environment layer (envs/local) through terraform_remote_state:
# the connection data a managed service would give. Secrets are separate
# sensitive outputs, as in the modules, so plans of the clients stay readable.

output "postgres" {
  value = {
    namespace = module.postgres.namespace
    host      = module.postgres.host
    port      = module.postgres.port
    user      = module.postgres.user
  }
}

output "postgres_password" {
  value     = module.postgres.password
  sensitive = true
}

output "seaweedfs" {
  value = {
    endpoint   = module.seaweedfs.endpoint
    access_key = module.seaweedfs.access_key
    buckets    = module.seaweedfs.buckets
  }
}

output "seaweedfs_secret_key" {
  value     = module.seaweedfs.secret_key
  sensitive = true
}

output "clickhouse" {
  value = {
    namespace   = module.clickhouse.namespace
    host        = module.clickhouse.host
    http_port   = module.clickhouse.http_port
    native_port = module.clickhouse.native_port
    user        = module.clickhouse.user
  }
}

output "clickhouse_password" {
  value     = module.clickhouse.password
  sensitive = true
}
