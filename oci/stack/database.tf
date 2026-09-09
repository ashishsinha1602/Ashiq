# schemagate MCP server on OCI. See README.md.
# ---------------- database (optional) ----------------
resource "oci_database_autonomous_database" "adb" {
  count = var.create_adb ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.adb_admin_password != ""
      error_message = "create_adb is true, so adb_admin_password is required."
    }
  }

  compartment_id              = var.compartment_ocid
  db_name                     = "sg${local.suffix}"
  display_name                = local.adb_display_name
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
