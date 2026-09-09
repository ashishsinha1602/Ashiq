# schemagate MCP server on OCI. See README.md.
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
  name           = "schemagate-mcp-dg-${local.suffix}"
  description    = "The schemagate MCP instance"
  matching_rule  = "ALL {instance.id = '${oci_core_instance.vm.id}'}"
}

resource "oci_identity_policy" "genai" {
  count          = (var.catalog_provider == "oci" || var.create_adb) ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "schemagate-genai-policy-${local.suffix}"
  description    = "Let the schemagate instance read its database's descriptor and call OCI Generative AI"
  statements = compact([
    var.catalog_provider == "oci" ? "Allow dynamic-group ${oci_identity_dynamic_group.dg[0].name} to use generative-ai-family in compartment id ${var.compartment_ocid}" : "",
    # /opt/resolve-db.sh reads the connection descriptor at boot, because the
    # database is created after this instance (its ACL names the instance's IP).
    var.create_adb ? "Allow dynamic-group ${oci_identity_dynamic_group.dg[0].name} to read autonomous-database-family in compartment id ${var.compartment_ocid}" : "",
  ])
}
