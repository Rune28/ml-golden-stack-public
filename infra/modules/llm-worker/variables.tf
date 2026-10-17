variable "namespace" {
  description = "Namespace of the llm worker."
  type        = string
  default     = "llm-worker"
}

variable "image" {
  description = "llm-worker image, repository:tag."
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
  description = "Object store the uploaded files are read from. endpoint is null in AWS."
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

variable "llm" {
  description = "The model server (base_url of an OpenAI-compatible API) and the models on it."
  type = object({
    base_url    = string
    chat_model  = string
    embed_model = string
  })
}

variable "node_selector" {
  description = "Where the worker runs."
  type        = map(string)
  default     = {}
}
