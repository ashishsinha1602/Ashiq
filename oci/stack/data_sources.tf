# schemagate MCP server on OCI. See README.md.
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
