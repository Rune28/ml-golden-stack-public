variable "namespace" {
  description = "Namespace the migration job runs in."
  type        = string
}

variable "name" {
  description = "Prefix of the job, ConfigMap and Secret."
  type        = string
}

variable "migrations_dir" {
  description = "Directory with golang-migrate files: <version>_<name>.up.sql / .down.sql, one statement per file."
  type        = string
}

variable "db" {
  description = "The database and its owner role (output of postgres-database)."
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

variable "image" {
  type    = string
  default = "migrate/migrate:v4.20.1"
}

variable "node_selector" {
  type    = map(string)
  default = {}
}
