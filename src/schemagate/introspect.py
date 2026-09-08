"""Dialect-agnostic schema reflection via SQLAlchemy.

Works on anything with a SQLAlchemy dialect -- postgres, oracle, mysql,
sqlite, duckdb, snowflake, mssql. No vendor SQL, no assumptions.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

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


def reflect(engine_or_url, include=None, exclude=None,
            schemas: Optional[List[str]] = None,
            include_views: bool = True) -> List[ObjectDoc]:
    """Return an ObjectDoc per table/view. ``include``/``exclude`` accept glob
    or SQL-LIKE style patterns ('sales_%', 'v_*')."""
    from sqlalchemy import inspect

    engine = (engine_from_url(engine_or_url)
              if isinstance(engine_or_url, str) else engine_or_url)
    insp = inspect(engine)

    if schemas is None:
        try:
            schemas = [s for s in insp.get_schema_names()
                       if s.lower() not in _SYSTEM_SCHEMAS]
        except NotImplementedError:
            schemas = [None]
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

            docs.append(ObjectDoc(
                name=name,
                schema=schema,
                kind=kind,
                description=comment,
                definition=definition,
                columns=[Column(name=c["name"], type=str(c["type"]),
                                nullable=bool(c.get("nullable", True)),
                                comment=c.get("comment"), pk=c["name"] in pk)
                         for c in raw_cols],
                foreign_keys=[ForeignKey(columns=list(f.get("constrained_columns") or []),
                                         ref_table=f.get("referred_table") or "",
                                         ref_columns=list(f.get("referred_columns") or []))
                              for f in raw_fks if f.get("referred_table")],
            ))
    return docs
