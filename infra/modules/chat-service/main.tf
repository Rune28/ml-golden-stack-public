# The chat product: the page to upload files and ask about them, behind a
# login. It talks to the platform API for the data and to its own Postgres
# database for users and sessions: the addresses in a ConfigMap, the
# database password in a Secret.

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

locals {
  chart_dir      = "${path.module}/../../charts/chat-service"
  chart_checksum = sha256(join("", [for f in sort(fileset(local.chart_dir, "**")) : filesha256("${local.chart_dir}/${f}")]))
  image          = regex("^(.+):([^:/]+)$", var.image)
}

resource "kubernetes_config_map_v1" "env" {
  metadata {
    name      = "chat-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    PLATFORM_API_URL = var.platform_api_url
    # Origins the page is served from: a request that changes something
    # must come from one of them (the guard against CSRF, with SameSite).
    PUBLIC_ORIGINS = join(",", var.public_origins)
    DB_HOST        = var.db.host
    DB_PORT        = tostring(var.db.port)
    DB_NAME        = var.db.database
    DB_USER        = var.db.user
  }
}

resource "kubernetes_secret_v1" "env" {
  metadata {
    name      = "chat-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    DB_PASSWORD = var.db_password
    # Signs the token of a user on every call to the platform API.
    PLATFORM_TOKEN_KEY = var.token_private_key
  }
}

resource "helm_release" "this" {
  name      = "chat-service"
  namespace = kubernetes_namespace_v1.this.metadata[0].name
  chart     = local.chart_dir

  wait    = true
  timeout = 300

  values = [yamlencode({
    # helm does not notice edits of a local chart: a checksum of its files
    # in the values makes every edit an upgrade.
    chartChecksum = local.chart_checksum
    podAnnotations = {
      "checksum/settings" = sha256(jsonencode([kubernetes_config_map_v1.env.data, kubernetes_secret_v1.env.data]))
    }
    image = {
      repository = local.image[0]
      tag        = local.image[1]
    }
    configMap    = kubernetes_config_map_v1.env.metadata[0].name
    secret       = kubernetes_secret_v1.env.metadata[0].name
    nodeSelector = var.node_selector
  })]
}
