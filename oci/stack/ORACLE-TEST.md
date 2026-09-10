# Verifying 0.1.10 against a live Autonomous Database

Run everything here in **OCI Cloud Shell**. It is already authenticated as you.

## Order matters

The instance installs schemagate from PyPI, unpinned:

```
/opt/schemagate/bin/pip install ... "schemagate[$extras]"
```

So **release 0.1.10 to PyPI first**. Applying the stack before that installs
0.1.9 and tests the code this release changed *nothing* about.

Wait until this prints `0.1.10`:

```bash
curl -s https://pypi.org/pypi/schemagate/json | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])'
```

## 1. The stack itself — 6/6

Unchanged from 0.1.9. One command, applies and destroys by itself:

```bash
curl -fsSL https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/oci/stack/verify.sh -o verify.sh
chmod +x verify.sh
./verify.sh
```

This is the one that matters most for this release, because the Terraform was
split into per-concern files after 0.1.9 and **has never been through a live
apply**. Static checks all pass (validate, fmt, 37/37 blocks identical in body,
zip layout, variable validation), but only an apply proves the split.

`6/6 CERTIFIED` means the stack is good.

## 2. The Oracle change — what 6/6 does *not* cover

6/6 certifies the stack boots and catalogues. It does not check the thing
0.1.10 actually changed: Oracle now reads column types **once per engine**
instead of once per table.

To keep the instance alive for this, run the verify with no teardown:

```bash
KEEP=1 ./verify.sh
```

It still runs all 6 checks; it just leaves the stack standing at the end and
prints the stack id and the destroy commands.

Then SSH in — verify.sh prints the exact line, key at `~/.schemagate-verify-key`:

```bash
ssh -i ~/.schemagate-verify-key opc@<IP>
```

On the instance:

```bash
sudo -u opc /opt/schemagate/bin/python - <<'PY'
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
el = time.time() - t0

nulls = [f"{d.name}.{c.name}" for d in cat.objects() for c in d.columns
         if str(c.type).upper() in ("NULL", "NULLTYPE")]

print("schemagate      :", schemagate.__version__)
print("objects         :", len(cat))
print("bootstrap       : %.1fs" % el)
print("all_tab_columns : %d queries" % len(hits), "<-- must be 0 or 1, never one per table")
print("NULL columns    :", nulls[:5] or "none")

sel = cat.select("which customers owe us money", top_k=6)
print("select          :", [d.name for d in sel.objects])
PY
```

**Pass:** version is `0.1.10`, `all_tab_columns` is `0` or `1` (never one per
table), and `NULL columns` is `none`.

`0` is a pass, not a miss: the query only runs when some column reflects as
NULL, and a schema with no XMLTYPE, JSON, SDO_GEOMETRY, object type or VECTOR
column has nothing to correct.

**Fail:** a query count near the object count means the per-engine cache is not
being hit. Any `NULL` column means a type SQLAlchemy could not render did not
get its real name, and the model would see `NULL` where a type belongs.

## 3. Tear down

```bash
oci resource-manager job create-destroy-job --stack-id <STACK_ID> --execution-plan-strategy AUTO_APPROVED
oci resource-manager stack delete --stack-id <STACK_ID> --force
```

verify.sh prints both lines with the id filled in if it exits before destroying.
