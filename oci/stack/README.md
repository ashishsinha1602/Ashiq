# schemagate on OCI — Resource Manager stack

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip)

One click into your own tenancy: an Always-Free-eligible VM running the
schemagate MCP server against an Autonomous Database it creates for you, or
against a database you already have. No Marketplace listing and no partner
membership needed — the button hands Resource Manager a zip of this folder,
attached to every release. You can also `zip` it yourself and import it under
**Resource Manager → Stacks → Create Stack → My configuration**.

    mcp_url     http://<public-ip>:8765/mcp      ← Claude Desktop / Cursor / any MCP client

**The library is on PyPI:** <https://pypi.org/project/schemagate/>. This stack
is one way to run it, not the only one — the instance it builds simply does
`pip install schemagate` on first boot. If you already have a database to point
at, `pip install schemagate` is the whole install and none of the rest of this
page applies.

**In a hurry?** You do not need this. `../quickstart.sh` runs schemagate
against your existing Autonomous Database from OCI Cloud Shell in about a
minute, with no VM at all. Use the stack when you want an endpoint that stays
up for other people.

> **Certified on a live tenancy, 9 September 2026.** `verify.sh` ran to
> `6/6 CERTIFIED`: plan, apply, the MCP endpoint answering, the instance
> reaching the database, and keyless cataloguing through OCI Generative AI —
> all checked against the running machine rather than the plan.
>
> ```
> schemagate 0.1.7 serving 16 objects over streamable-http
> described 16 object(s); saved to /etc/schemagate/catalog.json
> descriptions written by OCI Generative AI: 16
>   admin.hr_comp: This holds employee base pay and bonus details, answering
>                  how much staff are paid. | salary, wages,
> ```
>
> Sixteen objects reflected out of the stack's own Autonomous Database over
> one-way TLS, with the connection descriptor resolved at boot by the instance
> principal, and every description written by Oracle's model with no API key
> and nothing leaving the tenancy.
>
> **One caveat, narrowed to what is actually unproven.** Everything above is
> certified: `verify.sh` ran to `6/6 CERTIFIED` against a stack zip built from
> `main`. (The released asset carries the same tree; it did not yet exist when
> that run happened.)
> The single exception is that on that run the MCP server was started by hand.
> `resolve-db` had written the descriptor and then deadlocked on a blocking
> `systemctl restart schemagate` — the server is ordered after it, so each
> waited for the other. `systemctl kill` on the stuck unit and the server came
> up and served. Both in-unit restarts are `--no-block` now, which removes the
> deadlock, and the fix ships in v0.1.9 — but **no run has yet gone from apply
> to serving endpoint without that one manual step**. Nothing else is in doubt.
> If you certify the unattended path before we do, please open an issue either
> way.
>
> Nine applies got here, and every one of them found something a `terraform
> plan` cannot: an Autonomous Database that refuses one-way TLS without an
> access-control list; a `LaunchInstance` 404 that really means "this
> availability domain does not offer that shape"; tenancy-scoped names built
> from the compartment, so a second run collided with the first; a verify
> script whose progress output leaked into the job state it returned; and the
> one that hid all the rest — `owner: "root:opc"` on the first `write_files`
> entry, which runs before cloud-init creates users, aborting the module and
> silently discarding every file after it. No units, nothing to serve, on every
> run until it was found.
>
> The Cloud Shell route in [`../README.md`](../README.md) needs no VM and
> remains the fastest way to try schemagate on OCI.

## Cleaning up after a run that did not finish

Every name the stack creates is unique per apply, so a leftover cannot collide
with a new run. It can still get in the way: Always Free allows only two
Autonomous Databases, and an abandoned one uses a slot. `verify.sh` says so
before it starts. To clear them:

```bash
# Autonomous Databases from earlier runs
oci db autonomous-database list --compartment-id "$OCI_TENANCY" --all \
  --query "data[?contains(\"display-name\",'schemagate')].{name:\"display-name\",state:\"lifecycle-state\",id:id}" \
  --output table
oci db autonomous-database delete --autonomous-database-id <id> --force

# Dynamic groups and policies are tenancy-scoped, not compartment-scoped
oci iam dynamic-group list --all \
  --query "data[?contains(name,'schemagate')].{name:name,id:id}" --output table
oci iam dynamic-group delete --dynamic-group-id <id> --force

oci iam policy list --compartment-id "$OCI_TENANCY" --all \
  --query "data[?contains(name,'schemagate')].{name:name,id:id}" --output table
oci iam policy delete --policy-id <id> --force

# A RESERVED public IP outlives the instance it was attached to
oci network public-ip list --compartment-id "$OCI_TENANCY" --scope REGION --all \
  --query "data[?contains(\"display-name\",'schemagate')].{name:\"display-name\",ip:\"ip-address\",id:id}" \
  --output table
oci network public-ip delete --public-ip-id <id> --force
```

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
- **`adb_version` defaults to 26ai.** Always Free 26ai is available in every
  commercial region except Bogota (BOG), Riyadh (RUH) and Singapore West (XSP);
  set `19c` if your home region is one of those. `23ai` is no longer accepted —
  it stops being valid in December 2026, so the stack does not offer a version
  that would start failing.
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
