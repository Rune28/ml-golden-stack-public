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

variable "public_origins" {
  description = "Origins (scheme://host:port) the page is served from."
  type        = list(string)
}

variable "db" {
  description = "Its own database and owner role (output of postgres-database), migrated before it starts."
  type = object({
    host     = string
    port     = number
    database = string
    user     = string
  })
}

variable "db_password" {
  type      = string
  sensitive = true
}

variable "token_private_key" {
  description = "PEM private key (Ed25519, PKCS#8) the platform token of a user is signed with."
  type        = string
  sensitive   = true
}

variable "node_selector" {
  description = "Where the service runs."
  type        = map(string)
  default     = {}
}
