"""Dump the bundled schemas as JSON for the Studio and the parity test."""
import json, os, sqlite3, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
import sqlalchemy as sa
from ashiq import Catalog
from ashiq.demo_schema import DDL as C_DDL, HINTS as C_HINTS, GOLDEN as C_GOLDEN, GOLDEN_PARAPHRASE
import schema_fixture_health as H, schema_fixture_warehouse as W, schema_fixture_complex as X
import schema_fixture_finance as Fn, schema_fixture_telemetry as Tl

def sqlite_url(ddl):
    p = tempfile.mktemp(suffix=".db"); c = sqlite3.connect(p); c.executescript(ddl); c.commit(); c.close()
    return f"sqlite:///{p}"

def complex_engine():
    base = tempfile.mkdtemp(); eng = sa.create_engine("sqlite:///" + os.path.join(base, "m.db"))
    @sa.event.listens_for(eng, "connect")
    def _a(c, _):
        for s in X.ATTACHED_SCHEMAS: c.execute(f"ATTACH DATABASE '{os.path.join(base, s)}.db' AS {s}")
    with eng.begin() as c:
        for st in X.DDL.split(";\n"):
            if st.strip(): c.exec_driver_sql(st)
    return eng

def doc_json(d):
    return {"name": d.name, "schema": d.schema, "kind": d.kind, "description": d.description,
            "hint": d.hint, "definition": d.definition, "roles": d.roles,
            "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable, "comment": c.comment, "pk": c.pk} for c in d.columns],
            "foreign_keys": [{"columns": fk.columns, "ref_table": fk.ref_table, "ref_columns": fk.ref_columns} for fk in d.foreign_keys]}

SCHEMAS = {
    "commerce":  dict(title="Commerce", blurb="42 objects. Orders, billing, inventory, HR. A decoy invoice_draft table and a restricted hr_compensation.",
                      url=sqlite_url(C_DDL), hints=C_HINTS, restrict={"hr_compensation": ["payroll"]},
                      questions=[q for q, _ in C_GOLDEN] + [q for q, _ in GOLDEN_PARAPHRASE],
                      golden={q: sorted(g) for q, g in C_GOLDEN + GOLDEN_PARAPHRASE}),
    "clinical":  dict(title="Clinical claims", blurb="27 objects. Encounters, claims, pharmacy, quality measures. Code-lookup tables one join away from the facts.",
                      url=sqlite_url(H.DDL), hints=H.HINTS, restrict={"mbr_eligibility": ["benefits"]},
                      questions=[q for q, _ in H.GOLDEN], golden={q: sorted(g) for q, g in H.GOLDEN}),
    "warehouse": dict(title="Claims warehouse (star schema)", blurb="51 objects. Four fact grains, SCD2 member history, role-playing dates, bridges, and 15 backup/staging copies.",
                      url=sqlite_url(W.DDL), hints=W.HINTS, restrict=W.RESTRICTED,
                      questions=[q for q, _ in W.GOLDEN] + [q for q, _ in W.GOLDEN_NEEDS_DESCRIPTIONS],
                      golden={q: sorted(g) for q, g in W.GOLDEN + W.GOLDEN_NEEDS_DESCRIPTIONS}),
    "finance":   dict(title="Bank ledger and trading", blurb="39 objects. The ledger at three grains, trades vs positions vs settlements, FX, lending, and KYC/AML tables most callers must never see.",
                      url=sqlite_url(Fn.DDL), hints=Fn.HINTS, restrict=Fn.RESTRICTED,
                      questions=[q for q, _ in Fn.GOLDEN] + [q for q, _ in Fn.GOLDEN_NEEDS_DESCRIPTIONS],
                      golden={q: sorted(g) for q, g in Fn.GOLDEN + Fn.GOLDEN_NEEDS_DESCRIPTIONS}),
    "telemetry": dict(title="IoT fleet telemetry", blurb="40 objects. Readings at raw, 1-minute and hourly grains, six monthly partitions, an alarm lifecycle across three tables, device placement history.",
                      url=sqlite_url(Tl.DDL), hints=Tl.HINTS, restrict=Tl.RESTRICTED,
                      questions=[q for q, _ in Tl.GOLDEN] + [q for q, _ in Tl.GOLDEN_NEEDS_DESCRIPTIONS],
                      golden={q: sorted(g) for q, g in Tl.GOLDEN + Tl.GOLDEN_NEEDS_DESCRIPTIONS}),
    "hostile":   dict(title="Hostile (260 objects)", blurb="260 objects across 4 schemas. Same table name in three schemas, an 8-deep FK chain, a cycle, composite keys, 320 columns, Spanish and Japanese names.",
                      engine=complex_engine(), hints=X.HINTS, restrict=X.RESTRICTED,
                      questions=[q for q, _ in X.GOLDEN] + [q for q, _ in X.GOLDEN_CROSS_LANGUAGE],
                      golden={q: sorted(g) for q, g in X.GOLDEN + X.GOLDEN_CROSS_LANGUAGE}),
}

out = {}
for key, spec in SCHEMAS.items():
    cat = Catalog(name=key).bootstrap(spec.get("engine") or spec["url"])
    names = {d.name for d in cat._docs.values()}
    hints = {t: h for t, h in spec["hints"].items() if t in names}
    restrict = {t: r for t, r in spec["restrict"].items() if t in names or t in cat._docs}
    out[key] = {"title": spec["title"], "blurb": spec["blurb"], "docs": [doc_json(d) for d in cat._docs.values()],
                "hints": hints, "restrict": restrict, "questions": spec["questions"], "golden": spec["golden"]}
json.dump(out, open(os.path.join(os.path.dirname(__file__), "schemas.json"), "w"), ensure_ascii=False)
print({k: len(v["docs"]) for k, v in out.items()})
