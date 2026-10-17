# A model server outside the cluster (Ollama on the host, weights on its GPU),
# given a name inside it. Applications call http://llm.llm.svc and never know
# where the server runs: an in-cluster vLLM or a managed endpoint replaces
# this module, not the callers. Same idea as postgres-local against RDS.
#
# A Service without a selector does not pick pods; the EndpointSlice below
# tells it the address by hand. It must carry the service-name label and the
# same port name as the Service.

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_service_v1" "this" {
  metadata {
    name      = var.name
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }

  spec {
    port {
      name        = "http"
      port        = 80
      target_port = var.port
    }
  }
}

resource "kubernetes_endpoint_slice_v1" "this" {
  metadata {
    name      = "${var.name}-host"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
    labels = {
      "kubernetes.io/service-name"             = kubernetes_service_v1.this.metadata[0].name
      "endpointslice.kubernetes.io/managed-by" = "opentofu"
    }
  }

  address_type = "IPv4"

  endpoint {
    addresses = [var.host_address]
  }

  port {
    name         = "http"
    port         = tostring(var.port)
    protocol     = "TCP"
    app_protocol = "http"
  }
}
