"""Descriptions without an API key.

The AI layer is optional, and so is the API key: ``describe_prompt()`` gives
you one prompt to paste into any chat window, ``describe(dict)`` takes the
reply, ``schemagate describe`` does both from the shell, and the ``describe``
block of a catalog config keeps the result for select, Studio and MCP.
"""
import json
import os
import subprocess
import sys

import pytest

from schemagate import Catalog, Principal
from schemagate import config as sgconfig
from schemagate.demo_schema import HINTS, create_demo_db

Q = "things we're running out of"
TARGET = "main.v_stock_shortfall"
REPLY = {"main.v_stock_shortfall": "Items running out of stock, below their reorder level."}


@pytest.fixture
def cat():
    c = Catalog().bootstrap(create_demo_db())
    for t, h in HINTS.items():
        c.hint(t, h)
    return c


def test_prompt_is_metadata_only_and_covers_every_undescribed_object(cat):
    p = cat.describe_prompt()
    undescribed = [d for d in cat.objects() if not (d.description or d.hint)]
    assert undescribed
    heads = set(p.splitlines())
    for d in undescribed:
        assert f"{d.kind} {d.qname}" in heads
    for d in cat.objects():
        if d.hint:                                   # only_missing skips hinted objects
            assert f"{d.kind} {d.qname}" not in heads
    assert "JSON" in p and "reorder_point" in p
    # nothing that looks like a row: the demo DB is empty anyway, but the
    # prompt must be built from ObjectDoc, which has no row field at all
    assert "INSERT" not in p and "VALUES" not in p


def test_prompt_all_includes_hinted_objects(cat):
    assert "billing_invoice" in cat.describe_prompt(only_missing=False)


def test_mapping_reply_is_applied_and_changes_retrieval(cat):
    assert TARGET not in cat.select(Q).table_names
    n = cat.describe(REPLY)
    assert n == 1
    assert cat.select(Q).table_names[0] == TARGET


def test_bare_names_and_qualified_names_both_work(cat):
    assert cat.describe({"v_stock_shortfall": "Items below their reorder level."}) == 1
    assert cat._docs[TARGET].description.startswith("Items")


def test_unknown_names_and_blank_text_are_ignored(cat):
    assert cat.describe({"nope": "x", TARGET: "   "}) == 0


def test_only_missing_respects_existing_hint(cat):
    assert cat.describe({"billing_invoice": "should not land"}) == 0
    assert cat.describe({"billing_invoice": "lands with only_missing=False"},
                        only_missing=False) == 1
    # and the human hint still outranks it in the DDL
    ddl = cat.select("issued invoices").prompt_fragment()
    assert HINTS["billing_invoice"] in ddl


def test_config_roundtrip(tmp_path, cat):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"restrict": {"hr_compensation": ["payroll"]},
                                "hint": {"sales_invoice_draft": "drafts only"}}))
    assert sgconfig.merge_descriptions(str(path), REPLY) == 1
    data = json.loads(path.read_text())
    assert set(data) == {"restrict", "hint", "describe"}      # other blocks kept
    fresh = Catalog().bootstrap(create_demo_db())
    sgconfig.apply(fresh, sgconfig.load(str(path)))
    assert fresh.select(Q).table_names[0] == TARGET
    assert "main.hr_compensation" not in fresh.select("salary by employee").table_names
    assert "main.hr_compensation" in fresh.select(
        "salary by employee", principal=Principal("okta:x", roles={"payroll"})).table_names


def test_load_missing_file_is_empty_and_non_object_rejected(tmp_path):
    assert sgconfig.load(str(tmp_path / "none.json")) == {}
    bad = tmp_path / "bad.json"; bad.write_text("[1,2]")
    with pytest.raises(ValueError):
        sgconfig.load(str(bad))


def _run(*args):
    return subprocess.run([sys.executable, "-m", "schemagate.cli", *args],
                          capture_output=True, text=True, timeout=120)


def test_cli_prompt_then_apply_then_select(tmp_path):
    url = create_demo_db()
    prompt = tmp_path / "prompt.txt"; reply = tmp_path / "reply.json"; cfg = tmp_path / "catalog.json"
    r = _run("describe", "--url", url, "--out", str(prompt)); assert r.returncode == 0, r.stderr
    assert "VIEW main.v_stock_shortfall" in prompt.read_text()
    # a chat reply wrapped in fences with one made-up name
    reply.write_text("```json\n" + json.dumps({**REPLY, "made_up": "x"}) + "\n```\n")
    r = _run("describe", "--url", url, "--apply", str(reply), "--config", str(cfg))
    assert r.returncode == 0, r.stderr
    assert "applied 1" in r.stdout and "made_up" in r.stdout
    saved = json.loads(cfg.read_text())["describe"]
    assert TARGET in saved and "made_up" not in saved
    before = _run("select", "--url", url, Q).stdout
    after = _run("select", "--url", url, Q, "--config", str(cfg)).stdout
    assert TARGET not in before.splitlines()[2] and TARGET in after.splitlines()[2]


def test_cli_provider_requires_model_and_never_needs_a_key():
    url = create_demo_db()
    r = _run("describe", "--url", url, "--provider", "anthropic")
    assert r.returncode != 0 and "--model" in r.stderr
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    r = subprocess.run([sys.executable, "-m", "schemagate.cli", "describe", "--url", url,
                        "--provider", "auto", "--model", "m"], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "works without one" in (r.stderr + r.stdout)


def test_mcp_server_reads_describe_block(tmp_path):
    from schemagate.mcp_server import _apply_config
    cfg = tmp_path / "c.json"; cfg.write_text(json.dumps({"describe": REPLY}))
    c = Catalog().bootstrap(create_demo_db())
    _apply_config(c, str(cfg))
    assert c.select(Q).table_names[0] == TARGET
