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
    oci = { source = "oracle/oci", version = ">= 5.0" }
  }
}

provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

data "oci_core_images" "ol" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
  shape                    = var.instance_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# ---------------- network ----------------
resource "oci_core_vcn" "vcn" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.42.0.0/16"]
  display_name   = "schemagate-vcn"
  dns_label      = "schemagate"
}

resource "oci_core_internet_gateway" "igw" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "schemagate-igw"
}

resource "oci_core_route_table" "rt" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.igw.id
  }
}

resource "oci_core_security_list" "sl" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.vcn.id
  display_name   = "schemagate-sl"
  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.allowed_cidr
    tcp_options {
      min = var.mcp_port
      max = var.mcp_port
    }
  }
  ingress_security_rules {
    protocol = "6"
    source   = var.allowed_cidr
    tcp_options {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_subnet" "subnet" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.vcn.id
  cidr_block        = "10.42.1.0/24"
  display_name      = "schemagate-subnet"
  dns_label         = "app"
  route_table_id    = oci_core_route_table.rt.id
  security_list_ids = [oci_core_security_list.sl.id]
}

# ---------------- let the VM call OCI Generative AI, keylessly ----------------
# The instance principal is what makes cataloguing work with no API key and no
# prompt leaving the tenancy. Both live at tenancy root, which is where OCI
# requires dynamic groups and where GenAI policies are normally written.
resource "oci_identity_dynamic_group" "dg" {
  count          = var.catalog_provider == "oci" ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "schemagate-mcp-dg"
  description    = "The schemagate MCP instance"
  matching_rule  = "ALL {instance.compartment.id = '${var.compartment_ocid}'}"
}

resource "oci_identity_policy" "genai" {
  count          = var.catalog_provider == "oci" ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "schemagate-genai-policy"
  description    = "Let the schemagate instance call OCI Generative AI for schema descriptions"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.dg[0].name} to use generative-ai-family in tenancy",
  ]
}

# ---------------- database (optional) ----------------
resource "oci_database_autonomous_database" "adb" {
  count                       = var.create_adb ? 1 : 0
  compartment_id              = var.compartment_ocid
  db_name                     = "schemagate"
  display_name                = "schemagate-demo"
  db_workload                 = "OLTP"
  db_version                  = "23ai"
  is_free_tier                = true
  admin_password              = var.adb_admin_password
  is_mtls_connection_required = false # TLS without a wallet
  whitelisted_ips             = [oci_core_vcn.vcn.id]
}

locals {
  # When we created the database: the walletless TLS descriptor Oracle hands
  # back, passed to python-oracledb as `dsn`. The SQLAlchemy URL stays
  # `oracle+oracledb://@` and credentials travel in connect_args, the pattern
  # SQLAlchemy documents for thin-mode Oracle. When create_adb is false,
  # database_url is used as-is.
  adb_dsn      = var.create_adb ? oci_database_autonomous_database.adb[0].connection_strings[0].profiles[0].value : ""
  database_url = var.create_adb ? "oracle+oracledb://@" : var.database_url
  connect_args = var.create_adb ? jsonencode({
    user     = "ADMIN"
    password = var.adb_admin_password
    dsn      = local.adb_dsn
  }) : "{}"

  cloud_init = templatefile("${path.module}/cloud-init.yaml", {
    database_url     = local.database_url
    connect_args     = local.connect_args
    mcp_port         = var.mcp_port
    catalog_provider = var.catalog_provider
    catalog_model    = var.catalog_model
    compartment_ocid = var.compartment_ocid
    region           = var.region
  })
}

# ---------------- instance ----------------
resource "oci_core_instance" "vm" {
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
  shape               = var.instance_shape
  display_name        = "schemagate-mcp"
  create_vnic_details {
    subnet_id        = oci_core_subnet.subnet.id
    assign_public_ip = true
    hostname_label   = "schemagate"
  }
  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ol.images[0].id
  }
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data           = base64encode(local.cloud_init)
  }
}

output "mcp_url" {
  value       = "http://${oci_core_instance.vm.public_ip}:${var.mcp_port}/mcp"
  description = "Point Claude Desktop, Cursor or any MCP client here"
}
output "ssh" {
  value = "ssh opc@${oci_core_instance.vm.public_ip}"
}
output "catalogued_with" {
  value = var.catalog_provider == "oci" ? "OCI Generative AI (${var.catalog_model}), keyless via instance principal" : "identifiers only"
}
