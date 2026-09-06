"""The Studio's in-browser selector must rank exactly like the Python one.

``studio/ashiq.js`` is a port of the catalog, embedder and models. This test
runs it under Node against the same four schemas and a few hundred
(question, principal, options) cases, and requires identical object lists,
identical selection reasons, and scores equal to ten decimal places.

Skipped when Node or the studio directory is absent -- the port is a
sibling of the library, not part of the wheel.
"""
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STUDIO = ROOT / "studio"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (STUDIO / "ashiq.js").exists(),
    reason="needs node and ../studio/ashiq.js")

sys.path.insert(0, os.path.dirname(__file__))
from ashiq import Catalog, Principal  # noqa: E402
from ashiq.embedder import HashingEmbedder, tokenize  # noqa: E402


@pytest.fixture(scope="module")
def schemas():
    subprocess.run([sys.executable, str(STUDIO / "export_schemas.py")],
                   check=True, capture_output=True)
    return json.loads((STUDIO / "schemas.json").read_text("utf-8"))


def _python_catalog(spec, hints, describe=None):
    from ashiq import Column, ForeignKey, ObjectDoc
    cat = Catalog()
    for d in spec["docs"]:
        cat.add(ObjectDoc(
            name=d["name"], schema=d["schema"], kind=d["kind"],
            description=d["description"], hint=None, definition=d["definition"],
            roles=None,
            columns=[Column(c["name"], c["type"], c["nullable"], c["comment"], c["pk"])
                     for c in d["columns"]],
            foreign_keys=[ForeignKey(f["columns"], f["ref_table"], f["ref_columns"])
                          for f in d["foreign_keys"]]))
    if hints:
        for t, h in spec["hints"].items():
            cat.hint(t, h)
    for t, r in spec["restrict"].items():
        cat.restrict(t, r)
    if describe:
        for q, d in cat._docs.items():
            if q in describe or d.name in describe:
                d.description = describe.get(q) or describe.get(d.name)
        cat._stale = True
    return cat


def _cases(schemas):
    cases = []
    i = 0
    for key, spec in schemas.items():
        principals = [(None, []), ("okta:analyst", []),
                      ("okta:priv", sorted({r for rs in spec["restrict"].values() for r in rs}))]
        for hints in (False, True):
            for question in spec["questions"]:
                for principal, roles in principals:
                    for top_k, expand in ((6, True), (3, False), (10, True)):
                        cases.append(dict(id=i, schema=key, hints=hints, question=question,
                                          principal=principal, roles=roles, top_k=top_k,
                                          expand_fks=expand))
                        i += 1
        # explicit shadow naming and a described-object case
        for q in ["rows in stg_member by load batch", "stg_trade load batch",
                  "fact_claim_line_v2 migration batch", "dev_device_v2 migration batch"]:
            cases.append(dict(id=i, schema=key, hints=True, question=q,
                              principal=None, roles=[], top_k=5, expand_fks=False)); i += 1
    cases.append(dict(id=i, schema="commerce", hints=True, question="things we're running out of",
                      principal=None, roles=[], top_k=6, expand_fks=True,
                      describe={"v_stock_shortfall": "Items running out of stock, below their reorder level."}))
    return cases


def test_js_port_ranks_identically(schemas):
    cases = _cases(schemas)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cases, fh)
        cases_path = fh.name
    out = subprocess.run([NODE, str(STUDIO / "parity_runner.js"),
                          str(STUDIO / "schemas.json"), cases_path],
                         capture_output=True, text=True, check=True, cwd=STUDIO)
    js = json.loads(out.stdout)
    by_id = {r["id"]: r for r in js["results"]}

    cats = {}
    mismatches = []
    for c in cases:
        key = (c["schema"], c["hints"], json.dumps(c.get("describe") or {}, sort_keys=True))
        if key not in cats:
            cats[key] = _python_catalog(schemas[c["schema"]], c["hints"], c.get("describe"))
        cat = cats[key]
        who = Principal(c["principal"], roles=set(c["roles"])) if c["principal"] else None
        sel = cat.select(c["question"], top_k=c["top_k"], principal=who,
                         expand_fks=c["expand_fks"])
        py = dict(names=sel.table_names, reasons=[h.reason for h in sel.hits],
                  scores=[round(h.score, 10) for h in sel.hits])
        r = by_id[c["id"]]
        if (py["names"], py["reasons"]) != (r["names"], r["reasons"]):
            mismatches.append((c["schema"], c["question"], c["principal"], py["names"][:4], r["names"][:4]))
        else:
            for a, b in zip(py["scores"], r["scores"]):
                assert abs(a - b) < 1e-9, (c["question"], a, b)

    assert not mismatches, f"{len(mismatches)}/{len(cases)} cases differ, e.g. {mismatches[:3]}"
    assert len(cases) > 300


def test_js_tokenizer_matches(schemas):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump([], fh)
        empty = fh.name
    out = subprocess.run([NODE, str(STUDIO / "parity_runner.js"),
                          str(STUDIO / "schemas.json"), empty],
                         capture_output=True, text=True, check=True, cwd=STUDIO)
    js = json.loads(out.stdout)
    for text, tokens in js["tokenize"].items():
        assert tokenize(text) == tokens, text


def test_js_embedder_matches(schemas):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump([], fh)
        empty = fh.name
    out = subprocess.run([NODE, str(STUDIO / "parity_runner.js"),
                          str(STUDIO / "schemas.json"), empty],
                         capture_output=True, text=True, check=True, cwd=STUDIO)
    js = json.loads(out.stdout)
    py = HashingEmbedder(64).embed(["customer order line items", "売上明細 金額"])
    for a, b in zip(py, js["embed"]):
        assert max(abs(x - y) for x, y in zip(a, b)) < 1e-9
