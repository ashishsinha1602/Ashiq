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


def test_columns_with_a_known_type_do_not_hit_the_database():
    """The hook must be free when there is nothing to fill in -- it runs for
    every table reflected."""
    class Boom:
        def connect(self):
            raise AssertionError("queried the database with nothing to fill")
    unknown_types(Boom(), "public", "t", [{"name": "id", "type": "INTEGER"}])
