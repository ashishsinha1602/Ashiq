# schemagate MCP server on OCI. See README.md.
output "mcp_url" {
  value       = "http://${oci_core_public_ip.mcp.ip_address}:${var.mcp_port}/mcp"
  description = "Point Claude Desktop, Cursor or any MCP client here"
}
output "ssh" {
  value = "ssh opc@${oci_core_public_ip.mcp.ip_address}"
}
output "next_step" {
  value = "pip install schemagate  •  https://pypi.org/project/schemagate/"
}
output "catalogued_with" {
  value = var.catalog_provider == "oci" ? "OCI Generative AI (${var.catalog_model}), keyless via instance principal" : "identifiers only"
}
