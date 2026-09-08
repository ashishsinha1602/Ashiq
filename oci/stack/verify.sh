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
#
# Note on error handling: `set -e` is deliberately not used, and no OCI call is
# wrapped in `--wait-for-state`. `oci resource-manager job get` does not accept
# that flag, and the non-zero exit it returned took the whole script down
# through its own EXIT trap before any diagnostic could print -- a failure that
# looked exactly like the plan silently vanishing. Every wait below is an
# explicit poll that prints the state it sees.

ZIP_URL="${ZIP_URL:-https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip}"
COMPARTMENT="${COMPARTMENT:-${OCI_TENANCY:-}}"
REGION="${OCI_REGION:-${OCI_CLI_REGION:-}}"
KEEP="${KEEP:-0}"
WORK="$(mktemp -d)"
STACK_ID=""
APPLIED=0

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; teardown; exit 1; }
# The job log comes back as JSON with literal \n escapes, so it is one enormous
# line until they are turned back into newlines. Splitting on commas instead
# printed the whole plan and buried the error.
errs() { oci resource-manager job get-job-logs-content --job-id "$1" \
           | python3 -c 'import sys; sys.stdout.write(sys.stdin.read().replace("\\n", "\n"))' \
           | grep -i -E '\[error\]|error:|Suggestion:|Request Target:' | tail -30; }

# Poll until the job leaves the running states. Echoes the final state.
wait_job() {
  local job="$1" limit="$2" st=""
  for _ in $(seq 1 "$limit"); do
    st="$(oci resource-manager job get --job-id "$job" \
            --query 'data."lifecycle-state"' --raw-output 2>/dev/null)"
    case "$st" in SUCCEEDED|FAILED|CANCELED) break ;; esac
    # >&2: this runs inside ST="$(wait_job ...)", so anything on stdout is
    # captured into the caller's variable alongside the state.
    printf '   %s\r' "${st:-...}" >&2
    sleep 15
  done
  echo "$st"
}

teardown() {
  [ -n "$STACK_ID" ] || return 0
  if [ "$KEEP" = "1" ]; then
    echo
    echo "KEEP=1 -- stack left standing: $STACK_ID"
    echo "Destroy it with:"
    echo "  oci resource-manager job create-destroy-job --stack-id $STACK_ID --execution-plan-strategy AUTO_APPROVED"
    echo "  oci resource-manager stack delete --stack-id $STACK_ID --force"
    return 0
  fi
  say "Destroying everything the stack created"
  local d
  d="$(oci resource-manager job create-destroy-job --stack-id "$STACK_ID" \
        --execution-plan-strategy AUTO_APPROVED --query 'data.id' --raw-output 2>/dev/null)"
  if [ -n "$d" ]; then
    local st; st="$(wait_job "$d" 120)"
    echo "destroy: $st"
    [ "$st" = "SUCCEEDED" ] || errs "$d"
  fi
  oci resource-manager stack delete --stack-id "$STACK_ID" --force >/dev/null 2>&1
  echo "stack deleted"
}

[ -n "$COMPARTMENT" ] || { echo "Set COMPARTMENT to a compartment OCID, or run this in OCI Cloud Shell." >&2; exit 1; }
[ -n "$REGION" ]      || { echo "Set OCI_REGION." >&2; exit 1; }
command -v oci >/dev/null || { echo "The oci CLI is not on PATH. Run this in OCI Cloud Shell." >&2; exit 1; }

say "1/6  Preparing inputs"
# Hex gives upper, lower and digits with no quote characters and no way to
# accidentally spell "admin", which Oracle rejects.
ADB_PW="Sg$(openssl rand -hex 6 | tr 'a-f' 'A-F')x$(openssl rand -hex 6)9"
KEY="$WORK/id_verify"
ssh-keygen -t rsa -b 2048 -N "" -f "$KEY" -q || die "ssh-keygen"
PUBKEY="$(cat "$KEY.pub")"

MYIP="$(curl -fsS --max-time 20 https://ifconfig.me)"
[ -n "$MYIP" ] || die "could not determine this shell's public IP"
CIDR="$MYIP/32"
echo "compartment  : $COMPARTMENT"
echo "region       : $REGION"
echo "reaching from: $CIDR"

curl -fsSL "$ZIP_URL" -o "$WORK/stack.zip" || die "could not download $ZIP_URL"
unzip -l "$WORK/stack.zip" | grep -q ' main.tf$' \
  || die "main.tf is not at the root of the zip -- Resource Manager will not read it"
echo "stack zip    : $(stat -c%s "$WORK/stack.zip") bytes, main.tf at root"

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
[ -n "$STACK_ID" ] || die "stack create returned no id"
echo "stack: $STACK_ID"

say "3/6  Plan"
PLAN_JOB="$(oci resource-manager job create-plan-job --stack-id "$STACK_ID" \
  --query 'data.id' --raw-output)"
[ -n "$PLAN_JOB" ] || die "plan job create returned no id"
ST="$(wait_job "$PLAN_JOB" 80)"
echo "plan: $ST"
[ "$ST" = "SUCCEEDED" ] || { errs "$PLAN_JOB"; die "plan $ST"; }
oci resource-manager job get-job-logs-content --job-id "$PLAN_JOB" \
  | tr ',' '\n' | grep -E 'Plan: [0-9]' | tail -1

say "4/6  Apply -- creates a VM and an Autonomous Database, ~10 min"
APPLY_JOB="$(oci resource-manager job create-apply-job --stack-id "$STACK_ID" \
  --execution-plan-strategy AUTO_APPROVED --query 'data.id' --raw-output)"
[ -n "$APPLY_JOB" ] || die "apply job create returned no id"
ST="$(wait_job "$APPLY_JOB" 160)"
echo "apply: $ST"
[ "$ST" = "SUCCEEDED" ] || { errs "$APPLY_JOB"; die "apply $ST"; }
APPLIED=1

oci resource-manager job get-job-tf-state --job-id "$APPLY_JOB" --file "$WORK/state.json" >/dev/null
MCP_URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["outputs"]["mcp_url"]["value"])' "$WORK/state.json")"
IP="$(printf '%s' "$MCP_URL" | sed -E 's#http://([^:]+):.*#\1#')"
echo "mcp_url: $MCP_URL"

say "5/6  Verifying the machine, not the plan"
echo "-- waiting for the MCP port (cloud-init installs Python first)"
ok=0
for i in $(seq 1 40); do
  code="$(curl -sS --max-time 8 -o /dev/null -w '%{http_code}' "http://$IP:8765/mcp" 2>/dev/null)"
  case "$code" in 2*|4*) ok=1; echo "   answering after $((i*15))s (HTTP $code)"; break ;; esac
  sleep 15
done
[ "$ok" = "1" ] || die "MCP endpoint never answered on $IP:8765"

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 opc@$IP"

echo "-- the fresh database has no user tables, so give it two to describe"
$SSH "sudo bash -lc 'set -a; . /etc/schemagate.env; set +a; /opt/schemagate/bin/python -c \"
import json, os, sqlalchemy as sa
e = sa.create_engine(os.environ[\\\"SCHEMAGATE_DATABASE_URL\\\"], connect_args=json.loads(os.environ[\\\"SCHEMAGATE_CONNECT_ARGS\\\"]))
c = e.connect()
for d in [\\\"create table hr_comp (emp_id number primary key, base_amt number, bonus_amt number)\\\",
          \\\"create table cust_ord (ord_id number primary key, cust_id number, ord_dt date, tot_amt number)\\\"]:
    try: c.exec_driver_sql(d)
    except Exception as ex: print(\\\"skip:\\\", str(ex)[:70])
c.commit(); print(\\\"tables ready\\\")\"'" \
  || die "the instance could not reach the database -- routing or the ACL is wrong"

echo "-- re-running the keyless cataloguing and reading what it actually did"
$SSH "sudo OCI_CLI_AUTH=instance_principal /opt/catalog-once.sh" >/dev/null 2>&1
$SSH "sudo tail -20 /var/log/schemagate-catalog.log" 2>/dev/null

$SSH "sudo cat /etc/schemagate/catalog.json" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin).get("describe", {})
kept = {k: v for k, v in d.items() if isinstance(v, str) and len(v.split()) >= 4}
print(f"descriptions written by OCI Generative AI: {len(kept)}")
for k, v in list(kept.items())[:4]:
    print(f"  {k}: {v[:100]}")
sys.exit(0 if kept else 1)
' || die "cataloguing produced no descriptions -- the instance principal never reached OCI Generative AI"

say "6/6  CERTIFIED"
echo "plan, apply, MCP endpoint, database reachability and keyless OCI"
echo "Generative AI cataloguing all verified on a real deployment."
teardown
