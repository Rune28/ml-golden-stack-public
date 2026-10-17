variable "namespace" {
  type    = string
  default = "llm"
}

variable "name" {
  type    = string
  default = "llm"
}

variable "host_address" {
  description = <<-EOT
    Where the model server listens, seen from the cluster. For kind: the
    gateway of the docker network `kind` (172.18.0.1), the only interface the
    server should bind to, so it is not reachable from the rest of the LAN.
  EOT
  type        = string
  default     = "172.18.0.1"
}

variable "port" {
  type    = number
  default = 11434
}
