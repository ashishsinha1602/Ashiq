"""Oracle: what the Inspector cannot tell us, found live on 26ai.

* An Autonomous Database exposes ~1,500 objects to ADMIN, of which the
  user's own are a few dozen; the rest belong to APEX, ORDS, the OCI service
  layer and Oracle itself. 12c+ marks those users ORACLE_MAINTAINED.
* SQLAlchemy reports XMLTYPE, JSON, SDO_GEOMETRY, object types and VECTOR
  as NULL with a warning. The prompt needs the real name.
"""
from __future__ import annotations

from typing import Any, Dict, List, MutableMapping, Optional, Set
from weakref import WeakKeyDictionary

from . import FILL_UNKNOWN_TYPES, INTERNAL_SCHEMA, MAINTAINED_SCHEMAS


def maintained_schemas(engine) -> Set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT username FROM all_users WHERE oracle_maintained = 'Y'"
        ).fetchall()
    return {r[0] for r in rows}


#: engine -> {(owner, table, column): rendered type}. One query per engine,
#: not one per table. The PostgreSQL module pays this cost against a socket
#: on the same machine; here it is a round trip to a cloud database, so the
#: per-table version was the more expensive of the two by a wide margin.
#:
#: Keyed on the engine object, weakly. The obvious `id(engine)` is wrong in a
#: way that is quiet and awful: CPython reuses the id of a collected object,
#: so an engine opened after an earlier one was garbage collected can land on
#: the same key and be handed the *previous database's* column types without
#: issuing a single query. Reproduced in a loop -- a fresh engine got
#: `geometry(Point,4326)` for a column that was `integer`. A weak key cannot
#: collide, because the entry cannot outlive the engine it describes, and it
#: also stops the cache growing forever in a process that opens engines.
_TYPE_CACHE: "MutableMapping[Any, Dict[tuple, str]]" = WeakKeyDictionary()

#: Scoped to the user's own schemas by the same signal `maintained_schemas`
#: uses, which is what keeps this to a few thousand rows on an ADB that
#: exposes ~1,500 objects to ADMIN. Both views are already proven on 26ai --
#: this module has queried them since it was written -- so the one query
#: introduces no catalog access that was not already working.
_TYPES_SQL = """
    SELECT owner, table_name, column_name,
           data_type, data_length, data_precision, data_scale
      FROM all_tab_columns
     WHERE owner NOT IN (SELECT username FROM all_users
                          WHERE oracle_maintained = 'Y')
"""


def _render(dtype, length, prec, scale) -> str:
    """The name as the user would write it in DDL."""
    t = str(dtype)
    if t in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and length:
        return f"{t}({length})"
    if t == "NUMBER" and prec:
        return f"NUMBER({prec},{scale or 0})"
    return t                       # VECTOR(512, FLOAT32) comes back whole


def _catalog_types(engine) -> "Dict[tuple, str]":
    cached = _TYPE_CACHE.get(engine)
    if cached is not None:
        return cached
    out: "Dict[tuple, str]" = {}
    with engine.connect() as conn:
        for owner, table, col, dtype, length, prec, scale in conn.exec_driver_sql(
                _TYPES_SQL).fetchall():
            out[(str(owner).upper(), str(table).upper(), str(col).lower())] = \
                _render(dtype, length, prec, scale)
    _TYPE_CACHE[engine] = out
    return out


def unknown_types(engine, schema: Optional[str], table: str, columns: List[dict]) -> None:
    """Give XMLTYPE, JSON, SDO_GEOMETRY, object types and VECTOR their names.

    Unlike the PostgreSQL module, this can return without asking the database
    when nothing reads NULL, and the asymmetry is not an oversight. On
    PostgreSQL an enum reflects as ``VARCHAR(5)`` -- a settled-looking type
    that is nonetheless wrong -- so only the catalog can say whether a column
    needs correcting. Oracle has no equivalent: the types SQLAlchemy cannot
    render come back as NULL, and a column that reads anything else is right.
    """
    unknown = [c for c in columns if str(c.get("type")).upper() in ("NULL", "NULLTYPE")]
    if not unknown:
        return
    known = _catalog_types(engine)
    owner = (schema or engine.dialect.default_schema_name or "").upper()
    for c in unknown:
        real = known.get((owner, table.upper(), str(c["name"]).lower()))
        if real:
            c["type"] = real


#: Schema-name shapes that are Oracle platform plumbing: APEX (APEX_230200,
#: FLOWS_FILES), ORDS, common users (C##...), Autonomous Database service
#: schemas, and anything with a $ in it. PUBLIC is Oracle's pseudo-schema --
#: which is exactly why this list must never be applied to another engine.
_INTERNAL_PREFIXES = ("apex_", "flows_", "ords_", "c##", "sys$", "db_", "ggsys",
                      "ojvmsys", "dvsys", "dvf", "lbacsys", "dbsfwuser", "rqsys",
                      "pyqsys", "graph$", "mtssys", "adbsnmp", "oci_admin",
                      "sh$", "ssb$", "remote_scheduler_agent", "audsys",
                      "cloud$", "gsmuser", "gsmcatuser", "gsmrofuser", "xs$null",
                      "dip", "anonymous", "public",
                      "odi_repo", "oadc_", "oml$", "omlmod$", "dcat_", "adp_")


def looks_internal(schema: str) -> bool:
    low = schema.lower()
    return "$" in low or any(low.startswith(p) for p in _INTERNAL_PREFIXES)


MAINTAINED_SCHEMAS["oracle"] = maintained_schemas
INTERNAL_SCHEMA["oracle"] = looks_internal
FILL_UNKNOWN_TYPES["oracle"] = unknown_types
