"""Oracle: what the Inspector cannot tell us, found live on 26ai.

* An Autonomous Database exposes ~1,500 objects to ADMIN, of which the
  user's own are a few dozen; the rest belong to APEX, ORDS, the OCI service
  layer and Oracle itself. 12c+ marks those users ORACLE_MAINTAINED.
* SQLAlchemy reports XMLTYPE, JSON, SDO_GEOMETRY, object types and VECTOR
  as NULL with a warning. The prompt needs the real name.
"""
from __future__ import annotations

from typing import List, Optional, Set

from . import FILL_UNKNOWN_TYPES, MAINTAINED_SCHEMAS


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


MAINTAINED_SCHEMAS["oracle"] = maintained_schemas
FILL_UNKNOWN_TYPES["oracle"] = unknown_types
