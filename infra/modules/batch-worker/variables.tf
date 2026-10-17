variable "namespace" {
  description = "Namespace of the worker and of the pipeline Jobs it starts."
  type        = string
  default     = "batch-worker"
}

variable "image" {
  description = "batch-worker image, repository:tag."
  type        = string
}

variable "job_image" {
  description = "mle-models image the pipeline Jobs run, repository:tag."
  type        = string
}

variable "temporal_address" {
  type = string
}

variable "clickhouse" {
  type = object({
    host      = string
    http_port = number
    user      = string
  })
}

variable "clickhouse_password" {
  type      = string
  sensitive = true
}

variable "s3" {
  description = "Object store for the Jobs' result.json and the MNIST cache. endpoint is null in AWS."
  type = object({
    endpoint   = string
    bucket     = string
    access_key = string
  })
}

variable "s3_secret_key" {
  type      = string
  sensitive = true
}

variable "mlflow_tracking_uri" {
  type = string
}

variable "node_selector" {
  description = "Where the worker runs. The Jobs choose their pool per step."
  type        = map(string)
  default     = {}
}
