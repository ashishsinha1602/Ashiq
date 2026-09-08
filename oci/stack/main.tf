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

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# Always Free shapes are not offered in every availability domain -- in a live
# Phoenix tenancy VM.Standard.E2.1.Micro existed only in AD-3, and asking AD-1
# for it returned 404-NotAuthorizedOrNotFound at LaunchInstance. So ask each
# domain what it actually has rather than assuming the first one.
data "oci_core_shapes" "by_ad" {
  count               = length(data.oci_identity_availability_domains.ads.availability_domains)
  compartment_id      = var.compartment_ocid
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[count.index].name
}

data "oci_core_images" "ol" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
  shape                    = var.instance_shape
  state                    = "AVAILABLE"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"

  lifecycle {
    postcondition {
      condition     = length(self.images) > 0
      error_message = "No Oracle Linux 9 image for shape ${var.instance_shape} in ${var.region}. Pick another shape or region."
    }
  }
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

  # One default route out through the internet gateway, and nothing else.
  #
  # 0.1.4 added a service gateway here so the database's access-control list
  # could name this VCN -- Oracle only honours a VCN entry when traffic
  # arrives through one. A live apply rejected it: "Internet Gateway target
  # cannot be used together with Service Gateway target for All Services in
  # the same routing table". The two are mutually exclusive in one table, and
  # the instance needs the internet gateway to install anything at all, so the
  # service gateway had to go -- and with it the VCN-scoped ACL. See the
  # database resource below.
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
    source   = var.ssh_cidr
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
data "oci_core_vnic_attachments" "vm" {
  compartment_id = var.compartment_ocid
  instance_id    = oci_core_instance.vm.id
}

data "oci_core_private_ips" "vm" {
  vnic_id = data.oci_core_vnic_attachments.vm.vnic_attachments[0].vnic_id
}

# Reserved rather than ephemeral: the database's ACL has to name this address,
# and an ephemeral address does not exist until the instance is running.
resource "oci_core_public_ip" "mcp" {
  compartment_id = var.compartment_ocid
  display_name   = "schemagate-mcp-ip"
  lifetime       = "RESERVED"
  private_ip_id  = data.oci_core_private_ips.vm.private_ips[0].id
}

# Needed for keyless cataloguing, and also for the boot-time database lookup
# when this stack creates the database -- so it exists for either reason.
resource "oci_identity_dynamic_group" "dg" {
  count          = (var.catalog_provider == "oci" || var.create_adb) ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "schemagate-mcp-dg-${substr(md5(var.compartment_ocid), 0, 8)}"
  description    = "The schemagate MCP instance"
  matching_rule  = "ALL {instance.id = '${oci_core_instance.vm.id}'}"
}

resource "oci_identity_policy" "genai" {
  count          = (var.catalog_provider == "oci" || var.create_adb) ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "schemagate-genai-policy-${substr(md5(var.compartment_ocid), 0, 8)}"
  description    = "Let the schemagate instance read its database's descriptor and call OCI Generative AI"
  statements = compact([
    var.catalog_provider == "oci" ? "Allow dynamic-group ${oci_identity_dynamic_group.dg[0].name} to use generative-ai-family in compartment id ${var.compartment_ocid}" : "",
    # /opt/resolve-db.sh reads the connection descriptor at boot, because the
    # database is created after this instance (its ACL names the instance's IP).
    var.create_adb ? "Allow dynamic-group ${oci_identity_dynamic_group.dg[0].name} to read autonomous-database-family in compartment id ${var.compartment_ocid}" : "",
  ])
}

# ---------------- database (optional) ----------------
resource "oci_database_autonomous_database" "adb" {
  count                       = var.create_adb ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.adb_admin_password != ""
      error_message = "create_adb is true, so adb_admin_password is required."
    }
  }

  compartment_id              = var.compartment_ocid
  db_name                     = "sg${substr(md5(var.compartment_ocid), 0, 8)}"
  display_name                = "schemagate-demo"
  db_workload                 = "OLTP"
  db_version                  = var.adb_version
  is_free_tier                = true
  admin_password              = var.adb_admin_password
  is_mtls_connection_required = false # TLS without a wallet

  # An access-control list naming this VCN needs a service gateway, which
  # cannot coexist with the internet gateway the instance requires (see the
  # route table above). Naming the instance's public IP instead is circular:
  # the instance's cloud-init carries the database's connection descriptor, so
  # the instance already depends on the database.
  #
  # So the demo database is reachable from the internet, protected by TLS and
  # the password you supply -- which the console form and this stack both
  # require to be long and mixed-case. That is acceptable for a database this
  # stack creates and destroys with nothing in it, and it is the reason the
  # README tells you to point `create_adb = false` at your own database for
  # anything real. `adb_allowed_cidrs` narrows it if you know your egress
  # addresses; leave it empty and the instance can always reach the database.
  # Oracle refuses one-way TLS (mTLS off) on a public database with no ACL:
  # "One-way TLS connections require a private endpoint or a public IP with an
  # ACL". 0.1.7 set this to null and every apply failed here.
  #
  # The list has to name the instance's public IP, so the address is reserved
  # up front and attached to the instance afterwards -- that is why this
  # database is created *after* the instance, and why cloud-init looks its
  # connection descriptor up at boot rather than receiving it from Terraform.
  whitelisted_ips = concat([oci_core_public_ip.mcp.ip_address], var.adb_allowed_cidrs)
}

locals {
  # The availability domains that offer the requested shape, in order.
  ads_with_shape = [
    for i, ad in data.oci_identity_availability_domains.ads.availability_domains :
    ad.name
    if contains([for sh in data.oci_core_shapes.by_ad[i].shapes : sh.name], var.instance_shape)
  ]
  availability_domain = (
    var.availability_domain != "" ? var.availability_domain :
    length(local.ads_with_shape) > 0 ? local.ads_with_shape[0] :
    data.oci_identity_availability_domains.ads.availability_domains[0].name
  )

  # The database this stack creates cannot be referenced here: its ACL names
  # the instance's reserved IP, so it is created after the instance. cloud-init
  # resolves the descriptor at boot instead (see /opt/resolve-db.sh). When
  # create_adb is false, database_url is used as-is and nothing is resolved.
  database_url = var.create_adb ? "oracle+oracledb://@" : var.database_url
  connect_args = var.create_adb ? "{}" : "{}"

  cloud_init = templatefile("${path.module}/cloud-init.yaml", {
    database_url      = local.database_url
    connect_args      = local.connect_args
    mcp_port          = var.mcp_port
    catalog_provider  = var.catalog_provider
    catalog_model     = var.catalog_model
    compartment_ocid  = var.compartment_ocid
    region            = var.region
    create_adb        = var.create_adb ? "true" : "false"
    adb_display_name  = "schemagate-demo"
    adb_admin_password = var.adb_admin_password
  })
}

# ---------------- instance ----------------
resource "oci_core_instance" "vm" {
  compartment_id      = var.compartment_ocid

  lifecycle {
    precondition {
      condition     = var.create_adb || var.database_url != ""
      error_message = "create_adb is false, so database_url is required - there is nothing for the MCP server to reflect."
    }
    precondition {
      condition     = var.availability_domain != "" || length(local.ads_with_shape) > 0
      error_message = "No availability domain in ${var.region} offers ${var.instance_shape}. Always Free shapes are often in only one domain - pick another shape, or set availability_domain explicitly."
    }
  }

  availability_domain = local.availability_domain
  shape               = var.instance_shape
  display_name        = "schemagate-mcp"

  # Every Flex shape requires shape_config at the API, and A1.Flex is the other
  # Always Free option, so a user will pick one. Omitting it returns a 400.
  dynamic "shape_config" {
    for_each = length(regexall("Flex", var.instance_shape)) > 0 ? [1] : []
    content {
      ocpus         = var.instance_ocpus
      memory_in_gbs = var.instance_memory_gbs
    }
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.subnet.id
    assign_public_ip = false # the reserved IP below is attached instead
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
  value       = "http://${oci_core_public_ip.mcp.ip_address}:${var.mcp_port}/mcp"
  description = "Point Claude Desktop, Cursor or any MCP client here"
}
output "ssh" {
  value = "ssh opc@${oci_core_public_ip.mcp.ip_address}"
}
output "catalogued_with" {
  value = var.catalog_provider == "oci" ? "OCI Generative AI (${var.catalog_model}), keyless via instance principal" : "identifiers only"
}
