# schemagate on OCI — Resource Manager stack

One click from OCI Marketplace (or `zip` this folder and import it into
Resource Manager): an Always-Free-eligible VM running the schemagate MCP
server against an Autonomous Database it creates for you, or against a
database you already have.

    mcp_url     http://<public-ip>:8765/mcp      ← Claude Desktop / Cursor / any MCP client

**In a hurry?** You do not need this. `../quickstart.sh` runs schemagate
against your existing Autonomous Database from OCI Cloud Shell in about a
minute, with no VM at all. Use the stack when you want an endpoint that stays
up for other people.

## What it creates

VCN, subnet, internet gateway, security list, the VM, optionally an Always Free
Autonomous Database, and — when cataloguing is left on — a dynamic group and a
policy allowing that one instance to call OCI Generative AI. Destroying the
stack removes all of it.

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
