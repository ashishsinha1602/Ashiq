"""Regressions for bugs that were live in the first cut of 0.1.

Each of these failed before the fix and would have shipped silently.
"""
import json
import subprocess
import sys
import textwrap

import pytest

from ashiq import Catalog, Column, HashingEmbedder, ObjectDoc


# --- 1. embeddings must be stable across interpreter processes -------------
# The sign of each hashed feature used to come from builtin hash(), which is
# salted per process. Same-process tests could never catch it; a persisted
# index would have degraded to noise on the next run.

_PROBE = textwrap.dedent("""
    import json
    from ashiq import HashingEmbedder
    print(json.dumps(HashingEmbedder(dim=128).embed(["cust_order_line total_net"])[0]))
""")


def _embed_in_fresh_process(seed):
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed,
             "PYTHONPATH": ":".join(sys.path)},
    )
    return json.loads(out.stdout)


def test_embedding_is_stable_across_processes_and_hash_seeds():
    a = _embed_in_fresh_process("0")
    b = _embed_in_fresh_process("12345")
    c = _embed_in_fresh_process("random")
    assert a == b == c, "HashingEmbedder must not depend on PYTHONHASHSEED"


def test_embedding_matches_in_process_value():
    """A vector persisted by another process must match one computed here."""
    assert _embed_in_fresh_process("999") == pytest.approx(
        HashingEmbedder(dim=128).embed(["cust_order_line total_net"])[0]
    )


# --- 2. no dead lazy exports ----------------------------------------------
# __getattr__ advertised OracleStore while its module did not exist, so the
# attribute raised ModuleNotFoundError instead of AttributeError -- breaking
# hasattr() and anything that probes for optional backends. OracleStore is
# real now, so the guard is generalised: every lazily advertised name must
# actually resolve, and anything else must raise AttributeError.

LAZY_EXPORTS = ["OracleStore", "SentenceTransformerEmbedder"]


@pytest.mark.parametrize("name", LAZY_EXPORTS)
def test_every_lazy_export_resolves(name):
    """A lazy export must import without its optional extra installed.

    Optional third-party imports belong inside __init__, not at module
    scope, so probing for a backend never explodes.
    """
    import ashiq
    assert getattr(ashiq, name) is not None
    assert hasattr(ashiq, name)


def test_unknown_attribute_raises_attribute_error():
    import ashiq
    with pytest.raises(AttributeError):
        ashiq.NoSuchThing
    assert not hasattr(ashiq, "NoSuchThing")


def test_every_public_name_actually_resolves():
    import ashiq
    for name in ashiq.__all__:
        assert getattr(ashiq, name) is not None


# --- 3. a hint must take effect without a manual reindex ------------------
# hint() mutated the doc but left the vector and BM25 index stale, so the
# README's own example silently had no effect on retrieval.

def test_hint_takes_effect_without_explicit_reindex(cat):
    q = "things we are running out of"
    before = {d.name for d in cat.select(q, top_k=6).objects}
    assert "v_stock_shortfall" not in before

    cat.hint("v_stock_shortfall", "items below reorder point, running out of stock")
    after = {d.name for d in cat.select(q, top_k=6).objects}
    assert "v_stock_shortfall" in after, "hint() must invalidate the index"


def test_add_after_index_is_picked_up():
    c = Catalog()
    c.add(ObjectDoc(name="alpha", columns=[Column("id", "INT")]))
    c.index()
    c.add(ObjectDoc(name="beta_widget_price", columns=[Column("price", "NUMBER")]))
    assert "beta_widget_price" in {d.name for d in c.select("widget price").objects}


def test_restrict_needs_no_reindex(cat):
    """Visibility is a select-time filter, so it must apply immediately."""
    cat.index()
    assert not cat._stale
    cat.restrict("hr_compensation", ["payroll"])
    assert not cat._stale, "restrict() must not force a rebuild"
    names = {d.name for d in cat.select("salary by employee", top_k=10).objects}
    assert "hr_compensation" not in names


# --- 4. non-ASCII identifiers were shredded by the tokenizer ---------------
# tokenize() used [a-z0-9]+, so 'facturación' became ['facturaci', 'n'] and
# CJK names became [] -- making those objects unreachable by name in any
# schema not written in English. Found by the 260-object complex fixture.

from ashiq.embedder import tokenize  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("facturación_mensual", ["facturacion", "mensual"]),
    ("año", ["ano"]),
    ("naïve_café", ["naive", "cafe"]),
    ("Müller_Straße", ["muller", "strasse"]),
])
def test_accented_identifiers_survive_tokenisation(text, expected):
    assert tokenize(text) == expected


def test_accent_folding_makes_unaccented_queries_match():
    assert tokenize("facturacion") == tokenize("facturación")
    assert tokenize("ano") == tokenize("año")


@pytest.mark.parametrize("text", ["売上明細", "金額", "한국어"])
def test_scripts_without_word_boundaries_produce_tokens(text):
    """These used to tokenise to [], making the object unfindable at all."""
    tokens = tokenize(text)
    assert tokens, f"{text!r} produced no tokens"
    assert text[0] in tokens                       # unigrams
    assert any(len(t) == 2 for t in tokens)        # bigrams


def test_cjk_query_matches_cjk_identifier():
    from ashiq import Catalog, Column, ObjectDoc

    cat = Catalog()
    cat.add(ObjectDoc(name="売上明細", columns=[Column("金額", "REAL")]))
    cat.add(ObjectDoc(name="billing_invoice", columns=[Column("total", "REAL")]))
    assert cat.select("売上", top_k=1).objects[0].name == "売上明細"


def test_ascii_tokenisation_is_byte_identical_to_before():
    """The Unicode fix must not perturb English schemas at all."""
    for text, expected in [
        ("billing_invoice_total", ["billing", "invoice", "total"]),
        ("CustOrderLine", ["cust", "order", "line"]),
        ("v_stock_shortfall", ["v", "stock", "shortfall"]),
        ("FCT_TXN_LN_2024", ["fct", "txn", "ln", "2024"]),
    ]:
        assert tokenize(text) == expected


# --- 5. a newline in a comment leaked uncommented text into the DDL --------
# Multi-line table comments are common in Oracle and PostgreSQL. Found by
# hypothesis in tests/test_any_schema.py.

def test_multiline_comments_cannot_escape_their_comment_line():
    from ashiq import Column, ObjectDoc
    doc = ObjectDoc(name="t", hint="first line\nSELECT * FROM secrets",
                    columns=[Column("c", "TEXT", comment="a\r\nb\tc")])
    for line in doc.render_ddl().splitlines():
        if "SELECT" in line or "secrets" in line:
            assert line.startswith("--"), line
        assert "\t" not in line
    assert "-- first line SELECT * FROM secrets" in doc.render_ddl()
