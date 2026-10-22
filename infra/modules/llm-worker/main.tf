# The llm worker: events (an uploaded file, later a chat message) processed in
# the worker against the LLM server. Its connection settings are its own
# ConfigMap and Secret; the LLM settings live only here, so the embedding
# model of the chunks and of the questions is one value, read by one service.

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

locals {
  chart_dir      = "${path.module}/../../charts/llm-worker"
  chart_checksum = sha256(join("", [for f in sort(fileset(local.chart_dir, "**")) : filesha256("${local.chart_dir}/${f}")]))
  env_name       = "llm-worker"

  # [repository, tag]: the tag is everything after the last colon.
  image = regex("^(.+):([^:/]+)$", var.image)
}

resource "kubernetes_config_map_v1" "env" {
  metadata {
    name      = local.env_name
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = merge(
    {
      CLICKHOUSE_HOST     = var.clickhouse.host
      CLICKHOUSE_PORT     = tostring(var.clickhouse.http_port)
      CLICKHOUSE_USER     = var.clickhouse.user
      S3_BUCKET           = var.s3.bucket
      AWS_DEFAULT_REGION  = "us-east-1"
      LLM_BASE_URL        = var.llm.base_url
      LLM_CHAT_MODEL      = var.llm.chat_model
      LLM_EMBED_MODEL     = var.llm.embed_model
      LLM_DEFAULT_PROFILE = var.llm.default_profile
    },
    var.s3.endpoint == null ? {} : { AWS_ENDPOINT_URL_S3 = var.s3.endpoint },
  )
}

resource "kubernetes_secret_v1" "env" {
  metadata {
    name      = local.env_name
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    CLICKHOUSE_PASSWORD   = var.clickhouse_password
    AWS_ACCESS_KEY_ID     = var.s3.access_key
    AWS_SECRET_ACCESS_KEY = var.s3_secret_key
  }
}

resource "helm_release" "this" {
  name      = "llm-worker"
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
    temporal = {
      address = var.temporal_address
    }
    env = {
      configMap = kubernetes_config_map_v1.env.metadata[0].name
      secret    = kubernetes_secret_v1.env.metadata[0].name
    }
    nodeSelector = var.node_selector
  })]
}
