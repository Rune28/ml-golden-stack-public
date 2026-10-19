output "namespace" {
  value = helm_release.this.namespace
}

# The platform API inside the cluster, for products such as chat_service.
# 8080 is service.port of the chart, which this module leaves at its default.
output "url" {
  value = "http://${helm_release.this.name}.${helm_release.this.namespace}.svc:8080"
}
