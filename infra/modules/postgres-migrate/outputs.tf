output "job_name" {
  description = "Completed migration job. The application depends on this module, so it starts on the current schema."
  value       = kubernetes_job_v1.this.metadata[0].name
}
