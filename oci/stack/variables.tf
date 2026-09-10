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
  description = "ADMIN password for the new database (12-30 chars, upper, lower, digit; no quotes; must not contain 'admin')"

  validation {
    # Oracle rejects a password containing the word "admin" in any casing, and
    # the console pattern does not catch it -- so the apply would fail late.
    condition = var.adb_admin_password == "" || (
      length(var.adb_admin_password) >= 12 && length(var.adb_admin_password) <= 30 &&
      can(regex("[A-Z]", var.adb_admin_password)) &&
      can(regex("[a-z]", var.adb_admin_password)) &&
      can(regex("[0-9]", var.adb_admin_password)) &&
      !can(regex("(?i)admin", var.adb_admin_password)) &&
      !can(regex("[\"']", var.adb_admin_password))
    )
    error_message = "12-30 characters, with an upper, a lower and a digit; no quotes; must not contain \"admin\"."
  }
}

variable "adb_version" {
  type        = string
  default     = "19c"
  description = "Autonomous Database version. 19c is the default because it is the one this stack has applied with, repeatedly. Always Free 26ai is offered in every commercial region except Bogota (BOG), Riyadh (RUH) and Singapore West (XSP) - set it if you want it."

  # 23ai is gone from this list on purpose: it stops being a valid value in
  # December 2026, and a stack that still offers it would start failing then.
  # 26ai stays selectable but is not the default: no apply with it set has been
  # seen through to completion, and 19c has finished four.
  validation {
    condition     = contains(["19c", "26ai"], var.adb_version)
    error_message = "adb_version must be 19c or 26ai."
  }
}
variable "database_url" {
  type        = string
  sensitive   = true
  default     = ""
  description = "If create_adb is false: a SQLAlchemy URL for the database to reflect, e.g. oracle+oracledb://user:pw@host:1522/?service_name=..."
}

variable "catalog_provider" {
  type    = string
  default = "oci"

  validation {
    condition     = contains(["oci", "none"], var.catalog_provider)
    error_message = "catalog_provider must be \"oci\" or \"none\". Note that \"oci\" creates a dynamic group and a policy at the tenancy root, which needs tenancy-admin rights; use \"none\" if you do not have them."
  }
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
  type        = string
  default     = "VM.Standard.E2.1.Micro" # Always Free eligible
  description = "Compute shape. VM.Standard.E2.1.Micro and VM.Standard.A1.Flex are the Always Free options; a Flex shape also needs instance_ocpus and instance_memory_gbs."
}

variable "instance_ocpus" {
  type        = number
  default     = 1
  description = "OCPUs, for Flex shapes only. Always Free allows up to 4 across all A1.Flex instances."
}

variable "instance_memory_gbs" {
  type        = number
  default     = 6
  description = "Memory in GB, for Flex shapes only. Always Free allows up to 24 across all A1.Flex instances."
}

variable "availability_domain" {
  type        = string
  default     = ""
  description = "Availability domain name. Leave empty for the first one. Always Free micro instances exist in only one AD, so if you hit \"Out of host capacity\", try another."
}
variable "allowed_cidr" {
  type        = string
  description = "Who may reach the MCP endpoint. No default on purpose: the endpoint has no authentication of its own, so 0.0.0.0/0 lets anyone on the internet assert any identity and read your schema."

  validation {
    condition     = var.allowed_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0. The MCP endpoint has no auth of its own - identity comes from the principal each call passes - so opening it to the internet exposes your schema to anyone. Give the CIDR of your office, VPN or bastion."
  }
}

variable "ssh_cidr" {
  type        = string
  description = "Who may SSH to the instance. Separate from allowed_cidr on purpose: the people who administer the box are rarely the people who query it."

  validation {
    condition     = var.ssh_cidr != "0.0.0.0/0"
    error_message = "Refusing 0.0.0.0/0 for SSH."
  }
}
variable "mcp_port" {
  type    = number
  default = 8765
}

variable "adb_allowed_cidrs" {
  type        = list(string)
  default     = []
  description = "Optional access-control list for the database this stack creates: public CIDRs allowed to connect. Empty means reachable from the internet with TLS and the ADMIN password only -- fine for the throwaway demo database, not for your own data. If you set it, include the instance's own public IP or the MCP server will not be able to connect."
}

variable "schemagate_version" {
  type        = string
  default     = "0.1.10"
  description = "schemagate version the instance installs. Pinned so a stack that worked when you downloaded it still works later: the zip and the PyPI release are cut together, so this is the version this stack was actually tested against. Set it to \"\" to take whatever is newest on PyPI instead."

  validation {
    condition     = var.schemagate_version == "" || can(regex("^[0-9]+\\.[0-9]+(\\.[0-9]+)?([a-z0-9.]*)$", var.schemagate_version))
    error_message = "Give a version like 0.1.10, or \"\" for the newest on PyPI."
  }
}
