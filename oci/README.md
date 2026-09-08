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

## 2. Full route — `stack/`, a Resource Manager stack

For a shared, always-on **MCP endpoint** your team and Claude Desktop can point
at: an Always-Free-eligible VM running `python -m schemagate.mcp_server` against
an Autonomous Database it creates, or one you already have. This is also the
artifact for an OCI Marketplace "stack" listing; it works standalone by zipping
the folder and importing it into Resource Manager.

Use the fast route to try it. Use the stack when you want it running for others.

## Certification

The Oracle dialect and the native `VECTOR` store are certified live on Oracle AI
Database 26ai — `scripts/certify_dialect.py` and `SCHEMAGATE_ORACLE_DSN` in
TESTING.md.
