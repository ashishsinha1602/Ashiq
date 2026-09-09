# schemagate MCP server on OCI: one Always-Free-eligible VM running
# `python -m schemagate.mcp_server` over streamable-http, reflecting an
# Autonomous Database. Identity-scoped schema selection for any MCP client.
#
# Creates: VCN, subnet, internet gateway, security list, a dynamic group and
# policy so the VM can call OCI Generative AI, an ATP instance (optional), and
# the VM. Nothing else. Destroying the stack removes all of it.
terraform {
  required_version = ">= 1.2"
  required_providers {
    oci = { source = "oracle/oci", version = "~> 7.0" }
  }
}

provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}
