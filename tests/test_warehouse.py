"""Star-schema selection: grain, slowly-changing dimensions, role-playing
dates, bridges, and the backup/staging copies every warehouse accumulates.

The point of this fixture is that several tables are *plausible* for most
questions and only one is right. It found a real ranking weakness while
being written -- a 3-column ``_tmp`` copy outscored the 25-column table it
was copied from, even with a hint -- and the shadow demotion in
``ashiq.catalog`` exists because of it.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from schema_fixture_warehouse import (  # noqa: E402
    DDL, DECOYS, GOLDEN, GOLDEN_NEEDS_DESCRIPTIONS, HINTS, RESTRICTED,
)

from ashiq import Catalog, Principal  # noqa: E402
from ashiq.ai import CallableProvider, SchemaDescriber  # noqa: E402


@pytest.fixture(scope="module")
def url():
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


@pytest.fixture(scope="module")
def wh(url):
    cat = Catalog(name="warehouse").bootstrap(url)
    for table, text in HINTS.items():
        cat.hint(table, text)
    return cat


def rank_of(cat, question, name, k=8):
    names = [d.name for d in cat.select(question, top_k=k, expand_fks=False).objects]
    return names.index(name) + 1 if name in names else None


# --- recall on questions with plausible wrong answers ----------------------

def test_reflects_the_star_schema(wh):
    assert len(wh._docs) > 45
    kinds = {d.kind for d in wh._docs.values()}
    assert kinds == {"TABLE", "VIEW"}


def test_recall_with_hints(wh):
    hits = total = 0
    misses = []
    for question, gold in GOLDEN:
        got = {d.name for d in wh.select(question, top_k=6).objects}
        found = len(gold & got)
        hits += found
        total += len(gold)
        if found < len(gold):
            misses.append((question, sorted(gold - got)))
    assert hits / total == 1.0, f"misses: {misses}"


def test_recall_without_hints_is_still_high(url):
    """Hints help, but the model must not depend on them entirely."""
    bare = Catalog(name="bare").bootstrap(url)
    hits = total = 0
    for question, gold in GOLDEN:
        got = {d.name for d in bare.select(question, top_k=6).objects}
        hits += len(gold & got)
        total += len(gold)
    assert hits / total >= 0.9


# --- grain --------------------------------------------------------------

def test_line_grain_question_gets_the_line_fact(wh):
    names = {d.name for d in wh.select("paid amount per claim line", top_k=4).objects}
    assert "fact_claim_line" in names


def test_claim_count_question_gets_the_header(wh):
    names = {d.name for d in wh.select("how many claims did each payer submit",
                                       top_k=4).objects}
    assert "fact_claim_header" in names


def test_monthly_trend_gets_the_aggregate(wh):
    names = {d.name for d in wh.select("monthly paid trend by payer", top_k=4).objects}
    assert "fact_claim_monthly_agg" in names


def test_enrollment_gets_member_month_not_claims(wh):
    names = [d.name for d in wh.select("eligible member count by month", top_k=4,
                                       expand_fks=False).objects]
    assert "fact_member_month" in names[:3]


# --- slowly-changing dimension ------------------------------------------

def test_point_in_time_question_reaches_history(wh):
    names = {d.name for d in wh.select("member address at the time of service",
                                       top_k=6).objects}
    assert "v_claim_line_as_of_service" in names or "dim_member_history" in names


def test_current_attribute_question_reaches_current_dim(wh):
    names = {d.name for d in wh.select("current plan tier for each member",
                                       top_k=4).objects}
    assert "dim_member" in names


# --- role-playing date and bridge ---------------------------------------

def test_date_dimension_pulled_in_by_foreign_key(wh):
    sel = wh.select("paid amount per claim line", top_k=2, expand_fks=True)
    assert "dim_date" in {d.name for d in sel.objects}
    assert any(h.reason == "fk" and h.doc.name == "dim_date" for h in sel.hits)


def test_bridge_table_selected_for_many_to_many(wh):
    names = {d.name for d in wh.select("diagnoses on each claim line", top_k=4).objects}
    assert "bridge_claim_diagnosis" in names


# --- shadows: the reason this fixture exists ----------------------------

def test_every_backup_and_staging_copy_is_detected_as_a_shadow(wh):
    shadows = wh.shadows()
    expected = {
        "main.fact_claim_line_bkp": "main.fact_claim_line",
        "main.fact_claim_line_old": "main.fact_claim_line",
        "main.fact_claim_line_tmp": "main.fact_claim_line",
        "main.fact_claim_line_v2": "main.fact_claim_line",
        "main.stg_claim_line": "main.fact_claim_line",
        "main.stg_member": "main.dim_member",
        "main.dim_member_bkp": "main.dim_member",
        "main.dim_provider_old": "main.dim_provider",
        "main.fact_claim_header_new": "main.fact_claim_header",
    }
    for shadow, base in expected.items():
        assert shadows.get(shadow) == base, f"{shadow} -> {shadows.get(shadow)}"


def test_real_tables_are_never_shadows(wh):
    shadows = wh.shadows()
    for real in ["main.fact_claim_line", "main.dim_member", "main.dim_provider",
                 "main.dim_date", "main.fact_member_month", "main.etl_load_log"]:
        assert real not in shadows


def test_a_standalone_v2_with_no_base_is_not_a_shadow():
    """The rule needs the base to exist; a lone name is left alone."""
    from ashiq import Column, ObjectDoc
    cat = Catalog()
    cat.add(ObjectDoc(name="pricing_v2", columns=[Column("price", "REAL")]))
    cat.add(ObjectDoc(name="stg_events", columns=[Column("payload", "TEXT")]))
    assert cat.shadows() == {}


def test_shadow_matching_stays_inside_one_schema():
    from ashiq import Column, ObjectDoc
    cat = Catalog()
    cat.add(ObjectDoc(name="account", schema="crm", columns=[Column("id", "INT")]))
    cat.add(ObjectDoc(name="account_old", schema="billing", columns=[Column("id", "INT")]))
    assert cat.shadows() == {}, "billing.account_old must not shadow crm.account"


@pytest.mark.parametrize("question,wanted,decoy", DECOYS)
def test_real_table_outranks_its_copies(wh, question, wanted, decoy):
    """A view over the right table may legitimately win; the copy may not."""
    wanted_rank = rank_of(wh, question, wanted)
    decoy_rank = rank_of(wh, question, decoy)
    assert wanted_rank is not None, f"{wanted} missing for {question!r}"
    assert decoy_rank is None or wanted_rank < decoy_rank, \
        f"{decoy} (rank {decoy_rank}) beat {wanted} (rank {wanted_rank})"


def test_the_bug_that_motivated_shadows(url):
    """Before demotion, a 3-column _tmp copy beat the 25-column real table."""
    question = "paid amount per claim line"
    fixed = Catalog(name="fixed").bootstrap(url)
    naive = Catalog(name="naive", shadow_suffixes=(), shadow_prefixes=()).bootstrap(url)

    real, tmp = rank_of(fixed, question, "fact_claim_line"), rank_of(fixed, question, "fact_claim_line_tmp")
    assert real is not None and (tmp is None or real < tmp)

    # with demotion switched off the copy wins, which is the bug -- proving
    # the toggle does something, so the fix cannot silently no-op
    real_n, tmp_n = rank_of(naive, question, "fact_claim_line"), rank_of(naive, question, "fact_claim_line_tmp")
    assert naive.shadows() == {}
    assert tmp_n is not None and tmp_n < real_n, "expected the naive ranking to reproduce the bug"


def test_naming_a_shadow_outright_reaches_it(wh):
    """A heuristic must not override what the user literally typed."""
    assert rank_of(wh, "fact_claim_line_v2 migration batch", "fact_claim_line_v2") == 1
    assert rank_of(wh, "rows in stg_member by load batch", "stg_member") == 1


def test_shadow_demotion_can_be_disabled(url):
    cat = Catalog(name="off", shadow_suffixes=(), shadow_prefixes=()).bootstrap(url)
    assert cat.shadows() == {}


# --- identity scoping on the warehouse ----------------------------------

def test_phi_and_actuarial_objects_are_scoped(wh):
    for table, roles in RESTRICTED.items():
        wh.restrict(table, roles)
    analyst = Principal("okta:analyst")
    for question in ["member address history and versions",
                     "premium and capitation per member month"]:
        sel = wh.select(question, top_k=15, principal=analyst)
        names = {d.name for d in sel.objects}
        assert not names & set(RESTRICTED), f"leaked for {question!r}"
    actuary = Principal("okta:act", roles={"actuarial"})
    assert "fact_member_month" in {d.name for d in wh.select(
        "premium and capitation per member month", top_k=10,
        principal=actuary).objects}


def test_shadow_penalty_does_not_apply_when_base_is_hidden(wh):
    """If the caller cannot see fact_claim_line, its copies must not be
    demoted on its account -- scoping must never make an object vanish
    for a reason the caller cannot see."""
    wh.restrict("fact_claim_line", ["claims"])
    try:
        outsider = Principal("okta:outsider")
        sel = wh.select("paid amount per claim line", top_k=6,
                        principal=outsider, expand_fks=False)
        names = [d.name for d in sel.objects]
        assert "fact_claim_line" not in names
        # some claim-line object is still findable for this caller
        assert any(n.startswith(("fact_claim_line", "v_claim_line")) for n in names)
    finally:
        wh._docs["main.fact_claim_line"].roles = None


# --- where descriptions earn their keep ---------------------------------

_GRAIN_AWARE = {
    "fact_claim_monthly_agg": "Fast pre-summed paid totals by month; what we paid out last month at a glance.",
    "v_pmpm": "Per-member-per-month cost: how much each person costs us monthly.",
    "v_claim_line_as_of_service": "Claim lines with where the member lived when they were treated.",
    "v_service_lag": "How long we take to pay: days from service to payment per claim.",
}


def test_grain_aware_descriptions_lift_the_hard_questions(url):
    cat = Catalog(name="desc").bootstrap(url)
    for table, text in HINTS.items():
        cat.hint(table, text)

    def recall():
        hits = total = 0
        for question, gold in GOLDEN_NEEDS_DESCRIPTIONS:
            got = {d.name for d in cat.select(question, top_k=6).objects}
            hits += len(gold & got)
            total += len(gold)
        return hits / total

    before = recall()

    def describe(system, prompt):
        first = prompt.splitlines()[0]
        for name, text in _GRAIN_AWARE.items():
            if name in first:
                return text
        return "Warehouse table."

    cat.describe(SchemaDescriber(CallableProvider(describe), workers=1),
                 only_missing=False)
    after = recall()
    assert after > before, f"{before:.0%} -> {after:.0%}"
    assert after == 1.0, f"expected 100% after descriptions, got {after:.0%}"
