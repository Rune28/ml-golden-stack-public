# The platform API: the MNIST page and its API, and uploads of files for
# retrieval. Talks to ClickHouse, Temporal (the only client), MLflow (model
# versions) and the object store (uploaded files).

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_secret_v1" "this" {
  metadata {
    name      = "be-service"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  data = {
    CLICKHOUSE_PASSWORD   = var.clickhouse_password
    AWS_ACCESS_KEY_ID     = var.s3.access_key
    AWS_SECRET_ACCESS_KEY = var.s3_secret_key
  }
}

locals {
  chart_dir      = "${path.module}/../../charts/be-service"
  chart_checksum = sha256(join("", [for f in sort(fileset(local.chart_dir, "**")) : filesha256("${local.chart_dir}/${f}")]))
  image          = regex("^(.+):([^:/]+)$", var.image)
}

resource "helm_release" "this" {
  name      = "be-service"
  namespace = kubernetes_namespace_v1.this.metadata[0].name
  chart     = local.chart_dir

  wait    = true
  timeout = 300

  values = [yamlencode({
    # helm does not notice edits of a local chart: a checksum of its files
    # in the values makes every edit an upgrade.
    chartChecksum = local.chart_checksum
    podAnnotations = {
      "checksum/settings" = sha256(jsonencode([kubernetes_secret_v1.this.data]))
    }
    image = {
      repository = local.image[0]
      tag        = local.image[1]
    }
    env = merge(
      {
        CLICKHOUSE_URL      = "http://${var.clickhouse.host}:${var.clickhouse.http_port}"
        CLICKHOUSE_USER     = var.clickhouse.user
        CLICKHOUSE_DATABASE = "ml_data"
        TEMPORAL_ADDRESS    = "http://${var.temporal_address}"
        TEMPORAL_NAMESPACE  = "default"
        MLFLOW_URL          = var.mlflow_tracking_uri
        S3_BUCKET           = var.s3.bucket
      },
      var.s3.endpoint == null ? {} : { AWS_ENDPOINT_URL_S3 = var.s3.endpoint },
    )
    secretName = kubernetes_secret_v1.this.metadata[0].name
    service = {
      type     = var.service.type
      nodePort = var.service.node_port
    }
    nodeSelector = var.node_selector
  })]
}
