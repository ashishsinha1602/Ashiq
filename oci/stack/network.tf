# schemagate MCP server on OCI. See README.md.
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
