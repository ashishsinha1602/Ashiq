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


def reflect(engine_or_url, include=None, exclude=None,
            schemas: Optional[List[str]] = None,
            include_views: bool = True) -> List[ObjectDoc]:
    """Return an ObjectDoc per table/view. ``include``/``exclude`` accept glob
    or SQL-LIKE style patterns ('sales_%', 'v_*')."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(engine_or_url) if isinstance(engine_or_url, str) else engine_or_url
    insp = inspect(engine)

    if schemas is None:
        try:
            schemas = [s for s in insp.get_schema_names()
                       if s.lower() not in _SYSTEM_SCHEMAS]
        except NotImplementedError:
            schemas = [None]
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
                raw_cols = insp.get_columns(name, schema=schema)
            except Exception:
                continue
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
