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

> **The apply now succeeds; the endpoint has not yet answered.** Three live
> runs in a real tenancy each found a different bug, and each is fixed and
> pinned by a test: the database rejected one-way TLS with no access-control
> list (the instance's public IP is now reserved up front, and cloud-init
> resolves the connection descriptor at boot through the instance principal);
> `LaunchInstance` returned 404 because the first availability domain does not
> necessarily offer the shape (the stack now asks each one); and `verify.sh`
> tore down a good plan because its progress output leaked into the job state
> it returned.
>
> The fourth run applied all ten resources. It then failed the endpoint check:
> the MCP port had not answered within ten minutes. Two causes, both fixed
> here — the database lookup ran *after* the pip install rather than
> alongside it, so the two waits were added together instead of overlapped,
> and the check itself gave up too early. **That fix has not been applied
> against a live tenancy yet** — run the certification below and please open
> an issue if it fails.
>
> Expect the first boot to take about as long as Oracle takes to provision the
> Autonomous Database, which is most of ten minutes. The instance installs
> Python and schemagate underneath that wait, not after it.
>
> The Cloud Shell route in [`../README.md`](../README.md) *is* exercised,
> needs no VM, and is the supported way to run schemagate on OCI today.

## Certify it yourself

`verify.sh` runs the whole thing in your own tenancy through Resource Manager,
which is the same path the Deploy button takes, and then checks the machine
rather than the plan: that the MCP port answers, that the instance can actually
reach the database through the service gateway, and that cataloguing really
called OCI Generative AI through the instance principal rather than failing
quietly. It destroys everything it created afterwards.

```bash
# in OCI Cloud Shell, which is already authenticated as you
curl -fsSLO https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/oci/stack/verify.sh
bash verify.sh                      # apply, verify, destroy  (~15 min)
KEEP=1 bash verify.sh               # leave it standing
COMPARTMENT=ocid1.compartment... bash verify.sh
```

It generates its own SSH key and ADMIN password, and narrows `allowed_cidr`
and `ssh_cidr` to the shell's own address, so nothing is typed and nothing is
left open.

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
- **The database this stack creates is reachable from the internet.** An
  access-control list naming the VCN needs a service gateway, and OCI rejects
  a route table holding both a service gateway for all services and the
  internet gateway the instance needs to install anything. Naming the
  instance's public IP instead is circular — cloud-init already carries the
  database's connection descriptor, so the instance depends on the database.
  So the demo database is protected by TLS and the password you set, and
  nothing else. It is created empty and destroyed with the stack. Set
  `adb_allowed_cidrs` to narrow it, or point `create_adb = false` at your own
  database for anything real.

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
