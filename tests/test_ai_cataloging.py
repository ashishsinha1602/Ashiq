"""Optional AI cataloguing.

Everything here runs offline against fake providers. No network, no API key,
no cost. The point is to pin the contract: what gets sent, what happens when
a provider misbehaves, and that none of it is ever required.
"""
import json
import sys

import pytest

from schemagate import Catalog, Column, ForeignKey, HashingEmbedder, ObjectDoc
from schemagate.ai import (
    APIEmbedder,
    CallableProvider,
    ProviderError,
    SchemaDescriber,
    auto_provider,
    available_providers,
)
from schemagate.ai.describe import _render


def doc(name="cust_ord_ln_t", **kw):
    kw.setdefault("columns", [
        Column("id", "NUMBER", pk=True),
        Column("qty", "NUMBER"),
        Column("amt", "NUMBER", comment="line total"),
    ])
    kw.setdefault("foreign_keys", [ForeignKey(["id_ord"], "cust_ord_t")])
    return ObjectDoc(name=name, **kw)


class FakeProvider:
    """Records what it was asked, returns a canned sentence."""

    name = "fake:test"

    def __init__(self, reply="Order line items with quantity and amount.",
                 fail_on=(), vectors=None):
        self.reply, self.fail_on = reply, set(fail_on)
        self.calls, self.embed_calls = [], []
        self.vectors = vectors

    def complete(self, system, prompt, max_tokens=1024):
        self.calls.append({"system": system, "prompt": prompt,
                           "max_tokens": max_tokens})
        for token in self.fail_on:
            if token in prompt:
                raise ProviderError("simulated failure")
        return self.reply

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        if self.vectors is None:
            return [[0.1] * 8 for _ in texts]
        return [self.vectors[t] for t in texts]


# --- schemagate must work with no AI at all ------------------------------------

def test_core_import_does_not_pull_in_optional_packages():
    """`import schemagate` must stay one dependency: SQLAlchemy.

    Checked in a fresh interpreter, because this process has already
    imported everything. Nothing optional -- AI providers, MCP, LangChain,
    Oracle -- may be loaded by the core import.
    """
    import subprocess
    probe = (
        "import sys, schemagate\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'anthropic','openai','google','mcp','langchain_core','oracledb',"
        "'sentence_transformers'} or m.startswith(('schemagate.ai','schemagate.mcp',"
        "'schemagate.integrations','schemagate.stores.oracle')))\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "[]", f"core import loaded: {out.stdout}"


def test_selection_works_with_no_provider():
    cat = Catalog()
    cat.add(doc("billing_invoice"))
    assert cat.select("invoice amounts").objects


def test_describe_with_no_targets_costs_nothing():
    provider = FakeProvider()
    cat = Catalog()
    cat.add(doc("t", description="already documented"))
    assert cat.describe(SchemaDescriber(provider)) == 0
    assert provider.calls == []


# --- what leaves the building ---------------------------------------------

def test_only_metadata_is_sent_never_row_data():
    """The rendered prompt is built from ObjectDoc, which holds no rows."""
    d = doc()
    rendered = _render(d)
    assert "cust_ord_ln_t" in rendered
    assert "qty NUMBER" in rendered
    assert "FK id_ord -> cust_ord_t" in rendered
    # nothing resembling a value, a row, or a credential can appear
    for forbidden in ["SELECT", "VALUES", "password", "row", "sample"]:
        assert forbidden.lower() not in rendered.lower()


def test_object_doc_carries_no_row_data_at_all():
    """Structural guarantee: there is no field a row could hide in."""
    fields = set(ObjectDoc(name="x").__dataclass_fields__)
    assert not fields & {"rows", "data", "sample", "values", "preview"}


def test_column_count_is_capped_so_wide_tables_do_not_blow_the_prompt():
    wide = ObjectDoc(name="w", columns=[Column(f"c{i}", "TEXT")
                                        for i in range(200)])
    rendered = _render(wide, max_columns=30)
    assert "c29" in rendered and "c30" not in rendered
    assert "170 more columns" in rendered


# --- the happy path -------------------------------------------------------

def test_description_lands_on_the_doc_and_is_indexed():
    provider = FakeProvider(reply="Order line items with quantity and amount.")
    cat = Catalog()
    cat.add(doc("cust_ord_ln_t"))
    assert cat.describe(SchemaDescriber(provider, workers=1)) == 1
    d = cat._docs["cust_ord_ln_t"]
    assert d.description == "Order line items with quantity and amount."
    assert "quantity" in d.embed_text()


#: what a competent model returns for these objects, in business language
#: rather than identifier language -- this is the signal descriptions add
_CANNED = {
    "billing_invoice": "Issued invoices and what customers still owe.",
    "v_monthly_revenue": "Money coming in each month, totalled by currency.",
    "v_stock_shortfall": "Items running out of stock, below their reorder level.",
    "v_employee_headcount": "How many people work in each team or department.",
    "ship_shipment": "Parcels dispatched and when they arrived, late or on time.",
    "v_supplier_spend": "What the business spends with each vendor per year.",
}


def _canned_provider():
    def fn(system, prompt):
        first = prompt.splitlines()[0]
        for name, text in _CANNED.items():
            if name in first:
                return text
        raise ProviderError("not in this test's canned set")
    return CallableProvider(fn, name="canned")


def test_descriptions_lift_paraphrase_recall_on_the_real_fixture(cat):
    """The whole point of AI cataloguing: questions in business words.

    The offline embedder matches subwords, so 'things we are running out of'
    misses v_stock_shortfall whose identifiers share no vocabulary with the
    question. Descriptions are what close that gap.
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from schema_fixture import GOLDEN_PARAPHRASE

    def recall():
        hits = total = 0
        for question, gold in GOLDEN_PARAPHRASE:
            got = {d.name for d in cat.select(question, top_k=6).objects}
            hits += len(gold & got)
            total += len(gold)
        return hits / total

    before = recall()
    cat.describe(SchemaDescriber(_canned_provider(), workers=1),
                 only_missing=False)
    after = recall()

    assert after > before, (
        f"descriptions did not help: {before:.0%} -> {after:.0%}")
    assert after == 1.0, f"expected full paraphrase recall, got {after:.0%}"


def test_describe_marks_the_index_stale():
    cat = Catalog()
    cat.add(doc("t"))
    cat.index()
    assert not cat._stale
    cat.describe(SchemaDescriber(FakeProvider(), workers=1))
    assert cat._stale


def test_human_hint_outranks_an_ai_description():
    cat = Catalog()
    cat.add(doc("t"))
    cat.describe(SchemaDescriber(FakeProvider(reply="AI text"), workers=1))
    cat.hint("t", "human text")
    assert cat._docs["t"].render_ddl().startswith("-- human text")


def test_only_missing_skips_documented_objects():
    provider = FakeProvider()
    cat = Catalog()
    cat.add(doc("documented", description="from the database"))
    cat.add(doc("hinted", hint="from a human"))
    cat.add(doc("bare"))
    assert cat.describe(SchemaDescriber(provider, workers=1)) == 1
    assert len(provider.calls) == 1
    assert "bare" in provider.calls[0]["prompt"]


def test_only_missing_false_describes_everything():
    provider = FakeProvider()
    cat = Catalog()
    cat.add(doc("documented", description="from the database"))
    cat.add(doc("bare"))
    cat.describe(SchemaDescriber(provider, workers=1), only_missing=False)
    assert len(provider.calls) == 2


# --- misbehaving providers must not break cataloguing ---------------------

def test_a_failing_object_is_skipped_not_fatal():
    provider = FakeProvider(fail_on=["broken"])
    cat = Catalog()
    cat.add(doc("broken"))
    cat.add(doc("fine"))
    describer = SchemaDescriber(provider, workers=1)
    assert cat.describe(describer) == 1
    assert describer.failures == ["broken"]
    assert cat._docs["fine"].description
    assert cat._docs["broken"].description is None
    assert cat.select("quantity and amount").objects   # still selects


def test_strict_mode_reraises():
    describer = SchemaDescriber(FakeProvider(fail_on=["broken"]),
                                workers=1, strict=True)
    cat = Catalog()
    cat.add(doc("broken"))
    with pytest.raises(ProviderError):
        cat.describe(describer)


def test_empty_reply_is_treated_as_a_failure():
    describer = SchemaDescriber(FakeProvider(reply="   "), workers=1)
    cat = Catalog()
    cat.add(doc("t"))
    assert cat.describe(describer) == 0
    assert describer.failures == ["t"]


def test_reply_is_cleaned_of_quotes_and_whitespace():
    describer = SchemaDescriber(
        FakeProvider(reply='  "Order lines.\n  Really."  '), workers=1)
    cat = Catalog()
    cat.add(doc("t"))
    cat.describe(describer)
    assert cat._docs["t"].description == "Order lines. Really."


# --- caching: descriptions cost money -------------------------------------

def test_cache_prevents_a_second_billed_call(tmp_path):
    provider = FakeProvider()
    path = str(tmp_path / "desc.json")
    cat = Catalog()
    cat.add(doc("t"))

    cat.describe(SchemaDescriber(provider, cache_path=path, workers=1))
    assert len(provider.calls) == 1

    cat2 = Catalog()
    cat2.add(doc("t"))
    cat2.describe(SchemaDescriber(provider, cache_path=path, workers=1))
    assert len(provider.calls) == 1, "second run must be free"
    assert cat2._docs["t"].description


def test_cache_misses_when_the_schema_changes(tmp_path):
    provider = FakeProvider()
    path = str(tmp_path / "desc.json")
    cat = Catalog()
    cat.add(doc("t"))
    cat.describe(SchemaDescriber(provider, cache_path=path, workers=1))

    changed = Catalog()
    changed.add(doc("t", columns=[Column("id", "NUMBER"),
                                  Column("new_column", "TEXT")]))
    changed.describe(SchemaDescriber(provider, cache_path=path, workers=1))
    assert len(provider.calls) == 2, "a changed table must be re-described"


def test_corrupt_cache_does_not_break_the_run(tmp_path):
    path = tmp_path / "desc.json"
    path.write_text("{ this is not json")
    cat = Catalog()
    cat.add(doc("t"))
    assert cat.describe(SchemaDescriber(FakeProvider(), cache_path=str(path),
                                        workers=1)) == 1


def test_estimate_calls_reports_cost_before_spending(tmp_path):
    path = str(tmp_path / "desc.json")
    docs = [doc("a"), doc("b")]
    describer = SchemaDescriber(FakeProvider(), cache_path=path, workers=1)
    assert describer.estimate_calls(docs) == 2
    describer.describe(docs)
    assert SchemaDescriber(FakeProvider(), cache_path=path,
                           workers=1).estimate_calls(docs) == 0


def test_cache_file_is_human_readable(tmp_path):
    path = tmp_path / "desc.json"
    cat = Catalog()
    cat.add(doc("t"))
    cat.describe(SchemaDescriber(FakeProvider(), cache_path=str(path),
                                 workers=1))
    saved = json.loads(path.read_text())
    assert "Order line items with quantity and amount." in saved.values()


def test_concurrent_describe_preserves_order():
    provider = FakeProvider()
    docs = [doc(f"t{i}") for i in range(12)]
    out = SchemaDescriber(provider, workers=4).describe(docs)
    assert set(out) == {d.qname for d in docs}


# --- API embedder ---------------------------------------------------------

def test_api_embedder_normalises_and_caches(tmp_path):
    provider = FakeProvider()
    emb = APIEmbedder(provider, dim=8, cache_path=str(tmp_path / "v.json"))
    first = emb.embed(["billing invoice"])
    assert abs(sum(x * x for x in first[0]) - 1.0) < 1e-9
    emb.embed(["billing invoice"])
    assert len(provider.embed_calls) == 1, "cached text must not re-embed"


def test_api_embedder_rejects_a_dimension_change():
    """A changed embedding model silently invalidates every stored vector."""
    emb = APIEmbedder(FakeProvider(), dim=1536)
    with pytest.raises(ProviderError, match="1536"):
        emb.embed(["anything"])


def test_api_embedder_requires_an_embed_capable_provider():
    class NoEmbed:
        name = "x"

        def complete(self, system, prompt, max_tokens=1024):
            return ""

    with pytest.raises(TypeError, match="embed"):
        APIEmbedder(NoEmbed(), dim=8)


def test_api_embedder_is_a_drop_in_for_the_offline_one():
    cat = Catalog(embedder=APIEmbedder(FakeProvider(vectors={}), dim=8))
    assert cat.embedder.dim == 8
    assert hasattr(cat.embedder, "embed") and hasattr(cat.embedder, "name")


def test_catalog_rejects_mismatched_embedder_and_store_dims():
    from schemagate import MemoryStore

    class DimStore(MemoryStore):
        dim = 8

    cat = Catalog(embedder=HashingEmbedder(dim=512), store=DimStore())
    cat.add(doc("t"))
    with pytest.raises(ValueError, match="512"):
        cat.index()


# --- provider detection ---------------------------------------------------

def test_available_providers_reports_names_never_keys():
    env = {"ANTHROPIC_API_KEY": "sk-secret", "GEMINI_API_KEY": "g-secret"}
    names = available_providers(env)
    assert names == ["AnthropicProvider", "GeminiProvider"]
    assert "sk-secret" not in " ".join(names)


def test_available_providers_empty_without_keys():
    assert available_providers({}) == []


def test_auto_provider_explains_itself_when_no_key_is_set():
    with pytest.raises(ValueError, match="schemagate works without one"):
        auto_provider(model="whatever", env={})


@pytest.mark.parametrize("cls_name,kwargs", [
    ("AnthropicProvider", {}),
    ("OpenAIProvider", {}),
    ("GeminiProvider", {}),
])
def test_providers_require_an_explicit_model(cls_name, kwargs):
    """Model IDs change; a hardcoded default eventually 404s for everyone."""
    import schemagate.ai as ai
    with pytest.raises(ValueError, match="model is required"):
        getattr(ai, cls_name)(model="", **kwargs)


def test_callable_provider_needs_no_sdk():
    seen = {}

    def fn(system, prompt):
        seen["system"] = system
        return "A table."

    p = CallableProvider(fn, name="mine")
    assert p.complete("sys", "prompt") == "A table."
    assert seen["system"] == "sys"


def test_callable_provider_embed_requires_an_embed_fn():
    with pytest.raises(ProviderError, match="embed_fn"):
        CallableProvider(lambda s, p: "x").embed(["a"])
