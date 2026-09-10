"""PostgreSQL: what the Inspector cannot tell us.

Three things cost prompt budget or accuracy on a real PostgreSQL database and
the Inspector cannot help with any of them.

* ``get_schema_names()`` returns the catalog schemas alongside the user's.
  ``pg_catalog`` and ``information_schema`` are hundreds of objects nobody
  asks questions about, and they would be indexed and scored like any table.
  ``public`` is emphatically NOT internal here -- it is where most databases
  keep everything, which is exactly why this list is per-dialect.

* Extensions bring their own tables. PostGIS installs ``spatial_ref_sys``
  (~8,500 rows of projection definitions) and a ``topology`` schema;
  TimescaleDB installs ``_timescaledb_catalog`` and ``_timescaledb_internal``;
  pg_stat_statements, pgAgent and others do the same. None of them are the
  user's data, and ``pg_depend`` knows which objects an extension owns, so we
  do not have to guess from names.

* SQLAlchemy reports a type it has no class for as NULL with a warning --
  ``geometry`` and ``geography`` from PostGIS, ``vector`` from pgvector,
  ``hstore``, ``citext``, domains, enums and range types. A column whose type
  reads NULL in the prompt is worse than useless: the model cannot tell an
  integer from a polygon. ``format_type()`` gives the name the user would
  write, which is the name that belongs in the DDL.
"""
from __future__ import annotations

from typing import List, Optional, Set

from . import FILL_UNKNOWN_TYPES, INTERNAL_SCHEMA, MAINTAINED_SCHEMAS

#: Schemas the server itself owns. Deliberately short: anything an extension
#: brings is found through pg_depend below rather than pattern-matched, and
#: `public` is the user's, not the platform's.
_INTERNAL_PREFIXES = ("pg_catalog", "information_schema", "pg_toast", "pg_temp")


def looks_internal(schema: str) -> bool:
    low = schema.lower()
    return any(low == p or low.startswith(p) for p in _INTERNAL_PREFIXES)


def maintained_schemas(engine) -> Set[str]:
    """Schemas owned by an installed extension.

    ``pg_depend`` records the dependency an extension has on the schema it
    installs, so this is exact rather than a guess at names. A database with
    no extensions returns an empty set and nothing is excluded.
    """
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("""
            SELECT n.nspname
              FROM pg_depend d
              JOIN pg_extension e ON e.oid = d.refobjid
              JOIN pg_namespace n ON n.oid = d.objid
             WHERE d.refclassid = 'pg_extension'::regclass
               AND d.classid    = 'pg_namespace'::regclass
        """).fetchall()
    return {r[0] for r in rows}


def unknown_types(engine, schema: Optional[str],
                  table: str, columns: List[dict]) -> None:
    """Fill in the type names SQLAlchemy returned as NULL, in place.

    ``format_type`` renders the declared type the way a user would write it --
    ``geometry(Point,4326)``, ``vector(1536)``, ``numeric(10,2)`` -- including
    the modifier, which is often the part that matters.
    """
    unknown = [c for c in columns
               if str(c.get("type")).upper() in ("NULL", "NULLTYPE")]
    if not unknown:
        return
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
              FROM pg_attribute a
              JOIN pg_class     c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relname = %(t)s
               AND n.nspname = COALESCE(%(s)s::text, current_schema())   -- ::text or PostgreSQL cannot infer the type of a NULL parameter
               AND a.attnum > 0
               AND NOT a.attisdropped
        """, {"t": table, "s": schema}).fetchall()
    real = {r[0]: r[1] for r in rows}
    for col in unknown:
        name = col.get("name")
        if name in real and real[name]:
            col["type"] = real[name]


MAINTAINED_SCHEMAS["postgresql"] = maintained_schemas
INTERNAL_SCHEMA["postgresql"] = looks_internal
FILL_UNKNOWN_TYPES["postgresql"] = unknown_types
