terraform {
  required_version = ">= 1.8"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.1"
    }
  }
}

# Cluster connection comes from the cluster layer's outputs, not from shared
# variables, so this layer can be applied and destroyed on its own.
# The parts that differ per cloud live here: where the cluster state is
# (local file vs S3/GCS) and how providers authenticate (client certificate
# on kind vs `aws eks get-token` / Google access token).
data "terraform_remote_state" "cluster" {
  backend = "local"
  config = {
    path = var.cluster_state_path
  }
}

# The data stores, from their own layer: the same connection data a managed
# database, object store and ClickHouse would give.
data "terraform_remote_state" "stores" {
  backend = "local"
  config = {
    path = var.stores_state_path
  }
}

locals {
  cluster = data.terraform_remote_state.cluster.outputs
  stores  = data.terraform_remote_state.stores.outputs
}

provider "kubernetes" {
  host                   = local.cluster.endpoint
  client_certificate     = local.cluster.client_certificate
  client_key             = local.cluster.client_key
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
}

provider "helm" {
  kubernetes = {
    host                   = local.cluster.endpoint
    client_certificate     = local.cluster.client_certificate
    client_key             = local.cluster.client_key
    cluster_ca_certificate = local.cluster.cluster_ca_certificate
  }
}
