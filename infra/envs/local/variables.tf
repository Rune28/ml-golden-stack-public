variable "cluster_state_path" {
  description = "State file of the cluster layer."
  type        = string
  default     = "../../clusters/kind/terraform.tfstate"
}

variable "stores_state_path" {
  description = "State file of the data stores layer."
  type        = string
  default     = "../../stores/local/terraform.tfstate"
}

variable "images" {
  description = <<-EOT
    Images of the applications, repository:tag. `make platform-up` writes
    images.auto.tfvars.json with tags derived from the image content, so a
    changed image rolls the pods and an unchanged one changes nothing.
  EOT
  type = object({
    be_service   = string
    chat_service = string
    batch_worker = string
    llm_worker   = string
    mle_models   = string
  })
  default = {
    be_service   = "ghcr.io/rune28/be-service:latest"
    chat_service = "ghcr.io/rune28/chat-service:latest"
    batch_worker = "ghcr.io/rune28/batch-worker:latest"
    llm_worker   = "ghcr.io/rune28/llm-worker:latest"
    mle_models   = "ghcr.io/rune28/mle-models:latest"
  }
}

variable "app_node_port" {
  description = "NodePort of the pipeline page. Must match `port_mappings.app.node_port` of the kind cluster."
  type        = number
  default     = 30901
}

variable "mlflow_ui_node_port" {
  description = "NodePort of the MLflow UI. Must match `port_mappings.mlflow.node_port` of the kind cluster."
  type        = number
  default     = 30500
}

variable "temporal_ui_node_port" {
  description = "NodePort of the Temporal UI. Must match `port_mappings.temporal-ui.node_port` of the kind cluster."
  type        = number
  default     = 30080
}

variable "llm_chat_model" {
  description = "Chat model on the LLM server (`ollama pull` it on the host first)."
  type        = string
  default     = "qwen3:4b"
}

variable "llm_embed_model" {
  description = "Embedding model on the LLM server. A new one needs a new index of every file."
  type        = string
  default     = "bge-m3"
}

variable "llm_profiles" {
  description = "Profiles a chat may name: files in llm_worker/src/workflow/profiles, offered on the chat page."
  type        = list(string)
  default     = ["philosophy", "default"]
}

variable "llm_default_profile" {
  description = "Profile of a chat that names none, one of llm_profiles: the corpus uploaded today is a book of lectures on philosophy."
  type        = string
  default     = "philosophy"
}
