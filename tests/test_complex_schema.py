"""Stress and robustness against a large, hostile schema.

The other fixtures check that selection is *good*. This one checks that it
does not fall over: 260 objects across four schemas, duplicate names, an
8-deep foreign-key chain, a reference cycle, composite keys, a 320-column
table, 100-character identifiers, and non-ASCII names.

Everything here found at least one real bug while being written.
"""
import os
import sys
import tempfile
import time

import pytest
import sqlalchemy as sa

sys.path.insert(0, os.path.dirname(__file__))
from schema_fixture_complex import (  # noqa: E402
    ATTACHED_SCHEMAS,
    DDL,
    DECOYS,
    GOLDEN,
    GOLDEN_CROSS_LANGUAGE,
    HINTS,
    RESTRICTED,
)

from ashiq import Catalog, Principal  # noqa: E402
from ashiq.ai import CallableProvider, SchemaDescriber  # noqa: E402


from schema_fixture_complex import statements  # noqa: E402

#: The whole complex suite runs on every engine we can reach. SQLite always;
#: PostgreSQL when a URL is exported. A structural bug that only shows up on
#: a server database is exactly what this catches.
_ENGINES = ["sqlite"]
if os.environ.get("ASHIQ_POSTGRES_URL"):
    _ENGINES.append("postgres")


def _build_sqlite():
    """SQLite with three extra schemas ATTACHed on every connection.

    ATTACH is per-connection, so it has to run on connect -- getting this
    wrong silently reflects only the default schema, which is how the
    multi-schema half of this fixture nearly went untested.
    """
    base = tempfile.mkdtemp()
    eng = sa.create_engine("sqlite:///" + os.path.join(base, "main.db"))

    @sa.event.listens_for(eng, "connect")
    def _attach(dbapi_connection, _record):
        for schema in ATTACHED_SCHEMAS:
            dbapi_connection.execute(
                f"ATTACH DATABASE '{os.path.join(base, schema)}.db' AS {schema}")

    with eng.begin() as conn:
        for statement in DDL.split(";\n"):
            if statement.strip():
                conn.exec_driver_sql(statement)
    return eng


def _build_postgres():
    """Real schemas, and cycle constraints added after the fact.

    PostgreSQL rejects a foreign key that points at a table which does not
    exist yet, so every edge of the reference cycle is added by ALTER.
    """
    eng = sa.create_engine(os.environ["ASHIQ_POSTGRES_URL"],
                           isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        for schema in ["public"] + ATTACHED_SCHEMAS:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        for statement in statements(forward_refs=False):
            conn.exec_driver_sql(statement.rstrip(";"))
    return eng


@pytest.fixture(scope="module", params=_ENGINES)
def engine(request):
    eng = (_build_sqlite() if request.param == "sqlite"
           else _build_postgres())
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def big(engine):
    cat = Catalog(name="complex").bootstrap(engine)
    for table, text in HINTS.items():
        cat.hint(table, text)
    cat.index()
    return cat


def find(cat, bare_name):
    """Look up by bare name; the default schema is 'main' on SQLite and
    'public' on PostgreSQL, so tests must not hardcode either."""
    matches = [d for d in cat._docs.values() if d.name == bare_name]
    assert matches, f"{bare_name!r} not in the catalog"
    return matches[0]


# --- scale ----------------------------------------------------------------

def test_reflects_the_whole_thing(big):
    assert len(big._docs) >= 250
    assert {d.kind for d in big._docs.values()} == {"TABLE", "VIEW"}


def test_reflects_every_schema(big):
    schemas = {d.schema for d in big._docs.values()}
    assert {"billing", "crm", "sec"} <= schemas


def test_same_table_name_in_three_schemas_stays_distinct(big):
    """Anything keyed on the bare name collapses these into one."""
    accounts = sorted(q for q in big._docs if q.endswith(".account"))
    assert accounts == ["billing.account", "crm.account", "sec.account"]
    assert len({id(big._docs[q]) for q in accounts}) == 3
    assert big._docs["billing.account"].columns != big._docs["crm.account"].columns


def test_selection_stays_fast_at_this_size(big):
    """A linear scan per query is fine at 260 objects; catch it if it is not."""
    questions = [q for q, _ in GOLDEN]
    start = time.perf_counter()
    for question in questions:
        big.select(question, top_k=6)
    per_query = (time.perf_counter() - start) / len(questions)
    assert per_query < 0.5, f"{per_query * 1000:.0f}ms per query is too slow"


def test_reindex_is_not_pathological(engine):
    start = time.perf_counter()
    Catalog(name="timing").bootstrap(engine)
    assert time.perf_counter() - start < 30.0


# --- retrieval quality under heavy noise ----------------------------------

def test_recall_survives_the_noise(big):
    """Every bulk table has an _archive and a _stg twin competing with it."""
    hits = total = 0
    misses = []
    for question, gold in GOLDEN:
        got = {d.name for d in big.select(question, top_k=6).objects}
        found = len(gold & got)
        hits += found
        total += len(gold)
        if found < len(gold):
            misses.append((question, sorted(gold - got)))
    recall = hits / total
    assert recall == 1.0, f"recall {recall:.1%}, misses: {misses}"


@pytest.mark.parametrize("question,wanted,decoy", DECOYS)
def test_decoys_are_outranked(big, question, wanted, decoy):
    names = [d.name for d in big.select(question, top_k=6,
                                        expand_fks=False).objects]
    assert wanted in names, f"{wanted!r} missing for {question!r}"
    assert decoy not in names or names.index(wanted) < names.index(decoy), \
        f"{decoy!r} beat {wanted!r} for {question!r}"


def test_archive_and_staging_twins_are_demoted(big):
    """Every bulk table here has an _archive and a _stg copy, and a copy
    carries the same name words in a shorter document -- which cosine
    similarity rewards. Before shadow demotion the copies routinely won.
    Now the live table must outrank both of its own copies."""
    for question, live in [("customs entries", "logistics_customs_entry"),
                           ("device alarm thresholds", "telemetry_threshold_profile")]:
        names = [d.name for d in big.select(question, top_k=8,
                                            expand_fks=False).objects]
        assert live in names, f"{live} missing for {question!r}"
        for twin in (live + "_archive", live + "_stg"):
            assert twin not in names or names.index(live) < names.index(twin), \
                f"{twin} beat {live} for {question!r}"
    twins = {k for k in big.shadows() if k.endswith(("_archive", "_stg"))}
    assert len(twins) >= 100, "the _archive/_stg copies were not detected as shadows"


def test_exclude_patterns_remove_the_twins_at_reflection(engine):
    """Remedy one: never catalogue them."""
    cat = Catalog(name="excluded").bootstrap(
        engine, exclude=["*_archive", "*_stg"])
    assert len(cat._docs) < 150, "exclude patterns did not drop the twins"
    assert not [d for d in cat._docs.values()
                if d.name.endswith(("_archive", "_stg"))]
    top = [d.name for d in cat.select("customs entries", top_k=3,
                                      expand_fks=False).objects][0]
    assert top == "logistics_customs_entry"


def test_a_hint_promotes_the_live_table_over_its_twins(engine):
    """Remedy two: keep them, but say which one is authoritative."""
    cat = Catalog(name="hinted").bootstrap(engine)
    cat.hint("logistics_customs_entry",
             "live customs entries for consignments; the authoritative record")
    top = [d.name for d in cat.select("customs entries", top_k=3,
                                      expand_fks=False).objects][0]
    assert top == "logistics_customs_entry"


def test_view_on_view_on_view_is_reachable(big):
    names = {d.name for d in big.select("cash collected each month",
                                        top_k=6).objects}
    assert "v_cash_collected_monthly" in names


def test_meaning_that_lives_only_in_view_sql_is_indexed(big):
    """'dunning' appears nowhere except inside the view's definition."""
    doc = find(big, "v_at_risk_accounts")
    assert doc.definition, "the view definition was not reflected"
    assert "dunning" in doc.embed_text().lower()


# --- structures that break graph walks ------------------------------------

def test_deep_foreign_key_chain_is_reflected(big):
    for level in range(1, 8):
        doc = find(big, f"chain_level_{level}")
        assert any(fk.ref_table == f"chain_level_{level - 1}"
                   for fk in doc.foreign_keys)


def test_reference_cycle_does_not_hang_expansion(big):
    """a -> b -> c -> a. Expansion must terminate."""
    start = time.perf_counter()
    sel = big.select("cycle alpha beta gamma", top_k=6, expand_fks=True)
    assert time.perf_counter() - start < 5.0
    assert len(sel) < len(big._docs), "expansion pulled in the whole catalog"


def test_self_referencing_table_survives(big):
    doc = find(big, "org_unit")
    assert any(fk.ref_table == "org_unit" for fk in doc.foreign_keys)
    sel = big.select("organisational unit hierarchy", top_k=4)
    assert len(sel) <= len(big._docs)


def test_composite_foreign_key_is_reflected(big):
    doc = find(big, "composite_shipment_line")
    fk = next(f for f in doc.foreign_keys
              if f.ref_table == "composite_order_line")
    assert fk.columns == ["id_order", "line_no"], "composite FK lost a column"


def test_expansion_never_returns_more_than_the_catalog(big):
    for question, _ in GOLDEN:
        sel = big.select(question, top_k=6, expand_fks=True)
        assert len(sel) <= len(big._docs)
        assert len(set(sel.table_names)) == len(sel.table_names), "duplicate hit"


# --- pathological shapes --------------------------------------------------

def test_320_column_table_is_capped_in_the_prompt(big):
    doc = find(big, "wide_measurement_matrix")
    assert len(doc.columns) > 300
    rendered = doc.render_ddl(max_columns=40)
    # 40 columns rendered in total, of which one is the primary key
    assert rendered.count("\n  attribute_") == 39
    assert "\n  id INTEGER PK" in rendered
    assert "281 more columns" in rendered
    assert len(rendered) < 2000, "one wide table must not swamp the prompt"


def test_single_column_table_renders(big):
    rendered = find(big, "single_column_flag").render_ddl()
    assert rendered.count("(") == rendered.count(")")
    assert "only_column" in rendered


def test_long_identifier_is_preserved_as_the_database_reports_it(big):
    """Engines cap identifier length differently and ashiq must not add
    a second cap on top.

    PostgreSQL truncates to 63 bytes at CREATE time, Oracle to 128, SQLite
    not at all. So the assertion is that whatever the database kept, we
    kept too -- and that the object is still findable by its words.
    """
    candidates = [d.name for d in big._docs.values()
                  if d.name.startswith("enterprise_consolidated_quarterly")]
    assert candidates, "the long-identifier table was lost entirely"
    assert len(candidates[0]) >= 60, "the name was truncated beyond the engine's limit"
    found = {d.name for d in
             big.select("quarterly revenue recognition workpaper", top_k=5).objects}
    assert any(n.startswith("enterprise_consolidated") for n in found)


def test_prompt_fragment_is_balanced_for_every_object(big):
    """One malformed render would corrupt the whole system prompt."""
    for doc in big._docs.values():
        rendered = doc.render_ddl()
        assert rendered.count("(") == rendered.count(")"), doc.qname


# --- non-ASCII ------------------------------------------------------------

def test_non_ascii_objects_are_reflected_and_indexed(big):
    names = {d.name for d in big._docs.values()}
    assert "facturación_mensual" in names
    assert "売上明細" in names


def test_non_ascii_objects_are_findable_in_their_own_language(big):
    """Regression: [a-z0-9]+ tokenising shredded these into nothing."""
    spanish = {d.name for d in big.select("facturación mensual importe",
                                          top_k=5).objects}
    assert "facturación_mensual" in spanish

    japanese = {d.name for d in big.select("売上明細 金額", top_k=5).objects}
    assert "売上明細" in japanese


def test_accent_insensitive_matching(big):
    """People type 'facturacion'; the column is 'facturación'."""
    found = {d.name for d in big.select("facturacion mensual", top_k=5).objects}
    assert "facturación_mensual" in found


# --- identity scoping at scale --------------------------------------------

def test_restricted_objects_hidden_across_a_large_catalog(big):
    for table, roles in RESTRICTED.items():
        big.restrict(table, roles)

    analyst = Principal("okta:analyst")
    for question in ["audit trail of every session token",
                     "grievances and exit interviews",
                     "account balances and last login"]:
        sel = big.select(question, top_k=25, principal=analyst)
        names = {d.name for d in sel.objects}
        leaked = names & {"security_audit_trail", "security_session_token",
                          "hr_grievance", "hr_exit_interview"}
        assert not leaked, f"{leaked} leaked for {question!r}"
        fragment = sel.prompt_fragment().lower()
        for forbidden in leaked:
            assert forbidden not in fragment


def test_authorised_caller_still_sees_restricted_objects(big):
    for table, roles in RESTRICTED.items():
        big.restrict(table, roles)
    officer = Principal("okta:sec", roles={"security"})
    names = {d.name for d in big.select("audit trail", top_k=15,
                                        principal=officer).objects}
    assert "security_audit_trail" in names


def test_restriction_survives_fk_expansion_at_scale(big):
    """The chain is 8 deep; expansion must not smuggle a restricted table."""
    big.restrict("chain_level_3", ["deep"])
    sel = big.select("chain level label", top_k=10,
                     principal=Principal("okta:nobody"), expand_fks=True)
    assert "chain_level_3" not in {d.name for d in sel.objects}


def test_scoping_one_schema_does_not_affect_its_namesakes(big):
    """billing.account, crm.account and sec.account are different objects."""
    big.restrict("sec.account", ["security"])
    analyst = Principal("okta:analyst")
    visible = {d.qname for d in big.select("account number and status",
                                           top_k=25, principal=analyst).objects}
    assert "sec.account" not in visible
    assert big._docs["crm.account"].roles is None
    assert big._docs["billing.account"].roles is None


# --- what AI cataloguing is actually for ----------------------------------

_TRANSLATIONS = {
    "facturación_mensual": "Monthly invoicing totals: net amount billed per month.",
    "売上明細": "Sales detail lines with amount and quantity per transaction.",
}


def test_ai_descriptions_bridge_a_language_gap(engine):
    """The honest limit of the offline embedder, and the honest fix.

    It matches characters, so an English question cannot reach a
    Spanish- or Japanese-named table. A description in the question's
    language is what closes that, and this measures it rather than
    asserting it.
    """
    cat = Catalog(name="crosslang").bootstrap(engine)

    def recall():
        hits = total = 0
        for question, gold in GOLDEN_CROSS_LANGUAGE:
            got = {d.name for d in cat.select(question, top_k=6).objects}
            hits += len(gold & got)
            total += len(gold)
        return hits / total

    before = recall()
    assert before == 0.0, "the offline gap this test exists to measure is gone"

    def translate(system, prompt):
        first = prompt.splitlines()[0]
        for name, text in _TRANSLATIONS.items():
            if name in first:
                return text
        return "A table."

    cat.describe(SchemaDescriber(CallableProvider(translate), workers=1),
                 only_missing=False)
    assert recall() == 1.0, "descriptions did not bridge the language gap"
