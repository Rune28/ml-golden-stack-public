terraform {
  required_version = ">= 1.8"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

# Cluster connection from the cluster layer's outputs, as in envs/local.
data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = var.cluster_state_path
  }
}

locals {
  cluster = data.terraform_remote_state.cluster.outputs
}

provider "kubernetes" {
  host                   = local.cluster.endpoint
  client_certificate     = local.cluster.client_certificate
  client_key             = local.cluster.client_key
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
}
