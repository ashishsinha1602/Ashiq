"""Live provider tests. Real keys, real models, real network -- nothing mocked.

Each block runs only when its credentials are present and is skipped
otherwise, so CI stays offline while a developer with keys gets the real
thing. What is asserted is behaviour, not wording: the model must return one
sentence of the requested shape for a real table definition, the describer
must land it on the catalog, and business-language selection must improve.

    ANTHROPIC_API_KEY=...                       -> Anthropic block
    OPENAI_API_KEY=...                          -> OpenAI block
    GEMINI_API_KEY=...                          -> Gemini block
    OCI_COMPARTMENT_ID=ocid1... OCI_REGION=...  -> OCI block (uses ~/.oci/config)
    SCHEMAGATE_LIVE_LOCAL=1                     -> local transformers block

Model ids are read from SCHEMAGATE_<PROVIDER>_MODEL so they never go stale
in the code; a sensible current default is used when unset.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from schemagate import Catalog  # noqa: E402
from schemagate.ai import SchemaDescriber  # noqa: E402
from schemagate.demo_schema import GOLDEN_PARAPHRASE, create_demo_db  # noqa: E402

pytestmark = pytest.mark.live


def _model(var: str, default: str) -> str:
    return os.environ.get(var, default)


def _providers():
    """Every provider whose credentials are present, built for real."""
    from schemagate.ai import (AnthropicProvider, GeminiProvider, LocalProvider,
                               OCIGenAIProvider, OpenAIProvider)
    out = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append(AnthropicProvider(model=_model("SCHEMAGATE_ANTHROPIC_MODEL", "claude-sonnet-4-5")))
    if os.environ.get("OPENAI_API_KEY"):
        out.append(OpenAIProvider(model=_model("SCHEMAGATE_OPENAI_MODEL", "gpt-4.1-mini"),
                                  embed_model="text-embedding-3-small"))
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        out.append(GeminiProvider(model=_model("SCHEMAGATE_GEMINI_MODEL", "gemini-2.5-flash")))
    if os.environ.get("OCI_COMPARTMENT_ID"):
        out.append(OCIGenAIProvider(
            model=_model("SCHEMAGATE_OCI_MODEL", "meta.llama-3.3-70b-instruct"),
            region=os.environ.get("OCI_REGION"),
            embed_model=_model("SCHEMAGATE_OCI_EMBED_MODEL", "cohere.embed-multilingual-v3.0")))
    if os.environ.get("SCHEMAGATE_LIVE_LOCAL"):
        out.append(LocalProvider(model=_model("SCHEMAGATE_LOCAL_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")))
    return out


LIVE = _providers()
if not LIVE:
    pytest.skip("no provider credentials in the environment; see module docstring",
                allow_module_level=True)


@pytest.fixture(params=LIVE, ids=[p.name for p in LIVE])
def provider(request):
    return request.param


TABLE = ("TABLE main.billing_credit_note\n  id INTEGER PK\n  id_invoice INTEGER\n"
         "  issued_on TEXT\n  amount REAL\n  reason_code TEXT\n  FK id_invoice -> billing_invoice")


def test_model_writes_one_business_sentence(provider):
    from schemagate.ai.describe import _SYSTEM
    text = provider.complete(_SYSTEM, TABLE, max_tokens=120).strip()
    assert text, f"{provider.name} returned nothing"
    assert len(text.split()) <= 40, f"{provider.name} did not keep it to one sentence: {text!r}"
    assert "billing_credit_note" not in text.lower(), "must not repeat the object name"
    low = text.lower()
    assert any(w in low for w in ("credit", "refund", "invoice", "return")), text


def test_describer_lands_descriptions_on_the_catalog(provider):
    cat = Catalog().bootstrap(create_demo_db())
    n = cat.describe(SchemaDescriber(provider), only_missing=False)
    assert n == len(cat)
    described = [d for d in cat.objects() if d.description]
    assert len(described) == len(cat), f"{provider.name}: {len(described)}/{len(cat)} described"
    assert all(len(d.description.split()) <= 40 for d in described)


def test_descriptions_improve_business_language_recall(provider):
    """The claim from tests/test_business_language.py, with a real model
    writing the descriptions blind, on the commerce paraphrase set."""
    def recall(cat):
        hits = 0
        for q, gold in GOLDEN_PARAPHRASE:
            got = {n.split(".")[-1] for n in cat.select(q, top_k=6).table_names}
            hits += bool(gold & got)
        return hits / len(GOLDEN_PARAPHRASE)

    before = Catalog().bootstrap(create_demo_db())
    r0 = recall(before)
    after = Catalog().bootstrap(create_demo_db())
    after.describe(SchemaDescriber(provider), only_missing=False)
    after.index()
    r1 = recall(after)
    print(f"\n{provider.name}: business-language recall {r0:.0%} -> {r1:.0%}")
    assert r1 > r0, f"{provider.name}: descriptions did not help ({r0:.0%} -> {r1:.0%})"
    assert r1 >= 0.8, f"{provider.name}: {r1:.0%} with descriptions is below the 80% bar"


def test_embeddings_when_the_provider_offers_them(provider):
    if not getattr(provider, "embed_model", None):
        pytest.skip(f"{provider.name} not configured for embeddings")
    vecs = provider.embed(["unpaid invoices", "customer balance", "sensor firmware"])
    assert len(vecs) == 3 and len(vecs[0]) > 100
    import math
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    assert cos(vecs[0], vecs[1]) > cos(vecs[0], vecs[2]), "invoices should sit nearer balances than firmware"
