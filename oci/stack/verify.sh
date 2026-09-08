#!/usr/bin/env bash
#
# Certify this stack end to end, in your own tenancy, through Oracle Resource
# Manager -- the same path the "Deploy to Oracle Cloud" button takes. Nothing
# here is a simulation: it uploads the released stack zip, plans it, applies it,
# proves the MCP endpoint answers and that keyless cataloguing actually reached
# OCI Generative AI, then destroys everything it made.
#
# Run it in OCI Cloud Shell, which is already authenticated as you:
#
#     bash verify.sh                 # apply, verify, destroy
#     KEEP=1 bash verify.sh          # leave it standing; destroy by hand later
#     COMPARTMENT=ocid1.compartment.oc1..xxx bash verify.sh
#
# Cost: one Always Free VM and one Always Free Autonomous Database, alive for
# about fifteen minutes.

set -euo pipefail

ZIP_URL="${ZIP_URL:-https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip}"
COMPARTMENT="${COMPARTMENT:-${OCI_TENANCY:-}}"
REGION="${OCI_REGION:-${OCI_CLI_REGION:-}}"
KEEP="${KEEP:-0}"
WORK="$(mktemp -d)"
STACK_ID=""

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup_stack() {
  [ -n "$STACK_ID" ] || return 0
  if [ "$KEEP" = "1" ]; then
    echo "KEEP=1 -- leaving stack $STACK_ID standing."
    echo "Destroy it with:"
    echo "  oci resource-manager job create-destroy-job --stack-id $STACK_ID \\"
    echo "      --execution-plan-strategy AUTO_APPROVED --wait-for-state SUCCEEDED"
    echo "  oci resource-manager stack delete --stack-id $STACK_ID --force"
    return 0
  fi
  say "6/6  Destroying everything the stack created"
  oci resource-manager job create-destroy-job --stack-id "$STACK_ID" \
      --execution-plan-strategy AUTO_APPROVED \
      --wait-for-state SUCCEEDED --wait-for-state FAILED \
      --max-wait-seconds 1800 >/dev/null 2>&1 || echo "destroy job did not report success -- check the console"
  oci resource-manager stack delete --stack-id "$STACK_ID" --force >/dev/null 2>&1 || true
  echo "destroyed"
}
trap cleanup_stack EXIT

[ -n "$COMPARTMENT" ] || die "Set COMPARTMENT to a compartment OCID (or run in Cloud Shell, where OCI_TENANCY is set)."
[ -n "$REGION" ] || die "Set OCI_REGION."
command -v oci >/dev/null || die "The oci CLI is not on PATH. Run this in OCI Cloud Shell."

say "1/6  Preparing inputs"

# Hex gives us upper, lower and digits with no quote characters and no way to
# accidentally spell "admin", which Oracle rejects.
ADB_PW="Sg$(openssl rand -hex 6 | tr 'a-f' 'A-F')x$(openssl rand -hex 6)9"

KEY="$WORK/id_verify"
ssh-keygen -t rsa -b 2048 -N "" -f "$KEY" -q
PUBKEY="$(cat "$KEY.pub")"

# Cloud Shell's own egress address. The stack refuses 0.0.0.0/0, and this is
# the machine that has to reach both the MCP port and SSH.
MYIP="$(curl -fsS --max-time 20 https://ifconfig.me || true)"
[ -n "$MYIP" ] || die "Could not determine this shell's public IP."
CIDR="$MYIP/32"
echo "compartment : $COMPARTMENT"
echo "region      : $REGION"
echo "reaching from: $CIDR"

curl -fsSL "$ZIP_URL" -o "$WORK/stack.zip" || die "Could not download $ZIP_URL"
unzip -l "$WORK/stack.zip" | grep -q ' main.tf$' \
  || die "main.tf is not at the root of the zip -- Resource Manager will not read it."
echo "stack zip   : $(stat -c%s "$WORK/stack.zip") bytes, main.tf at root"

cat > "$WORK/vars.json" <<JSON
{
  "tenancy_ocid": "${OCI_TENANCY}",
  "compartment_ocid": "${COMPARTMENT}",
  "region": "${REGION}",
  "create_adb": "true",
  "adb_admin_password": "${ADB_PW}",
  "adb_version": "19c",
  "catalog_provider": "oci",
  "catalog_model": "google.gemini-2.5-pro",
  "ssh_public_key": "${PUBKEY}",
  "allowed_cidr": "${CIDR}",
  "ssh_cidr": "${CIDR}",
  "mcp_port": "8765"
}
JSON

say "2/6  Creating the Resource Manager stack from the released zip"
STACK_ID="$(oci resource-manager stack create \
  --compartment-id "$COMPARTMENT" \
  --config-source "$WORK/stack.zip" \
  --display-name "schemagate-verify-$(date +%s)" \
  --description "End-to-end certification of the schemagate stack" \
  --terraform-version "1.5.x" \
  --variables "file://$WORK/vars.json" \
  --query 'data.id' --raw-output)"
echo "stack: $STACK_ID"

say "3/6  Plan"
PLAN_JOB="$(oci resource-manager job create-plan-job --stack-id "$STACK_ID" \
  --query 'data.id' --raw-output)"
oci resource-manager job get --job-id "$PLAN_JOB" --wait-for-state SUCCEEDED --wait-for-state FAILED \
  --max-wait-seconds 1200 >/dev/null
PLAN_STATE="$(oci resource-manager job get --job-id "$PLAN_JOB" --query 'data."lifecycle-state"' --raw-output)"
if [ "$PLAN_STATE" != "SUCCEEDED" ]; then
  oci resource-manager job get-job-logs-content --job-id "$PLAN_JOB" | tail -60
  die "plan $PLAN_STATE"
fi
oci resource-manager job get-job-logs-content --job-id "$PLAN_JOB" | grep -E 'Plan:|Error' | tail -5 || true

say "4/6  Apply (this creates a VM and an Autonomous Database; ~10 min)"
APPLY_JOB="$(oci resource-manager job create-apply-job --stack-id "$STACK_ID" \
  --execution-plan-strategy AUTO_APPROVED \
  --query 'data.id' --raw-output)"
oci resource-manager job get --job-id "$APPLY_JOB" --wait-for-state SUCCEEDED --wait-for-state FAILED \
  --max-wait-seconds 3600 >/dev/null
APPLY_STATE="$(oci resource-manager job get --job-id "$APPLY_JOB" --query 'data."lifecycle-state"' --raw-output)"
if [ "$APPLY_STATE" != "SUCCEEDED" ]; then
  oci resource-manager job get-job-logs-content --job-id "$APPLY_JOB" | tail -80
  die "apply $APPLY_STATE"
fi

oci resource-manager job get-job-tf-state --job-id "$APPLY_JOB" --file "$WORK/state.json" >/dev/null
MCP_URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["outputs"]["mcp_url"]["value"])' "$WORK/state.json")"
IP="$(printf '%s' "$MCP_URL" | sed -E 's#http://([^:]+):.*#\1#')"
echo "mcp_url: $MCP_URL"

say "5/6  Verifying the machine, not the plan"

echo "-- waiting for the MCP port to answer (cloud-init installs Python first)"
ok=0
for i in $(seq 1 40); do
  if curl -fsS --max-time 8 -o /dev/null -w '' "http://$IP:8765/mcp" 2>/dev/null \
     || curl -sS --max-time 8 -o /dev/null -w '%{http_code}' "http://$IP:8765/mcp" 2>/dev/null | grep -qE '^[24]'; then
    ok=1; echo "   MCP endpoint answering after $((i*15))s"; break
  fi
  sleep 15
done
[ "$ok" = "1" ] || die "MCP endpoint never answered on $IP:8765"

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 opc@$IP"

echo "-- the fresh database has no user tables, so give it two to describe"
$SSH "sudo bash -lc 'set -a; . /etc/schemagate.env; set +a;
  /opt/schemagate/bin/python - <<PY
import json, os, sqlalchemy as sa
e = sa.create_engine(os.environ[\"SCHEMAGATE_DATABASE_URL\"],
                     connect_args=json.loads(os.environ[\"SCHEMAGATE_CONNECT_ARGS\"]))
with e.begin() as c:
    for ddl in [
      \"create table hr_comp (emp_id number primary key, base_amt number, bonus_amt number)\",
      \"create table cust_ord (ord_id number primary key, cust_id number, ord_dt date, tot_amt number)\",
    ]:
        try: c.exec_driver_sql(ddl)
        except Exception as ex: print(\"skip:\", str(ex)[:80])
print(\"tables ready\")
PY'" || die "could not reach the database from the instance -- the service gateway or ACL is wrong"

echo "-- re-running the keyless cataloguing and reading what it actually did"
$SSH "sudo OCI_CLI_AUTH=instance_principal /opt/catalog-once.sh" >/dev/null 2>&1 || true
LOG="$($SSH 'sudo tail -40 /var/log/schemagate-catalog.log' 2>/dev/null || true)"
printf '%s\n' "$LOG" | tail -20

CATALOG="$($SSH 'sudo cat /etc/schemagate/catalog.json' 2>/dev/null || echo '{}')"
python3 - "$CATALOG" <<'PY' || die "cataloguing did not produce descriptions -- the instance principal never reached OCI Generative AI"
import json, sys
d = json.loads(sys.argv[1]).get("describe", {})
kept = {k: v for k, v in d.items() if isinstance(v, str) and len(v.split()) >= 4}
print(f"descriptions written by OCI Generative AI: {len(kept)}")
for k, v in list(kept.items())[:4]:
    print(f"  {k}: {v[:100]}")
sys.exit(0 if kept else 1)
PY

echo
echo "CERTIFIED: plan, apply, MCP endpoint, database reachability and keyless"
echo "OCI Generative AI cataloguing all verified on a real deployment."
