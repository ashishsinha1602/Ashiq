"""Finance and telemetry, run through one contract.

Every domain fixture answers the same questions: does recall hold, does the
real table beat its copies, does scoping hold, and do descriptions lift the
questions phrased in business words. Parametrising over fixtures means a
new domain is one import away from the full treatment.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import schema_fixture_finance as finance      # noqa: E402
import schema_fixture_telemetry as telemetry  # noqa: E402

from ashiq import Catalog, Principal  # noqa: E402
from ashiq.ai import CallableProvider, SchemaDescriber  # noqa: E402

DOMAINS = {"finance": finance, "telemetry": telemetry}

#: What a competent model writes for the objects the business-word questions
#: target. Kept per domain so the lift is measured, not assumed.
DESCRIPTIONS = {
    "finance": {
        "gl_daily_balance": "What we owe and what is owed to us: closing balance per account per day.",
        "v_trial_balance": "Balances rolled up by account type, the accountant's trial balance.",
        "v_failed_settlements": "Deals that never actually completed: settlements that failed and why.",
        "v_counterparty_exposure": "How much we are on the hook for with each bank or counterparty.",
        "v_loans_in_arrears": "Customers behind on their loan payments and how much is overdue.",
    },
    "telemetry": {
        "v_open_alarms": "Alarms still beeping that nobody has cleared yet.",
        "v_silent_devices": "Boxes that have gone quiet: active devices not heard from in a day.",
        "v_calibration_overdue": "Sensors that need a technician visit soon because calibration is overdue.",
        "ops_work_order": "Technician visits: open and closed work orders per device.",
    },
}


@pytest.fixture(scope="module", params=sorted(DOMAINS))
def domain(request):
    mod = DOMAINS[request.param]
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(mod.DDL)
    conn.commit()
    conn.close()
    url = f"sqlite:///{path}"
    cat = Catalog(name=request.param).bootstrap(url)
    for table, text in mod.HINTS.items():
        cat.hint(table, text)
    return request.param, mod, url, cat


def rank_of(cat, question, name, k=8):
    names = [d.name for d in cat.select(question, top_k=k, expand_fks=False).objects]
    return names.index(name) + 1 if name in names else None


def test_reflects(domain):
    _, mod, _, cat = domain
    assert len(cat._docs) >= 35
    assert {d.kind for d in cat._docs.values()} == {"TABLE", "VIEW"}


def test_recall_with_hints(domain):
    _, mod, _, cat = domain
    hits = total = 0
    misses = []
    for question, gold in mod.GOLDEN:
        got = {d.name for d in cat.select(question, top_k=6).objects}
        found = len(gold & got)
        hits += found
        total += len(gold)
        if found < len(gold):
            misses.append((question, sorted(gold - got)))
    assert hits / total == 1.0, f"misses: {misses}"


def test_recall_without_hints(domain):
    _, mod, url, _ = domain
    bare = Catalog(name="bare").bootstrap(url)
    hits = total = 0
    for question, gold in mod.GOLDEN:
        got = {d.name for d in bare.select(question, top_k=6).objects}
        hits += len(gold & got)
        total += len(gold)
    assert hits / total >= 0.9


def test_real_table_outranks_its_copies(domain):
    _, mod, _, cat = domain
    for question, wanted, decoy in mod.DECOYS:
        w, d = rank_of(cat, question, wanted), rank_of(cat, question, decoy)
        assert w is not None, f"{wanted} missing for {question!r}"
        assert d is None or w < d, f"{decoy} (rank {d}) beat {wanted} (rank {w})"


def test_shadows_detected_and_real_tables_untouched(domain):
    name, mod, _, cat = domain
    shadows = cat.shadows()
    for _, wanted, decoy in mod.DECOYS:
        assert f"main.{wanted}" not in shadows, f"{wanted} wrongly marked as a shadow"
    copies = [k for k in shadows if k.split(".")[-1].startswith("stg_")
              or k.endswith(("_bkp", "_old", "_v2", "_tmp"))]
    assert len(copies) >= 3, f"too few shadows detected in {name}: {shadows}"


def test_scoping(domain):
    _, mod, _, cat = domain
    for table, roles in mod.RESTRICTED.items():
        cat.restrict(table, roles)
    role = next(iter(next(iter(mod.RESTRICTED.values()))))
    analyst = Principal("okta:analyst")
    officer = Principal("okta:officer", roles={role})
    for table in mod.RESTRICTED:
        words = table.replace("_", " ")
        assert table not in {d.name for d in cat.select(words, top_k=15, principal=analyst).objects}
        assert table not in cat.select(words, top_k=15, principal=analyst).prompt_fragment()
        assert table in {d.name for d in cat.select(words, top_k=15, principal=officer).objects}


def test_descriptions_lift_business_words(domain):
    name, mod, url, _ = domain
    cat = Catalog(name="desc").bootstrap(url)
    for table, text in mod.HINTS.items():
        cat.hint(table, text)

    def recall():
        hits = total = 0
        for question, gold in mod.GOLDEN_NEEDS_DESCRIPTIONS:
            got = {d.name for d in cat.select(question, top_k=6).objects}
            hits += len(gold & got)
            total += len(gold)
        return hits / total

    before = recall()
    canned = DESCRIPTIONS[name]

    def describe(system, prompt):
        first = prompt.splitlines()[0]
        for obj, text in canned.items():
            if obj in first:
                return text
        return "Table."

    cat.describe(SchemaDescriber(CallableProvider(describe), workers=1), only_missing=False)
    after = recall()
    assert after > before, f"{name}: {before:.0%} -> {after:.0%}"
    assert after == 1.0, f"{name}: expected 100% after descriptions, got {after:.0%}"


# --- domain-specific checks ---------------------------------------------

def test_finance_ledger_grains_are_distinguished(domain):
    name, _, _, cat = domain
    if name != "finance":
        pytest.skip()
    assert "gl_journal_line" in {d.name for d in cat.select("debits and credits by account", top_k=3).objects}
    assert "gl_daily_balance" in {d.name for d in cat.select("closing balance per account per day", top_k=3).objects}
    assert "gl_journal_header" in {d.name for d in cat.select("who posted each journal", top_k=3).objects}


def test_finance_did_hold_moved(domain):
    name, _, _, cat = domain
    if name != "finance":
        pytest.skip()
    top = lambda q: [d.name for d in cat.select(q, top_k=3, expand_fks=False).objects]  # noqa: E731
    assert "trd_trade" in top("trades executed by each trader")
    assert "trd_position" in top("end of day positions and unrealised pnl")
    assert "v_failed_settlements" in top("settlements that failed and why")


def test_telemetry_grain_follows_time_horizon(domain):
    name, _, _, cat = domain
    if name != "telemetry":
        pytest.skip()
    top = lambda q: [d.name for d in cat.select(q, top_k=3, expand_fks=False).objects]  # noqa: E731
    assert "tel_reading_raw" in top("every raw sample received in the last thirty seconds")
    assert "tel_reading_1m" in top("minute by minute readings for today")
    assert "tel_reading_1h" in top("hourly p95 over the last month")


def test_telemetry_partitions_do_not_crowd_out_the_base_table(domain):
    """Six monthly partitions of tel_reading_raw are the same table six times.
    They are not shadows by any suffix rule; exclude= is the tool for them,
    and without it the base table must still be reachable."""
    name, mod, url, cat = domain
    if name != "telemetry":
        pytest.skip()
    question = "every raw sample received in the last thirty seconds"
    names = [d.name for d in cat.select(question, top_k=8, expand_fks=False).objects]
    assert "tel_reading_raw" in names
    assert names.index("tel_reading_raw") < 4
    trimmed = Catalog(name="nopart").bootstrap(url, exclude=["tel_reading_raw_2*"])
    assert not any(n in {d.name for d in trimmed._docs.values()} for n in mod.PARTITIONS)


def test_telemetry_alarm_lifecycle(domain):
    name, _, _, cat = domain
    if name != "telemetry":
        pytest.skip()
    assert "v_open_alarms" in {d.name for d in cat.select("alarms that have not been cleared", top_k=3).objects}
    assert "v_alarm_time_to_ack" in {d.name for d in cat.select("how long alarms take to be acknowledged", top_k=3).objects}
