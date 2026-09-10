#!/usr/bin/env bash
# Verify the 0.1.10 release against a live Autonomous Database.
# Run this in OCI Cloud Shell. It is already authenticated as you.
#
#   bash verify-0110.sh
#
# It does three things:
#   1. refuses to start until 0.1.10 is actually on PyPI
#   2. runs the stack's own verify.sh (6/6), leaving the instance standing
#   3. checks the thing 6/6 does not cover: the Oracle change in 0.1.10
# then prints the teardown commands.

set -uo pipefail
# Defaults to whatever this stack declares, so the script does not go stale
# the moment the next release is cut. Override with VERSION=0.1.11 bash ...
VERSION="${VERSION:-$(curl -fsSL https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/oci/stack/schema.yaml 2>/dev/null | sed -nE 's/^version: *"([^"]+)".*/\1/p' | head -1)}"
VERSION="${VERSION:-0.1.10}"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n!! %s\n' "$*" >&2; exit 1; }

command -v oci >/dev/null || die "no oci CLI -- run this in OCI Cloud Shell."

# ---------------------------------------------------------------- 1. PyPI
say "1. Is $VERSION on PyPI?"
have="$(curl -fsS https://pypi.org/pypi/schemagate/json \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)"
echo "   PyPI has: ${have:-unknown}"
if [ "$have" != "$VERSION" ]; then
  die "PyPI is on '${have:-unknown}', not $VERSION.
   The instance installs schemagate from PyPI, so applying now would test
   the old version and prove nothing. Publish the GitHub release first:
   https://github.com/ashishsinha1602/schemagate/releases/new"
fi

# The stack pins the version it installs, and the pin lives in the zip. If the
# release zip is older than the release, the pin will not say 0.1.10.
say "2. Does the released stack zip pin $VERSION?"
tmp="$(mktemp -d)"
curl -fsSL https://github.com/ashishsinha1602/schemagate/releases/latest/download/schemagate-oci-stack.zip \
     -o "$tmp/stack.zip" || die "could not download the stack zip"
unzip -p "$tmp/stack.zip" schema.yaml 2>/dev/null | grep -m1 '^version:' || echo "   (no version line)"
unzip -p "$tmp/stack.zip" variables.tf 2>/dev/null \
  | grep -A2 'variable "schemagate_version"' | grep default || echo "   (no pin -- older zip)"

# ------------------------------------------------------------- 3. the stack
say "3. Running the stack's own verification (6/6). ~15 min."
curl -fsSL https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/oci/stack/verify.sh \
     -o "$tmp/verify.sh" || die "could not download verify.sh"
chmod +x "$tmp/verify.sh"

KEEP=1 bash "$tmp/verify.sh" 2>&1 | tee "$tmp/verify.log"
grep -q "6/6" "$tmp/verify.log" || die "verify.sh did not reach 6/6 -- see $tmp/verify.log"

STACK_ID="$(grep -m1 '^stack: ' "$tmp/verify.log" | awk '{print $2}')"
IP="$(grep -m1 '^ssh    : ' "$tmp/verify.log" | sed -E 's/.*opc@//')"
KEY="$HOME/.schemagate-verify-key"
[ -n "$IP" ] || die "could not find the instance IP in the log"

# ------------------------------------------------------- 4. the 0.1.10 change
say "4. Checking what 6/6 does not: Oracle reads column types once"
cat > "$tmp/check.py" <<'INNER'
import os, time
from sqlalchemy import create_engine, event
from schemagate import Catalog
import schemagate

url = os.environ.get("SCHEMAGATE_DATABASE_URL")
if not url:
    for line in open("/etc/schemagate.env"):
        if line.startswith("SCHEMAGATE_DATABASE_URL="):
            url = line.split("=", 1)[1].strip()

eng = create_engine(url)
hits = []

@event.listens_for(eng, "before_cursor_execute")
def _c(conn, cur, stmt, *a):
    if "all_tab_columns" in stmt:
        hits.append(stmt)

t0 = time.time()
cat = Catalog().bootstrap(eng)
elapsed = time.time() - t0
nulls = [f"{d.name}.{c.name}" for d in cat.objects() for c in d.columns
         if str(c.type).upper() in ("NULL", "NULLTYPE")]

print("version         :", schemagate.__version__)
print("objects         :", len(cat))
print("bootstrap       : %.1fs" % elapsed)
print("catalog queries :", len(hits))
print("NULL columns    :", nulls[:5] or "none")

fail = []
if schemagate.__version__ != os.environ.get("WANT", ""):
    fail.append("wrong version installed")
if len(hits) > 1:
    fail.append(f"{len(hits)} catalog queries -- the per-engine cache is not being hit")
if nulls:
    fail.append(f"{len(nulls)} column(s) still reading NULL")
print("\nRESULT:", "FAIL -- " + "; ".join(fail) if fail else "PASS")
INNER

scp -o StrictHostKeyChecking=no -i "$KEY" "$tmp/check.py" "opc@$IP:/tmp/check.py" \
  || die "could not copy the check to the instance"
ssh -o StrictHostKeyChecking=no -i "$KEY" "opc@$IP" \
  "sudo -u opc WANT=$VERSION /opt/schemagate/bin/python /tmp/check.py" \
  | tee "$tmp/check.log"

# ------------------------------------------------------------- 5. teardown
say "5. Done. Tear it down with:"
echo "   oci resource-manager job create-destroy-job --stack-id $STACK_ID --execution-plan-strategy AUTO_APPROVED"
echo "   oci resource-manager stack delete --stack-id $STACK_ID --force"
echo
grep -q "RESULT: PASS" "$tmp/check.log" \
  && echo "0.1.10 VERIFIED on a live Autonomous Database." \
  || echo "See $tmp/check.log -- the Oracle check did not pass."
echo "logs: $tmp"
