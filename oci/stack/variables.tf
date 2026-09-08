# Inputs for the "schemagate MCP server" Resource Manager stack.
# schema.yaml drives the console form; these are the same names.

variable "tenancy_ocid" { type = string }
variable "region" { type = string }
variable "compartment_ocid" {
  type        = string
  description = "Compartment for the instance, network and (optionally) the Autonomous Database"
}

variable "create_adb" {
  type        = bool
  default     = true
  description = "Create an Always Free Autonomous Database for the demo. Set false to point at your own."
}
variable "adb_admin_password" {
  type        = string
  sensitive   = true
  default     = ""
  description = "ADMIN password for the new database (12-30 chars, upper, lower, digit; no quotes)"
}
variable "database_url" {
  type        = string
  sensitive   = true
  default     = ""
  description = "If create_adb is false: a SQLAlchemy URL for the database to reflect, e.g. oracle+oracledb://user:pw@host:1522/?service_name=..."
}

variable "catalog_provider" {
  type        = string
  default     = "oci"
  description = "Who writes the one-sentence table descriptions on first boot: 'oci' (OCI Generative AI, no API key), or 'none' to skip and use identifiers alone."
}
variable "catalog_model" {
  type        = string
  default     = "google.gemini-2.5-pro"
  description = "OCI Generative AI model id used for cataloguing when catalog_provider = oci"
}

variable "ssh_public_key" {
  type        = string
  description = "SSH public key for the opc user"
}
variable "instance_shape" {
  type    = string
  default = "VM.Standard.E2.1.Micro" # Always Free eligible
}
variable "allowed_cidr" {
  type        = string
  default     = "0.0.0.0/0"
  description = "Who may reach the MCP endpoint. Narrow this to your office or VPN."
}
variable "mcp_port" {
  type    = number
  default = 8765
}
