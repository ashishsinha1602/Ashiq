"""Selection quality on a schema from an unrelated domain.

The commerce fixture could flatter the retriever: its questions and its
identifiers share a lot of vocabulary. These tests run the same machinery
against a clinical-claims schema, where they mostly do not. If a change
tunes selection to one domain, this file is what notices.
"""
import sqlite3
import sys
import os
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from schema_fixture_health import DDL, DECOYS, GOLDEN, HINTS  # noqa: E402

from ashiq import Catalog, Principal  # noqa: E402


@pytest.fixture(scope="module")
def health():
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    cat = Catalog(name="health").bootstrap(f"sqlite:///{path}")
    for table, text in HINTS.items():
        cat.hint(table, text)
    return cat


def test_reflects_the_whole_schema(health):
    assert len(health._docs) == 27
    assert {d.kind for d in health._docs.values()} == {"TABLE", "VIEW"}


def test_recall_holds_in_a_second_domain(health):
    """The headline claim must not be specific to commerce vocabulary."""
    hits = total = 0
    misses = []
    for question, gold in GOLDEN:
        got = {d.name for d in health.select(question, top_k=6).objects}
        found = len(gold & got)
        hits += found
        total += len(gold)
        if found < len(gold):
            misses.append((question, sorted(gold - got)))
    recall = hits / total
    assert recall == 1.0, f"recall fell to {recall:.1%}; misses: {misses}"


@pytest.mark.parametrize("question,wanted,decoy", DECOYS)
def test_decoy_is_outranked_in_a_second_domain(health, question, wanted, decoy):
    """clm_claim_submitted is pre-adjudication; it is not payment data."""
    names = [d.name for d in
             health.select(question, top_k=6, expand_fks=False).objects]
    assert wanted in names, f"{wanted} missing for {question!r}"
    assert decoy not in names or names.index(wanted) < names.index(decoy), \
        f"{decoy} outranked {wanted} for {question!r}"


def test_fk_expansion_works_on_a_code_lookup_schema(health):
    """Clinical schemas hide meaning in code tables one join away."""
    sel = health.select("procedures performed on each encounter", top_k=3)
    assert any(h.reason == "fk" for h in sel.hits)


def test_isolation_holds_in_a_second_domain(health):
    """The security contract is not schema-specific."""
    health.restrict("mbr_eligibility", ["benefits"])
    analyst = Principal("okta:analyst")
    sel = health.select("coverage tier and premium per member", top_k=10,
                        principal=analyst)
    assert "mbr_eligibility" not in {d.name for d in sel.objects}
    assert "mbr_eligibility" not in sel.prompt_fragment().lower()

    officer = Principal("okta:benefits", roles={"benefits"})
    allowed = health.select("coverage tier and premium per member", top_k=10,
                            principal=officer)
    assert "mbr_eligibility" in {d.name for d in allowed.objects}


def test_two_catalogs_do_not_share_an_index(health, cat):
    """Distinct catalog names must not collide in one store."""
    assert health.name != cat.name
    health_names = {d.name for d in health.select("denial rate by payer").objects}
    commerce_names = {d.name for d in cat.select("revenue by month").objects}
    assert not (health_names & commerce_names)
