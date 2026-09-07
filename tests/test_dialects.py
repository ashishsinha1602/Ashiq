"""Reflection across database dialects.

``schemagate.introspect.reflect`` is written against SQLAlchemy's dialect-agnostic
``Inspector`` API, so it should work anywhere SQLAlchemy does. "Should" is
not evidence, so:

* SQLite runs everywhere and is always tested.
* Postgres runs whenever ``SCHEMAGATE_POSTGRES_URL`` is set.
* Oracle, SQL Server and MySQL run whenever their URL variables are set.
* A dialect-independent test asserts we only call Inspector methods that
  every mainstream dialect implements, which is what makes the untested
  ones a reasonable expectation rather than a guess.

To certify a dialect against your own database::

    export SCHEMAGATE_ORACLE_URL='oracle+oracledb://user:pw@host:1521/?service_name=FREEPDB1'
    pytest tests/test_dialects.py -v

or run ``python scripts/certify_dialect.py <url>`` for a standalone report.
"""
import os

import pytest
import sqlalchemy as sa

from schemagate import Catalog, Principal
from schemagate.introspect import reflect

# --- a small schema expressed in portable DDL -----------------------------
# Deliberately not the big fixture: this must run identically on five
# engines, so it avoids anything dialect-specific.

TABLES = sa.MetaData()

customer = sa.Table(
    "schemagate_customer", TABLES,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("account_number", sa.String(40), nullable=False),
    sa.Column("segment", sa.String(40)),
    comment="Customer accounts",
)

invoice = sa.Table(
    "schemagate_invoice", TABLES,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("id_customer", sa.Integer, sa.ForeignKey("schemagate_customer.id")),
    sa.Column("total_gross", sa.Numeric(12, 2)),
    sa.Column("status", sa.String(20)),
)

payroll = sa.Table(
    "schemagate_payroll", TABLES,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("id_customer", sa.Integer, sa.ForeignKey("schemagate_customer.id")),
    sa.Column("annual_amount", sa.Numeric(12, 2)),
)

#: dialect -> the environment variable that certifies it
DIALECT_ENV = {
    "sqlite": None,                     # always available, no URL needed
    "postgres": "SCHEMAGATE_POSTGRES_URL",
    "oracle": "SCHEMAGATE_ORACLE_URL",
    "mssql": "SCHEMAGATE_MSSQL_URL",
    "mysql": "SCHEMAGATE_MYSQL_URL",
}

DIALECT_URLS = {
    name: ("sqlite://" if var is None else os.environ.get(var))
    for name, var in DIALECT_ENV.items()
}


def _engine_for(name):
    url = DIALECT_URLS.get(name)
    if not url:
        pytest.skip(f"set {DIALECT_ENV[name]} to certify {name}")
    if name == "sqlite":
        url = "sqlite:///" + str(pytest.importorskip("tempfile").mktemp(suffix=".db"))
    return sa.create_engine(url)


@pytest.fixture(params=list(DIALECT_URLS))
def engine(request):
    eng = _engine_for(request.param)
    TABLES.drop_all(eng, checkfirst=True)
    TABLES.create_all(eng)
    try:
        yield eng
    finally:
        TABLES.drop_all(eng, checkfirst=True)
        eng.dispose()


# --- reflection -----------------------------------------------------------

def test_reflects_tables_columns_and_types(engine):
    docs = {d.name: d for d in reflect(engine)}
    assert "schemagate_customer" in docs and "schemagate_invoice" in docs
    cols = {c.name: c for c in docs["schemagate_customer"].columns}
    assert set(cols) >= {"id", "account_number", "segment"}
    assert cols["id"].pk is True
    assert cols["account_number"].type, "a type string must be captured"


def test_reflects_foreign_keys(engine):
    docs = {d.name: d for d in reflect(engine)}
    refs = {fk.ref_table.lower() for fk in docs["schemagate_invoice"].foreign_keys}
    assert "schemagate_customer" in refs


def test_reflection_is_idempotent(engine):
    a = sorted(d.qname for d in reflect(engine))
    b = sorted(d.qname for d in reflect(engine))
    assert a == b


def test_include_and_exclude_patterns(engine):
    only = {d.name for d in reflect(engine, include=["schemagate_inv%"])}
    assert only == {"schemagate_invoice"}
    without = {d.name for d in reflect(engine, exclude=["schemagate_payroll"])}
    assert "schemagate_payroll" not in without and "schemagate_invoice" in without


def test_end_to_end_selection_and_isolation(engine):
    """The whole library against a real engine, not just reflection."""
    cat = Catalog(name=f"dialect-{engine.dialect.name}")
    cat.add_all(reflect(engine, include=["schemagate_%"]))
    cat.index()
    cat.restrict("schemagate_payroll", ["payroll"])

    analyst = Principal("db:ANALYST")
    names = {d.name for d in cat.select("invoice totals per customer",
                                        top_k=5, principal=analyst).objects}
    assert "schemagate_invoice" in names
    assert "schemagate_payroll" not in names

    officer = Principal("db:HR", roles={"payroll"})
    allowed = {d.name for d in cat.select("annual amount per customer",
                                          top_k=5, principal=officer).objects}
    assert "schemagate_payroll" in allowed


def test_prompt_fragment_renders_for_this_dialect(engine):
    cat = Catalog(name=f"frag-{engine.dialect.name}")
    cat.add_all(reflect(engine, include=["schemagate_%"]))
    fragment = cat.select("invoice totals", top_k=3).prompt_fragment()
    assert "schemagate_invoice" in fragment
    assert fragment.count("(") == fragment.count(")")


# --- why the untested dialects are a reasonable expectation ---------------

def test_reflect_uses_only_universal_inspector_methods():
    """Guard against a vendor-specific call sneaking into reflection.

    Every method below is part of SQLAlchemy's dialect-agnostic Inspector
    contract. If someone adds a call outside this set, this test fails and
    the multi-dialect claim gets re-examined rather than quietly broken.
    """
    import inspect as pyinspect
    import re

    from schemagate import introspect

    source = pyinspect.getsource(introspect)
    called = set(re.findall(r"insp\.(\w+)", source))
    universal = {
        "get_schema_names", "get_table_names", "get_view_names",
        "get_columns", "get_pk_constraint", "get_foreign_keys",
        "get_table_comment", "get_view_definition", "default_schema_name",
    }
    assert called <= universal, f"non-universal Inspector calls: {called - universal}"


def test_reflect_contains_no_vendor_sql():
    """No hand-written SQL means nothing to port between dialects."""
    import inspect as pyinspect
    import re

    from schemagate import introspect

    source = pyinspect.getsource(introspect).upper()
    for statement in ["SELECT ", "FROM ALL_", "FROM DBA_", "FROM SYS.",
                      "INFORMATION_SCHEMA.", "PG_CATALOG."]:
        assert statement not in source, f"vendor SQL in reflection: {statement}"


def test_every_dialect_url_variable_is_documented():
    """A new dialect must be added to the docs, not just the test."""
    readme = (
        pytest.importorskip("pathlib").Path(__file__).parent.parent / "README.md"
    ).read_text()
    for name, var in DIALECT_ENV.items():
        if var is None:
            continue
        assert var in readme, f"{var} is not documented in the README"
