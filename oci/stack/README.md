# schemagate on OCI — Resource Manager stack

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip)

One click into your own tenancy: an Always-Free-eligible VM running the
schemagate MCP server against an Autonomous Database it creates for you, or
against a database you already have. No Marketplace listing and no partner
membership needed — the button hands Resource Manager a zip of this folder,
attached to every release. You can also `zip` it yourself and import it under
**Resource Manager → Stacks → Create Stack → My configuration**.

    mcp_url     http://<public-ip>:8765/mcp      ← Claude Desktop / Cursor / any MCP client

**In a hurry?** You do not need this. `../quickstart.sh` runs schemagate
against your existing Autonomous Database from OCI Cloud Shell in about a
minute, with no VM at all. Use the stack when you want an endpoint that stays
up for other people.

> **Not yet applied end to end.** The Terraform is reviewed and cross-checked
> against the OCI provider docs, but nobody has run `terraform apply` on it
> yet. If you deploy it and something fails, please open an issue — that is
> the fastest way to get it fixed. The Cloud Shell route in
> [`../README.md`](../README.md) *is* exercised and needs no VM.

## Before you deploy

- **Home region only.** Always Free Autonomous Database and the
  `VM.Standard.E2.1.Micro` shape exist only in your tenancy's home region, and
  the free ADB is limited to two per tenancy.
- **`adb_version` defaults to 19c.** Always Free offers 19c everywhere; 26ai
  and 23ai only in a handful of regions.
- **Cataloguing needs tenancy-admin.** `catalog_provider = "oci"` creates a
  dynamic group and a policy at the tenancy root, which only a tenancy
  administrator can do. Set it to `none` if you are not one — selection still
  works, just on identifiers alone rather than descriptions.
- **`allowed_cidr` and `ssh_cidr` have no defaults, deliberately.** The MCP
  endpoint has no authentication of its own; identity comes from the
  `principal` each call passes. Opening it to `0.0.0.0/0` would let anyone
  claim any identity and read your schema, so the stack refuses that value.
- **The ADMIN password reaches the VM through instance metadata**, which any
  local process on that VM can read at `169.254.169.254`. It is also in
  Terraform state. Treat the demo database as a demo database.
- **An idle Always Free database stops after 7 days** and can be reclaimed
  after 90 days idle. Fine for a trial, not for something you rely on.

## What it creates

VCN, subnet, internet gateway, **service gateway**, route table, security list,
the VM, optionally an Always Free Autonomous Database, and — when cataloguing is
left on — a dynamic group and a policy allowing that one instance to call OCI
Generative AI. Destroying the stack removes all of it.

The service gateway is not optional decoration: the database's access-control
list admits the VCN, and Oracle only honours a VCN entry when the traffic
arrives through a service gateway. Without it the database would refuse the
VM's connections.

## Cataloguing, keylessly

On first boot the instance principal calls OCI Generative AI (default
`google.gemini-2.5-pro`) to write a one-sentence description of every table and
view. No API key is stored anywhere, and the prompts — schema metadata only,
never rows — stay inside your tenancy. It roughly doubles retrieval accuracy on
questions phrased in business language rather than column names. Set
**Write table descriptions with** to `none` to skip it.

Re-run it any time:

    sudo -u opc OCI_CLI_AUTH=instance_principal /opt/catalog-once.sh

## Using it

Claude Desktop config:

    {"mcpServers": {"schemagate": {"url": "http://<public-ip>:8765/mcp"}}}

The `health` MCP tool reports reflection state; call it from your client.

Restrictions, hints and descriptions live in `/etc/schemagate/catalog.json` on
the VM (`ssh opc@<ip>`); the service restarts on change:
`sudo systemctl restart schemagate`. Narrow `allowed_cidr` before pointing
anything real at it — the endpoint has no auth of its own; identity comes from
the `principal` each call passes.
