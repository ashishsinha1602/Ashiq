# schemagate MCP server on OCI. See README.md.
# ---------------- instance ----------------
resource "oci_core_instance" "vm" {
  compartment_id = var.compartment_ocid

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
