"""Oracle: what the Inspector cannot tell us, found live on 26ai.

* An Autonomous Database exposes ~1,500 objects to ADMIN, of which the
  user's own are a few dozen; the rest belong to APEX, ORDS, the OCI service
  layer and Oracle itself. 12c+ marks those users ORACLE_MAINTAINED.
* SQLAlchemy reports XMLTYPE, JSON, SDO_GEOMETRY, object types and VECTOR
  as NULL with a warning. The prompt needs the real name.
"""
from __future__ import annotations

from typing import List, Optional, Set

from . import FILL_UNKNOWN_TYPES, INTERNAL_SCHEMA, MAINTAINED_SCHEMAS


def maintained_schemas(engine) -> Set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT username FROM all_users WHERE oracle_maintained = 'Y'"
        ).fetchall()
    return {r[0] for r in rows}


def unknown_types(engine, schema: Optional[str], table: str, columns: List[dict]) -> None:
    unknown = [c for c in columns if str(c.get("type")).upper() in ("NULL", "NULLTYPE")]
    if not unknown:
        return
    owner = (schema or engine.dialect.default_schema_name or "").upper()
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT column_name, data_type, data_length, data_precision, data_scale "
            "FROM all_tab_columns WHERE owner = :o AND table_name = :t",
            {"o": owner, "t": table.upper()},
        ).fetchall()
    types = {}
    for name, dtype, length, prec, scale in rows:
        t = str(dtype)
        if t in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and length:
            t = f"{t}({length})"
        elif t == "NUMBER" and prec:
            t = f"NUMBER({prec},{scale or 0})"
        types[str(name).lower()] = t          # VECTOR(512, FLOAT32) comes back whole
    for c in unknown:
        real = types.get(str(c["name"]).lower())
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
