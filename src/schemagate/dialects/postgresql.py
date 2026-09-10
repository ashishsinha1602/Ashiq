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

from . import (FILL_UNKNOWN_TYPES, INTERNAL_SCHEMA, MAINTAINED_OBJECTS,
               MAINTAINED_SCHEMAS)

#: Schemas the server itself owns. Deliberately short: anything an extension
#: brings is found through pg_depend below rather than pattern-matched, and
#: `public` is the user's, not the platform's.
_INTERNAL_PREFIXES = ("pg_catalog", "information_schema", "pg_toast", "pg_temp")


def looks_internal(schema: str) -> bool:
    low = schema.lower()
    return any(low == p or low.startswith(p) for p in _INTERNAL_PREFIXES)


def maintained_schemas(engine) -> Set[str]:
    """Schemas an extension owns.

    Two sources, and the first one alone is not enough -- found by installing
    PostGIS. `pg_extension.extnamespace` is the schema an extension lives in,
    which is how `postgis_topology` gets its `topology` schema; `pg_depend`
    catches a schema an extension's script created without living in. A
    database with no extensions returns an empty set.
    """
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("""
            SELECT n.nspname
              FROM pg_extension e
              JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE n.nspname NOT IN ('pg_catalog', 'public')
            UNION
            SELECT n.nspname
              FROM pg_depend d
              JOIN pg_extension e ON e.oid = d.refobjid
              JOIN pg_namespace n ON n.oid = d.objid
             WHERE d.refclassid = 'pg_extension'::regclass
               AND d.classid    = 'pg_namespace'::regclass
        """).fetchall()
    return {r[0] for r in rows}


def maintained_objects(engine) -> Set[tuple]:
    """Tables and views an extension owns inside a user schema.

    An extension installed into `public` -- which is the default, and what
    PostGIS does -- leaves its own tables among the user's. `spatial_ref_sys`
    is 8,500 rows of map projections; `geometry_columns` and
    `geography_columns` are catalog views. None of them are anybody's data,
    and excluding the schema is not an option because the schema is `public`.
    """
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("""
            SELECT n.nspname, c.relname
              FROM pg_depend d
              JOIN pg_extension e ON e.oid = d.refobjid
              JOIN pg_class     c ON c.oid = d.objid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE d.refclassid = 'pg_extension'::regclass
               AND d.classid    = 'pg_class'::regclass
               AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
        """).fetchall()
    return {(r[0], r[1]) for r in rows}


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


#: One catalog query per engine. Reflecting 404 tables must not mean 404
#: round trips -- the whole point of the index is that it is built once.
_TYPE_CACHE: "Dict[int, Dict[tuple, str]]" = {}

#: ``format_type`` is what gives back the name the user would write:
#: ``geometry(Point,4326)``, not the NULL SQLAlchemy reports for a type it
#: has no class for. The CASE carries the one extra fact each awkward kind
#: needs -- an enum's labels, a domain's base type -- so ``_render`` can put
#: something useful in the prompt instead of ``VARCHAR(5)`` or ``DOMAIN``.
#:
#: The prefix tests use ``left()`` rather than the ``LIKE 'pg_toast%'`` they
#: obviously want to be. This goes through ``exec_driver_sql``, which hands
#: the string to the driver unchanged, and psycopg3 reads ``%`` as the start
#: of a placeholder: the whole query fails with "only '%s', '%b', '%t' are
#: allowed as placeholders". Escaping it as ``%%`` would work on psycopg and
#: break on any driver that does not do that substitution, so the query
#: simply contains no percent sign.
_TYPES_SQL = """
    SELECT n.nspname,
           c.relname,
           a.attname,
           format_type(a.atttypid, a.atttypmod),
           t.typtype,
           CASE t.typtype
             WHEN 'e' THEN (SELECT string_agg(quote_literal(e.enumlabel), ', '
                                              ORDER BY e.enumsortorder)
                              FROM pg_enum e WHERE e.enumtypid = t.oid)
             WHEN 'd' THEN format_type(t.typbasetype, t.typtypmod)
           END
      FROM pg_attribute a
      JOIN pg_class     c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_type      t ON t.oid = a.atttypid
     WHERE a.attnum > 0
       AND NOT a.attisdropped
       AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND left(n.nspname, 8) <> 'pg_toast'
       AND left(n.nspname, 7) <> 'pg_temp'
"""


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
    known = _catalog_types(engine)
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
MAINTAINED_OBJECTS["postgresql"] = maintained_objects
INTERNAL_SCHEMA["postgresql"] = looks_internal
FILL_UNKNOWN_TYPES["postgresql"] = unknown_types
