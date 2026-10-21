# Applies the migrations of one Postgres database as a one-off Job before
# its application starts: the same mechanism as clickhouse-migrate (a
# ConfigMap of the SQL files, the stock migrate image, a job named after the
# files), kept as a copy of a few lines rather than one module for both.

locals {
  files = {
    for f in fileset(var.migrations_dir, "*.sql") : f => file("${var.migrations_dir}/${f}")
  }

  # No new files, same name, nothing runs again; a new file, a new job.
  migrations_hash = substr(sha256(jsonencode(local.files)), 0, 8)
}

resource "kubernetes_config_map_v1" "migrations" {
  metadata {
    name      = "${var.name}-files"
    namespace = var.namespace
  }

  data = local.files
}

resource "kubernetes_secret_v1" "this" {
  metadata {
    name      = var.name
    namespace = var.namespace
  }

  data = {
    password = var.db_password
  }
}

resource "kubernetes_job_v1" "this" {
  metadata {
    name      = "${var.name}-${local.migrations_hash}"
    namespace = var.namespace
  }

  spec {
    backoff_limit = 3

    template {
      metadata {
        labels = { "app.kubernetes.io/name" = var.name }
      }

      spec {
        restart_policy = "Never"
        node_selector  = var.node_selector

        container {
          name  = "migrate"
          image = var.image

          # schema_migrations is kept in the database itself, owned by its
          # role. $(DB_PASSWORD) is expanded by Kubernetes from the env; the
          # generated password has no characters that need escaping in a URL.
          args = [
            "-path=/migrations",
            "-database=postgres://${var.db.user}:$(DB_PASSWORD)@${var.db.host}:${var.db.port}/${var.db.database}?sslmode=disable",
            "up",
          ]

          env {
            name = "DB_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.this.metadata[0].name
                key  = "password"
              }
            }
          }

          resources {
            requests = { cpu = "50m", memory = "32Mi" }
            limits   = { memory = "128Mi" }
          }

          volume_mount {
            name       = "migrations"
            mount_path = "/migrations"
            read_only  = true
          }
        }

        volume {
          name = "migrations"
          config_map {
            name = kubernetes_config_map_v1.migrations.metadata[0].name
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "5m"
  }
}
