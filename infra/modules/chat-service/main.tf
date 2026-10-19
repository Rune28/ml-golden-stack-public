# The chat product: the page to upload files and ask about them. It talks
# only to the platform API, whose address is its one setting, in a
# ConfigMap of its own.

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
      "checksum/settings" = sha256(jsonencode([kubernetes_config_map_v1.env.data]))
    }
    image = {
      repository = local.image[0]
      tag        = local.image[1]
    }
    configMap    = kubernetes_config_map_v1.env.metadata[0].name
    nodeSelector = var.node_selector
  })]
}
