# schemagate MCP server on OCI. See README.md.
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

  # Unique per apply, not per compartment. These three names are the only
  # ones in the stack that must not collide with anything already in the
  # tenancy: a dynamic group and a policy are tenancy-scoped, and an
  # Autonomous Database name has to be unique across the whole region.
  # Deriving them from the compartment made them stable across runs, so a
  # single leftover from a previous run -- a destroy that did not finish, an
  # apply someone interrupted -- failed every later apply with
  # "DynamicResourceGroup with the same displayName already exists" and
  # "a database named sg... already exists". The VCN is created first and its
  # OCID is unique to this apply, so everything downstream can key off it.
  suffix           = substr(md5(oci_core_vcn.vcn.id), 0, 8)
  adb_display_name = "schemagate-demo-${local.suffix}"

  # The database this stack creates cannot be referenced here: its ACL names
  # the instance's reserved IP, so it is created after the instance. cloud-init
  # resolves the descriptor at boot instead (see /opt/resolve-db.sh). When
  # create_adb is false, database_url is used as-is and nothing is resolved.
  database_url = var.create_adb ? "oracle+oracledb://@" : var.database_url
  connect_args = var.create_adb ? "{}" : "{}"

  cloud_init = templatefile("${path.module}/cloud-init.yaml", {
    database_url       = local.database_url
    connect_args       = local.connect_args
    mcp_port           = var.mcp_port
    catalog_provider   = var.catalog_provider
    catalog_model      = var.catalog_model
    compartment_ocid   = var.compartment_ocid
    region             = var.region
    create_adb         = var.create_adb ? "true" : "false"
    adb_display_name   = local.adb_display_name
    adb_admin_password = var.adb_admin_password
    schemagate_version = var.schemagate_version
  })
}
