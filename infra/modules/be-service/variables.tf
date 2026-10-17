variable "namespace" {
  type    = string
  default = "be-service"
}

variable "image" {
  description = "be-service image, repository:tag."
  type        = string
}

variable "temporal_address" {
  description = "Temporal frontend, host:port."
  type        = string
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

variable "mlflow_tracking_uri" {
  type = string
}

variable "service" {
  description = "Service of the page and API: ClusterIP, NodePort with node_port, ..."
  type = object({
    type      = string
    node_port = optional(number)
  })
  default = { type = "ClusterIP" }
}

variable "node_selector" {
  type    = map(string)
  default = {}
}

variable "s3" {
  description = "Object store for uploaded files: S3 endpoint (null for AWS), bucket, access key."
  type = object({
    endpoint   = optional(string)
    bucket     = string
    access_key = string
  })
}

variable "s3_secret_key" {
  type      = string
  sensitive = true
}
