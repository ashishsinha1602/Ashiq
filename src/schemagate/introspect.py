"""Dialect-agnostic schema reflection via SQLAlchemy.

Works on anything with a SQLAlchemy dialect -- postgres, oracle, mysql,
sqlite, duckdb, snowflake, mssql. No vendor SQL, no assumptions.
"""
from __future__ import annotations

import re

from typing import Iterable, List, Optional, Sequence

from .models import Column, ForeignKey, ObjectDoc

_SYSTEM_SCHEMAS = {
    "information_schema", "pg_catalog", "pg_toast", "sys", "mysql",
    "performance_schema", "sysaux", "system", "audsys", "ctxsys",
    "mdsys", "olapsys", "ordsys", "xdb", "wmsys", "dbsnmp", "outln",
    "lbacsys", "dvsys", "gsmadmin_internal", "appqossys",
}


def _match(name: str, patterns: Optional[Iterable[str]]) -> bool:
    if not patterns:
        return True
    import fnmatch
    n = name.lower()
    return any(fnmatch.fnmatch(n, p.lower().replace("%", "*")) for p in patterns)



def connect_args_from_env() -> dict:
    """Driver keyword arguments for :func:`sqlalchemy.create_engine`, read from
    the ``SCHEMAGATE_CONNECT_ARGS`` environment variable.

    Some databases cannot be described by a URL alone. Oracle Autonomous
    Database is the usual case: the connection needs a wallet directory and a
    wallet password, which have no place in a URL, so the URL degenerates to
    ``oracle+oracledb://@`` and everything else travels here::

        export SCHEMAGATE_CONNECT_ARGS='{"config_dir": "./wallet",
                                         "wallet_location": "./wallet",
                                         "wallet_password": "...",
                                         "user": "ADMIN", "password": "...",
                                         "dsn": "mydb_high"}'

    Returns an empty dict when the variable is unset. A value that is not
    valid JSON, or is valid JSON but not an object, raises ``ValueError`` --
    silently ignoring a malformed value would surface later as a confusing
    authentication failure.
    """
    import json
    import os

    raw = os.environ.get("SCHEMAGATE_CONNECT_ARGS")
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            "SCHEMAGATE_CONNECT_ARGS is not valid JSON: %s" % exc
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "SCHEMAGATE_CONNECT_ARGS must be a JSON object, got %s"
            % type(parsed).__name__
        )
    return parsed


def engine_from_url(url: str, **kwargs):
    """``create_engine(url)`` with :func:`connect_args_from_env` merged in.

    Explicit ``connect_args`` passed by the caller win over the environment,
    key by key.
    """
    from sqlalchemy import create_engine

    env_args = connect_args_from_env()
    if env_args:
        merged = dict(env_args)
        merged.update(kwargs.pop("connect_args", None) or {})
        kwargs["connect_args"] = merged
    return create_engine(url, **kwargs)


#: Only short string columns are candidates. A `VARCHAR(30)` that holds four
#: values is a category; a `VARCHAR(4000)` is prose and asking for its
#: distinct values is a table scan that returns nothing useful. The bound is
#: structural rather than a list of names like "status" or "type", because
#: the interesting column on someone else's schema is called something this
#: file has never heard of.
_SAMPLEABLE = re.compile(r"^(VARCHAR|VARCHAR2|NVARCHAR|NVARCHAR2|CHAR|NCHAR|TEXT)"
                         r"(\((\d+)[^)]*\))?$", re.I)
_MAX_WIDTH = 40


#: Column names whose contents are personal by default. Matched on the name
#: as a substring, case-insensitively, so `customer_email` and `EMAILADDR`
#: both match.
#:
#: This is a deny-list, which means it is wrong by construction: it will miss
#: `nachname`, `nino`, `mrn`. It is here because the alternative was no guard
#: at all -- `--values` sent real names and home addresses to whichever model
#: the user had configured, and nothing in the code said otherwise. Treat it
#: as a floor, not a boundary: the guarantees are `restrict_column`, which is
#: exact, and the fact that this is off by default.
_PII_NAMES = ("name", "address", "email", "phone", "ssn", "dob", "birth",
              "passport")

#: A column whose values are nearly unique is an identifier, not a category,
#: whatever it is called. Four distinct values in a four-row table says only
#: that the table is small. Requiring the distinct count to be well under the
#: row count is what stops a two-row table qualifying for anything.
_MAX_DISTINCT_RATIO = 5


def _looks_personal(name: str, deny: "Sequence[str]") -> bool:
    low = name.casefold()
    return any(p in low for p in deny)


def _candidates(raw_cols, pk_names, deny=_PII_NAMES) -> "List[str]":
    """Columns worth one small query each.

    Declared width used to decide this on its own, and unbounded TEXT was
    skipped as "assume prose". That silently disabled the whole feature for
    SQLite, where type affinity declares almost everything TEXT, and for the
    many PostgreSQL schemas that use `text` by convention instead of
    `varchar(n)`. Those users got nothing, with nothing to say why.

    So a declared width still rules a column out when it is wide -- a
    VARCHAR(4000) really is prose and there is no reason to ask -- but no
    declared width no longer rules one out. An unbounded column is asked, and
    then judged on what actually came back, in `_sample_values`. The cost is
    the same one bounded query either way.
    """
    out = []
    for c in raw_cols:
        if c["name"] in pk_names:          # a key is not a category
            continue
        # A column somebody has already restricted must never have its
        # contents read, let alone rendered. Reflection rarely knows about it
        # -- `restrict_column` is usually called afterwards, and clears any
        # values that were already sampled -- but when a caller reflects into
        # an existing catalog it does, and then this is the cheaper stop.
        if c.get("roles"):
            continue
        if _looks_personal(c["name"], deny):
            continue
        m = _SAMPLEABLE.match(str(c["type"]).strip())
        if not m:
            continue
        width = m.group(3)
        if width is not None and int(width) > _MAX_WIDTH:
            continue
        out.append(c["name"])
    return out


def _sample_values(engine, schema, table, raw_cols, kind, max_distinct,
                   deny=_PII_NAMES):
    """The distinct values of short string columns, when there are few.

    This exists because of a specific wrong answer, and it is worth stating
    plainly: a model was handed `status VARCHAR(30)` and wrote
    `WHERE status = 'DENIED'`. The rows say `denied`. The query was correct
    in every way a schema can express, and returned nothing -- which reads as
    "there are no denied claims", not as a mistake. Nothing in a catalog can
    tell a model the casing of a value it has never seen.

    Bounded on purpose, because this is the only place the library reads
    rows: short string columns only, one query each, `LIMIT max_distinct + 1`
    so a high-cardinality column costs one small query and is then dropped
    rather than pulled into memory. A column that fails -- no privilege, a
    view that cannot be scanned -- is skipped, not fatal: this is an
    enrichment, and reflection must still finish without it.
    """
    from sqlalchemy import Column as SAColumn, MetaData, Table, func, select

    pk_names = {c["name"] for c in raw_cols if c.get("primary_key")}
    names = _candidates(raw_cols, pk_names, deny)
    if not names:
        return None

    md = MetaData()
    tbl = Table(table, md, *[SAColumn(n, None) for n in names], schema=schema)
    found = {}
    with engine.connect() as conn:
        # One count, reused for every column: a distinct count only means
        # "category" relative to how many rows there are.
        try:
            rows_total = conn.execute(
                select(func.count()).select_from(tbl)).scalar() or 0
        except Exception:
            rows_total = 0
        for n in names:
            col = tbl.c[n]
            try:
                rows = conn.execute(
                    select(col).where(col.is_not(None))
                               .distinct().limit(max_distinct + 1)).fetchall()
            except Exception:
                continue
            if not rows or len(rows) > max_distinct:
                continue
            vals = sorted(str(r[0]) for r in rows)
            # Judged on what came back, not on what was declared. An unbounded
            # TEXT column holding four short codes is a category; one holding a
            # paragraph is prose, and the paragraph is what says so.
            if any(len(v) > _MAX_WIDTH for v in vals):
                continue
            # And nearly-unique means identifier, not category. Without this a
            # three-row employee table hands over three full names, because
            # three distinct values is under any fixed cap.
            if rows_total and len(vals) * _MAX_DISTINCT_RATIO >= rows_total:
                continue
            found[n] = vals
    return found or None


def reflect(engine_or_url, include=None, exclude=None,
            schemas: Optional[List[Optional[str]]] = None,
            include_views: bool = True,
            sample_values: bool = False,
            max_distinct: int = 25,
            deny_columns: Optional[Sequence[str]] = None) -> List[ObjectDoc]:
    """Return an ObjectDoc per table/view. ``include``/``exclude`` accept glob
    or SQL-LIKE style patterns ('sales_%', 'v_*').

    ``sample_values`` is the one option here that reads rows rather than the
    catalog, which is why it is off by default. It fills in ``Column.values``
    for short string columns that turn out to hold only a handful of distinct
    values -- a status, a code, a category. See ``_sample_values`` for why
    that is worth a query.
    """
    from sqlalchemy import inspect

    engine = (engine_from_url(engine_or_url)
              if isinstance(engine_or_url, str) else engine_or_url)
    insp = inspect(engine)

    if schemas is None:
        # `None` is a real value here, not a missing one: a dialect with no
        # schema concept reflects everything under it, and the reflection loop
        # passes it straight to get_table_names(schema=None).
        found: List[Optional[str]]
        try:
            found = [s for s in insp.get_schema_names()
                     if s.lower() not in _SYSTEM_SCHEMAS]
        except NotImplementedError:
            found = [None]
        schemas = found
        # Anything beyond that is vendor-specific, and has to be: "PUBLIC" is a
        # pseudo-schema on Oracle and the user's entire database on PostgreSQL,
        # so one shared list of "internal-looking" names empties one engine's
        # catalog in order to tidy up another's.
        from .dialects import is_internal_schema, vendor_maintained
        maintained = vendor_maintained(engine)
        schemas = [s for s in schemas
                   if s not in maintained and not is_internal_schema(engine, s)]
        default = insp.default_schema_name
        if default and default in schemas:
            schemas = [default] + [s for s in schemas if s != default]

    from .dialects import vendor_maintained_objects
    # Excluding whole schemas is not enough: an extension installed into a
    # user schema leaves its own tables sitting there among the user's. PostGIS
    # puts spatial_ref_sys -- 8,500 rows of map projections -- into public.
    maintained_objects = vendor_maintained_objects(engine)
    # The hook reports the schema the server records, which is never NULL, so
    # a reflection that did not name a schema is matched against the default
    # rather than a literal "public" -- that word means something else on
    # Oracle, and hardcoding it here is what the per-dialect registry exists
    # to avoid. Only asked for when there is something to match: a dialect
    # with no hook returns an empty set without connecting, and this keeps
    # the reflection path for those engines exactly as it was.
    default_schema = insp.default_schema_name if maintained_objects else None
    docs: List[ObjectDoc] = []
    seen = set()
    for schema in schemas:
        names = [(n, "TABLE") for n in insp.get_table_names(schema=schema)]
        if include_views:
            names += [(n, "VIEW") for n in insp.get_view_names(schema=schema)]
        for name, kind in names:
            if (schema, name) in seen:
                continue
            seen.add((schema, name))
            if (schema or default_schema, name) in maintained_objects:
                continue
            if not _match(name, include) or (exclude and _match(name, exclude)):
                continue
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Did not recognize type")
                    raw_cols = insp.get_columns(name, schema=schema)
            except Exception:
                continue
            from .dialects import fill_unknown_types
            fill_unknown_types(engine, schema, name, raw_cols)
            try:
                pk = set(insp.get_pk_constraint(name, schema=schema
                                                ).get("constrained_columns") or [])
            except Exception:
                pk = set()
            try:
                raw_fks = insp.get_foreign_keys(name, schema=schema)
            except Exception:
                raw_fks = []
            try:
                comment = (insp.get_table_comment(name, schema=schema) or {}).get("text")
            except Exception:
                comment = None

            definition = None
            if kind == "VIEW":
                try:
                    definition = insp.get_view_definition(name, schema=schema)
                except Exception:
                    definition = None

            values = (_sample_values(engine, schema, name, raw_cols, kind,
                                     max_distinct,
                                     _PII_NAMES if deny_columns is None
                                     else tuple(deny_columns))
                      if sample_values else None)
            docs.append(ObjectDoc(
                name=name,
                schema=schema,
                kind=kind,
                description=comment,
                definition=definition,
                columns=[Column(name=c["name"], type=str(c["type"]),
                                nullable=bool(c.get("nullable", True)),
                                comment=c.get("comment"), pk=c["name"] in pk,
                                values=(values or {}).get(c["name"]))
                         for c in raw_cols],
                foreign_keys=[ForeignKey(columns=list(f.get("constrained_columns") or []),
                                         ref_table=f.get("referred_table") or "",
                                         ref_columns=list(f.get("referred_columns") or []))
                              for f in raw_fks if f.get("referred_table")],
            ))
    return docs
