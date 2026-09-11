"""Reflect an Autonomous Database over ORDS, on HTTPS.

Why this exists: SQL*Net wants port 1522, and a great many corporate networks
do not give it one. The failure is not subtle to live with -- packets vanish
and the driver reports "cannot connect" a timeout later, or a proxy accepts
the connection and drops it mid-handshake and the driver reports that the
database closed it. Neither is true and neither is fixable from here.

Meanwhile every Autonomous Database publishes Database Actions on 443, and the
same box that cannot reach 1522 loads that page fine. ORDS will run a
statement and hand back JSON, so the catalog can be read that way instead: the
same `ObjectDoc` list `introspect.reflect` produces, built from `USER_*` views
rather than from SQLAlchemy's inspector.

The trade is real and worth stating. This reads the catalog only -- no row
counts, no sampled values, and no running of the SQL a model writes, because
those need a connection this path does not have. What it does give you is a
catalog on a machine that otherwise has nothing at all.

Nothing here imports a driver. It is `urllib` and the standard library, so it
works on a bare `pip install schemagate`.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from .models import Column, ForeignKey, ObjectDoc

__all__ = ["OrdsError", "ords_base", "reflect_ords", "run_ords_sql"]


class OrdsError(RuntimeError):
    """ORDS refused, or answered with something that is not a result."""


#: The catalog, in one request. ORDS runs a script and returns one item per
#: statement, so this is a single round trip rather than six -- which matters
#: when the round trip is to another continent.
_SCRIPT = """
SELECT table_name AS name, 'TABLE' AS kind FROM user_tables;
SELECT view_name AS name, 'VIEW' AS kind FROM user_views;
SELECT table_name, column_name, data_type, data_length, data_precision,
       data_scale, nullable, column_id
  FROM user_tab_columns ORDER BY table_name, column_id;
SELECT cc.table_name, cc.column_name
  FROM user_constraints c
  JOIN user_cons_columns cc ON c.constraint_name = cc.constraint_name
 WHERE c.constraint_type = 'P';
SELECT c.table_name, cc.column_name, rc.table_name AS ref_table,
       rcc.column_name AS ref_col
  FROM user_constraints c
  JOIN user_cons_columns cc
    ON c.constraint_name = cc.constraint_name AND c.constraint_type = 'R'
  JOIN user_constraints rc ON c.r_constraint_name = rc.constraint_name
  JOIN user_cons_columns rcc
    ON rc.constraint_name = rcc.constraint_name AND rcc.position = cc.position;
SELECT table_name, comments FROM user_tab_comments WHERE comments IS NOT NULL;
SELECT table_name, column_name, comments
  FROM user_col_comments WHERE comments IS NOT NULL;
"""


def ords_base(url: str) -> str:
    """The ``/ords`` root, from whatever someone pasted.

    The link people have is the one in the wallet's README or the address bar
    of Database Actions -- ``.../ords/sql-developer``, or a sign-in URL with a
    query string on it. Trimming that back here means the form can accept the
    thing they already have rather than asking them to construct a base URL.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise OrdsError("no ORDS URL given")
    if "://" not in raw:
        raw = "https://" + raw
    raw = raw.split("?", 1)[0].rstrip("/")
    marker = "/ords"
    i = raw.find(marker)
    if i == -1:
        return raw + "/ords"
    return raw[:i + len(marker)]


def _post(base: str, schema: str, user: str, password: str,
          sql: str, timeout: int = 60) -> List[Dict[str, Any]]:
    endpoint = f"{base}/{schema.lower()}/_/sql"
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        endpoint, data=sql.encode("utf-8"),
        headers={"Content-Type": "application/sql",
                 "Accept": "application/json",
                 "Authorization": f"Basic {token}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        # Never the body: an ORDS error page can echo the statement, and a
        # statement can carry a literal.
        hint = {401: " -- check the user and password",
                404: (" -- is the schema REST-enabled? As ADMIN: "
                      "BEGIN ORDS_ADMIN.ENABLE_SCHEMA(p_schema => "
                      f"'{schema.upper()}'); END;")}.get(e.code, "")
        raise OrdsError(f"ORDS returned HTTP {e.code}{hint}") from None
    except urllib.error.URLError as e:
        raise OrdsError(f"could not reach ORDS: {e.reason}") from None
    except json.JSONDecodeError:
        raise OrdsError("ORDS did not return JSON -- is that the /ords URL?") from None

    items = payload.get("items")
    if not isinstance(items, list):
        raise OrdsError("ORDS returned no statement results")
    for it in items:
        if it.get("errorDetails"):
            raise OrdsError(str(it["errorDetails"]).split("\n")[0][:200])
    return items


def run_ords_sql(base: str, schema: str, user: str, password: str,
                 sql: str) -> Tuple[List[str], List[List[Any]]]:
    """One statement, as ``(columns, rows)``. Used by the tests and by
    anything that wants to ask this database a question over HTTPS."""
    items = _post(base, schema, user, password, sql)
    for it in items:
        rs = it.get("resultSet")
        if rs:
            cols = [m.get("jsonColumnName") or m.get("columnName")
                    for m in rs.get("metadata", [])]
            return cols, [[r.get(c) for c in cols] for r in rs.get("items", [])]
    return [], []


def _rows(items: List[Dict[str, Any]], i: int) -> List[Dict[str, Any]]:
    """The rows of the i-th statement. A statement that matched nothing comes
    back without a resultSet rather than with an empty one."""
    if i >= len(items):
        return []
    rs = items[i].get("resultSet")
    return list(rs.get("items", [])) if rs else []


def _render(dtype: Any, length: Any, prec: Any, scale: Any) -> str:
    """The type as someone would write it in DDL. Deliberately the same rules
    as the oracle dialect uses over SQL*Net -- a catalog read one way must not
    describe a column differently from the same catalog read the other."""
    t = str(dtype or "")
    if t in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW") and length:
        return f"{t}({length})"
    if t == "NUMBER" and prec:
        return f"NUMBER({prec},{scale or 0})"
    return t


def reflect_ords(url: str, schema: str, user: str, password: str,
                 include_views: bool = True,
                 timeout: int = 60) -> List[ObjectDoc]:
    """``ObjectDoc`` per table and view, read over HTTPS instead of SQL*Net."""
    base = ords_base(url)
    schema = (schema or user or "").strip()
    if not schema:
        raise OrdsError("a schema is required (the REST-enabled user, e.g. APPUSER)")
    items = _post(base, schema, user, password, _SCRIPT, timeout=timeout)

    kinds: Dict[str, str] = {}
    for r in _rows(items, 0):
        kinds[str(r["name"])] = "TABLE"
    if include_views:
        for r in _rows(items, 1):
            kinds[str(r["name"])] = "VIEW"

    cols: Dict[str, List[Column]] = {}
    for r in _rows(items, 2):
        t = str(r["table_name"])
        if t not in kinds:
            continue
        cols.setdefault(t, []).append(Column(
            name=str(r["column_name"]),
            type=_render(r.get("data_type"), r.get("data_length"),
                         r.get("data_precision"), r.get("data_scale")),
            nullable=str(r.get("nullable") or "Y").upper() != "N",
        ))

    pk = {(str(r["table_name"]), str(r["column_name"])) for r in _rows(items, 3)}
    for t, cl in cols.items():
        for c in cl:
            if (t, c.name) in pk:
                c.pk = True

    fks: Dict[str, Dict[str, ForeignKey]] = {}
    for r in _rows(items, 4):
        t, ref = str(r["table_name"]), str(r["ref_table"])
        if t not in kinds:
            continue
        fk = fks.setdefault(t, {}).setdefault(ref, ForeignKey(columns=[], ref_table=ref))
        fk.columns.append(str(r["column_name"]))
        fk.ref_columns.append(str(r["ref_col"]))

    tcom = {str(r["table_name"]): str(r["comments"]) for r in _rows(items, 5)}
    ccom = {(str(r["table_name"]), str(r["column_name"])): str(r["comments"])
            for r in _rows(items, 6)}
    for (t, cname), text in ccom.items():
        for c in cols.get(t, []):
            if c.name == cname:
                c.comment = text

    out: List[ObjectDoc] = []
    for name, kind in sorted(kinds.items()):
        out.append(ObjectDoc(
            name=name, schema=schema.upper(), kind=kind,
            description=tcom.get(name),
            columns=cols.get(name, []),
            foreign_keys=list(fks.get(name, {}).values()),
        ))
    return out
