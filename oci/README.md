# schemagate on Oracle Cloud

Two ways in. Start with the fast one.

## 1. Fast route — 60 seconds, no VM, no Terraform

Open **OCI Cloud Shell** (the `>_` icon in the console header). It already has
Python and your OCI identity, so nothing needs configuring:

    pip install --user 'schemagate[oracle,oci]'
    export SCHEMAGATE_CONNECT_ARGS='{"user":"ADMIN","password":"...","dsn":"<your_high_alias>"}'
    schemagate describe --url 'oracle+oracledb://@' --provider oci \
        --model google.gemini-2.5-pro --config catalog.json
    schemagate select --url 'oracle+oracledb://@' --config catalog.json \
        "which customers still owe us money" --prompt

That catalogs your Autonomous Database with OCI Generative AI — no API key, the
prompt never leaves your tenancy — and prints the exact table set a text-to-SQL
model should see. `quickstart.sh` in this folder runs the same thing.

Wallet users: unzip it and add `"config_dir"`, `"wallet_location"` and
`"wallet_password"` to `SCHEMAGATE_CONNECT_ARGS`.

## 2. One click — `stack/`, a Resource Manager stack

For a shared, always-on **MCP endpoint** your team and its MCP clients can point
at: an Always-Free-eligible VM running `python -m schemagate.mcp_server` against
an Autonomous Database it creates, or one you already have.

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip)

That opens Resource Manager in your own tenancy with the stack loaded. Nothing
is installed on your machine, and there is no Marketplace listing or partner
membership in the way — the button just hands OCI a zip built from this folder
and attached to every release.

Prefer to do it by hand? `zip` this folder and import it under
**Developer Services → Resource Manager → Stacks → Create Stack → My
configuration**.

Use the fast route to try it. Use the stack when you want it running for others.

The stack needs a couple of decisions the fast route does not: it is home-region
only, `catalog_provider = "oci"` needs tenancy-admin rights, and you must supply
CIDRs for the MCP endpoint and for SSH — there are no defaults, because the
endpoint has no auth of its own. [`stack/README.md`](stack/README.md) covers all
of it. It has not been applied end to end yet; the Cloud Shell route has.

## Certification

The Oracle dialect and the native `VECTOR` store are certified live on Oracle AI
Database 26ai — `scripts/certify_dialect.py` and `SCHEMAGATE_ORACLE_DSN` in
TESTING.md.
