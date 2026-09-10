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

from typing import Dict, List, Optional, Set

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


#: engine -> {(schema, table, column): rendered type}. One query per engine,
#: not per table: a 400-table database would otherwise pay 400 round trips
#: during reflection, and the answer is the same for all of them.
_TYPE_CACHE: "Dict[int, Dict[tuple, str]]" = {}

_TYPES_SQL = """
SELECT n.nspname, c.relname, a.attname,
       format_type(a.atttypid, a.atttypmod) AS declared,
       t.typtype,
       CASE t.typtype
         WHEN 'e' THEN (SELECT string_agg(quote_literal(enumlabel), ', '
                                          ORDER BY enumsortorder)
                          FROM pg_enum WHERE enumtypid = t.oid)
         WHEN 'd' THEN format_type(t.typbasetype, t.typtypmod)
       END AS detail
  FROM pg_attribute a
  JOIN pg_class     c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_type      t ON t.oid = a.atttypid
 WHERE a.attnum > 0
   AND NOT a.attisdropped
   AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
   AND n.nspname NOT IN ('pg_catalog', 'information_schema')
   AND (t.typtype IN ('e', 'd') OR t.typtype = 'b')
"""


def _render(declared: str, typtype: str, detail: Optional[str]) -> Optional[str]:
    """What belongs in the prompt for this column.

    An enum is the case worth the trouble. SQLAlchemy maps PostgreSQL's
    ``mood`` to ``VARCHAR(5)`` -- plausible, and useless: the model cannot
    know the column only ever holds 'sad', 'ok' or 'happy', which is exactly
    what it needs to write a correct WHERE clause. A domain is reported as
    the bare word ``DOMAIN``, which does not even say it is an integer.
    """
    if typtype == "e" and detail:
        return f"{declared} ENUM({detail})"
    if typtype == "d" and detail:
        return f"{declared} DOMAIN OVER {detail}"
    return None


def _catalog_types(engine) -> "Dict[tuple, str]":
    key = id(engine)
    if key in _TYPE_CACHE:
        return _TYPE_CACHE[key]
    out: "Dict[tuple, str]" = {}
    with engine.connect() as conn:
        for nsp, rel, col, declared, typtype, detail in conn.exec_driver_sql(_TYPES_SQL):
            better = _render(declared, typtype, detail)
            out[(nsp, rel, col)] = better or declared
    _TYPE_CACHE[key] = out
    return out


def unknown_types(engine, schema: Optional[str],
                  table: str, columns: List[dict]) -> None:
    """Correct the column types SQLAlchemy could not render usefully.

    Three cases, and only the first is the one the Oracle module deals with:

    * a type SQLAlchemy has no class for comes back as NULL -- ``geometry``
      and ``geography`` from PostGIS, ``vector`` from pgvector, ``citext``.
    * an enum comes back as ``VARCHAR(n)``. Worse than NULL, because it looks
      right; the allowed values are lost and nothing flags it.
    * a domain comes back as the bare word ``DOMAIN``.

    Found by reflecting a live PostgreSQL 16: ``hstore``, ``int4range`` and
    ``inet`` resolve on their own, so this only rewrites what it can improve.
    """
    if not columns:
        return
    try:
        known = _catalog_types(engine)
    except Exception:
        return
    nsp = schema or "public"
    for col in columns:
        rendered = known.get((nsp, table, col.get("name")))
        if not rendered:
            continue
        current = str(col.get("type") or "").upper()
        if (current in ("NULL", "NULLTYPE", "DOMAIN")
                or "ENUM(" in rendered or "DOMAIN OVER" in rendered):
            col["type"] = rendered


MAINTAINED_SCHEMAS["postgresql"] = maintained_schemas
INTERNAL_SCHEMA["postgresql"] = looks_internal
FILL_UNKNOWN_TYPES["postgresql"] = unknown_types
