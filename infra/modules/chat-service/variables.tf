variable "namespace" {
  description = "Namespace of the chat product."
  type        = string
  default     = "chat-service"
}

variable "image" {
  description = "chat-service image, repository:tag."
  type        = string
}

variable "platform_api_url" {
  description = "Base URL of the platform API (be_service) inside the cluster."
  type        = string
}

variable "node_selector" {
  description = "Where the service runs."
  type        = map(string)
  default     = {}
}
