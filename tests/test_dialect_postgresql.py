"""PostgreSQL reflection refinements.

These are unit tests over the dialect hooks. The SQL they wrap is exercised
against a live server by `scripts/certify_dialect.py`; what is worth pinning
here is the classification, because getting it wrong empties a catalog rather
than tidying one.
"""
from __future__ import annotations

import pytest

from schemagate.dialects import (FILL_UNKNOWN_TYPES, INTERNAL_SCHEMA,
                                 MAINTAINED_SCHEMAS)
from schemagate.dialects.postgresql import looks_internal, unknown_types


def test_public_is_the_users_schema_not_the_platforms():
    """The trap this whole per-dialect design exists for. `PUBLIC` is a
    pseudo-schema on Oracle and is skipped there; on PostgreSQL it is where
    most databases keep everything. Treating it as internal would return an
    empty catalog for the majority of PostgreSQL users."""
    assert looks_internal("public") is False
    assert looks_internal("PUBLIC") is False


@pytest.mark.parametrize("schema", ["pg_catalog", "information_schema",
                                    "pg_toast", "pg_toast_temp_1", "pg_temp_3"])
def test_server_owned_schemas_are_excluded(schema):
    """Hundreds of objects nobody asks questions about, scored like any table
    and paid for in every prompt if they are indexed."""
    assert looks_internal(schema) is True


@pytest.mark.parametrize("schema", ["sales", "hr", "app", "reporting",
                                    "pgx", "pgbouncer", "pg_my_own_schema"])
def test_user_schemas_are_kept(schema):
    """`pgx` and `pgbouncer` are the checks that matter: a loose match on
    "pg" would eat both.

    `pg_my_own_schema` is kept too, and that is deliberate. Only the four
    schema families the server actually owns are excluded by name; anything
    else is the user's until proven otherwise. PostgreSQL reserves the `pg_`
    prefix, so such a schema is unusual -- but indexing one schema too many
    costs a little prompt budget, while dropping a user's tables makes the
    answer wrong and says nothing about why."""
    assert looks_internal(schema) is False


def test_all_three_hooks_are_registered():
    for table in (INTERNAL_SCHEMA, MAINTAINED_SCHEMAS, FILL_UNKNOWN_TYPES):
        assert "postgresql" in table


def test_a_table_with_no_columns_does_not_hit_the_database():
    """This used to assert something stronger -- that a column already
    reading INTEGER meant the hook could skip the catalog entirely. That is
    not true and cannot be: an enum reflects as ``VARCHAR(5)``, which looks
    just as settled as INTEGER and is just as wrong. Deciding a column needs
    no correction requires the catalog, so the only free case left is having
    nothing to correct.

    Worth recording how that assertion survived being false: a
    ``try/except Exception`` inside ``unknown_types`` caught this test's own
    AssertionError and returned, so the test passed against a module where
    the whole feature raised NameError on every call. The except is gone --
    ``fill_unknown_types`` already guards a database that will not answer,
    and one layer of swallowing is enough.

    Cost is bounded by the cache, not by skipping: see
    ``test_the_catalog_is_queried_once_per_engine_not_once_per_table``."""
    class Boom:
        def connect(self):
            raise AssertionError("queried the database with nothing to fill")
    unknown_types(Boom(), "public", "t", [])


# --- what a live PostgreSQL 16 reflection actually returned -----------------
# The unit tests above pass on a version of this module that leaves enums and
# domains broken. These pin what reflecting a real server showed.

from schemagate.dialects.postgresql import _render  # noqa: E402


def test_an_enum_carries_its_allowed_values_into_the_prompt():
    """SQLAlchemy maps a PostgreSQL enum to VARCHAR(n) -- plausible, and
    worse than NULL because nothing flags it. The model never learns the
    column only holds three values, which is exactly what it needs to write
    a correct WHERE clause."""
    assert _render("mood", "e", "'sad', 'ok', 'happy'") == \
        "mood ENUM('sad', 'ok', 'happy')"


def test_a_domain_says_what_it_is_a_domain_over():
    """Reported as the bare word DOMAIN, which does not even say integer."""
    assert _render("positive_int", "d", "integer") == \
        "positive_int DOMAIN OVER integer"


def test_ordinary_types_are_left_alone():
    """hstore, int4range and inet resolve on their own; rewriting them would
    be churn. Only improve what can be improved."""
    for declared in ("hstore", "int4range", "inet", "numeric(12,2)"):
        assert _render(declared, "b", None) is None


def test_the_catalog_is_queried_once_per_engine_not_once_per_table():
    """A 400-table database would otherwise pay 400 round trips during
    reflection. Measured on a live server: 1 query for 404 tables."""
    import schemagate.dialects.postgresql as pg
    calls = []

    class FakeConn:
        def exec_driver_sql(self, sql):
            calls.append(sql)
            return [("public", "t", "c", "mood", "e", "'a', 'b'")]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeEngine:
        def connect(self): return FakeConn()

    eng = FakeEngine()
    pg._TYPE_CACHE.pop(id(eng), None)
    for _ in range(50):
        pg.unknown_types(eng, "public", "t", [{"name": "c", "type": "VARCHAR(5)"}])
    assert len(calls) == 1, f"queried the catalog {len(calls)} times"
    pg._TYPE_CACHE.pop(id(eng), None)


def test_the_catalog_query_contains_no_percent_sign():
    """`exec_driver_sql` hands the statement to the driver verbatim, and
    psycopg3 parses `%` as the start of a placeholder -- a `LIKE 'pg_toast%'`
    fails the entire query with "only '%s', '%b', '%t' are allowed as
    placeholders", which takes every type fill down with it. Cost a live
    debugging round; the prefix tests use `left()` instead."""
    from schemagate.dialects.postgresql import _TYPES_SQL
    assert "%" not in _TYPES_SQL
