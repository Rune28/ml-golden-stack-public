variable "cluster_state_path" {
  description = "State file of the cluster layer."
  type        = string
  default     = "../../clusters/kind/terraform.tfstate"
}
