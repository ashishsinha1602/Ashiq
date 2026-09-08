"""Persistent Store backed by Oracle Database 23ai native VECTOR.

Why 23ai and not a BLOB of floats: ``VECTOR`` is a first-class type with
``VECTOR_DISTANCE()`` evaluated in the database, so the k-nearest search
runs server-side over the whole catalog instead of shipping every row to
Python. On 23ai you can also add an HNSW index and keep the same SQL.

Identity scoping is enforced in the WHERE clause, not in Python, so a
restricted row is never fetched at all::

    from schemagate import Catalog, Principal
    from schemagate.stores.oracle import OracleStore

    store = OracleStore(dsn="user/pw@host:1521/FREEPDB1")
    store.create_schema()                      # once, DDL
    cat = Catalog(store=store).bootstrap("oracle+oracledb://...")

Requires ``pip install 'schemagate[oracle]'``. Every statement here is plain
SQL against one table; nothing depends on an Oracle-specific Python API
beyond the driver's connection object.
"""
from __future__ import annotations

import array
import json
from typing import Any, Dict, List, Optional, Sequence

_DEFAULT_TABLE = "SCHEMAGATE_VECTORS"

# One row per (namespace, key). ``scope`` NULL means "visible to everyone";
# a non-NULL scope is a Principal.scope() digest, never a username.
_DDL = """
CREATE TABLE {table} (
  ns        VARCHAR2(200)  NOT NULL,
  vkey      VARCHAR2(400)  NOT NULL,
  scope     VARCHAR2(64),
  embedding VECTOR({dim}, FLOAT32) NOT NULL,
  payload   CLOB CHECK (payload IS JSON),
  CONSTRAINT {table}_pk PRIMARY KEY (ns, vkey)
)
"""

_INDEX = """
CREATE INDEX {table}_scope_ix ON {table} (ns, scope)
"""


class OracleStore:
    """Store protocol over a single Oracle 23ai table.

    Parameters
    ----------
    dsn / user / password
        Passed straight to ``oracledb.connect``. Ignored if ``connection``
        is given.
    connection
        An existing ``oracledb`` connection or pool-checked-out connection.
        Use this to share the app's pool rather than opening another.
    dim
        Vector dimension. Must match the embedder; ``Catalog`` will raise
        on mismatch rather than silently truncating.
    """

    def __init__(self, dsn: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None, connection: Any = None,
                 table: str = _DEFAULT_TABLE, dim: int = 512,
                 distance: str = "COSINE", **connect_kwargs: Any):
        if connection is None and dsn is None:
            raise ValueError("OracleStore needs either connection= or dsn=")
        if not table.replace("_", "").isalnum():
            # this value is interpolated into DDL, so it must be an identifier
            raise ValueError(f"unsafe table name {table!r}")
        if distance.upper() not in {"COSINE", "EUCLIDEAN", "DOT", "MANHATTAN"}:
            raise ValueError(f"unsupported distance {distance!r}")
        self.table = table.upper()
        self.dim = int(dim)
        self.distance = distance.upper()
        self._owns_conn = connection is None
        if connection is not None:
            self._conn = connection
        else:
            try:
                import oracledb
            except ImportError as e:  # pragma: no cover - import guard
                raise ImportError(
                    "pip install 'schemagate[oracle]' to use OracleStore"
                ) from e
            # Autonomous Database needs a wallet directory and password that
            # have no place in dsn/user/password. They arrive either as
            # keyword arguments here or through SCHEMAGATE_CONNECT_ARGS, the
            # same JSON every other entry point honours. Explicit wins.
            from ..introspect import connect_args_from_env
            kwargs = dict(connect_args_from_env())
            kwargs.update(connect_kwargs)
            for k, v in (("user", user), ("password", password), ("dsn", dsn)):
                if v is not None:
                    kwargs[k] = v
            self._conn = oracledb.connect(**kwargs)

    # ---------------- helpers ----------------

    @staticmethod
    def _vec(values: Sequence[float]) -> "array.array":
        """oracledb binds array('f') straight to VECTOR(*, FLOAT32)."""
        return array.array("f", [float(v) for v in values])

    def _cursor(self):
        return self._conn.cursor()

    @staticmethod
    def _read_lob(value: Any) -> Any:
        return value.read() if hasattr(value, "read") else value

    @classmethod
    def _payload(cls, value: Any) -> dict:
        """Decode a payload however the driver hands it back.

        Found live on 26ai: a CLOB CHECK (IS JSON) column can come back as a
        LOB, a str, bytes, or -- with python-oracledb 4 and native JSON
        handling -- an already-decoded dict. All four must land as a dict.
        """
        value = cls._read_lob(value)
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        return json.loads(value)

    # ---------------- schema ----------------

    def create_schema(self, with_index: bool = True) -> None:
        """Create the table if absent. Safe to call repeatedly."""
        with self._cursor() as cur:
            try:
                cur.execute(_DDL.format(table=self.table, dim=self.dim))
            except Exception as e:  # ORA-00955: name is already used
                if "ORA-00955" not in str(e):
                    raise
            if with_index:
                try:
                    cur.execute(_INDEX.format(table=self.table))
                except Exception as e:  # ORA-00955 / ORA-01408
                    if "ORA-00955" not in str(e) and "ORA-01408" not in str(e):
                        raise
        self._conn.commit()

    def drop_schema(self) -> None:
        with self._cursor() as cur:
            try:
                cur.execute(f"DROP TABLE {self.table} PURGE")
            except Exception as e:  # ORA-00942: table does not exist
                if "ORA-00942" not in str(e):
                    raise
        self._conn.commit()

    # ---------------- Store protocol ----------------

    def upsert(self, ns: str, key: str, vec: Sequence[float],
               payload: Dict[str, Any], scope: Optional[str] = None) -> None:
        if len(vec) != self.dim:
            raise ValueError(
                f"vector has {len(vec)} dims, store was created with {self.dim}; "
                "pass dim= matching your embedder"
            )
        sql = f"""
            MERGE INTO {self.table} t
            USING (SELECT :ns AS ns, :vkey AS vkey FROM dual) s
              ON (t.ns = s.ns AND t.vkey = s.vkey)
            WHEN MATCHED THEN UPDATE
              SET t.embedding = :emb, t.payload = :payload, t.scope = :scope
            WHEN NOT MATCHED THEN
              INSERT (ns, vkey, scope, embedding, payload)
              VALUES (:ns, :vkey, :scope, :emb, :payload)
        """
        with self._cursor() as cur:
            cur.execute(sql, ns=ns, vkey=key, scope=scope,
                        emb=self._vec(vec), payload=json.dumps(payload))
        self._conn.commit()

    def search(self, ns: str, vec: Sequence[float], k: int = 6,
               max_distance: float = 1.0, scope: Optional[str] = None
               ) -> List[Dict[str, Any]]:
        # Two things matter in this statement:
        #  * the scope predicate sits in the inner query, so a row the caller
        #    may not see is never scored and never leaves the database -- it
        #    cannot leak through a bug in Python-side filtering;
        #  * VECTOR_DISTANCE is computed once in the inline view instead of
        #    twice (SELECT list and WHERE), which halves the distance work.
        sql = f"""
            SELECT vkey, payload, dist FROM (
                SELECT vkey, payload,
                       VECTOR_DISTANCE(embedding, :emb, {self.distance}) AS dist
                  FROM {self.table}
                 WHERE ns = :ns
                   AND (scope IS NULL OR scope = :scope)
            )
             WHERE dist <= :maxd
             ORDER BY dist
             FETCH FIRST :k ROWS ONLY
        """
        with self._cursor() as cur:
            cur.execute(sql, emb=self._vec(vec), ns=ns, scope=scope,
                        maxd=float(max_distance), k=int(k))
            rows = cur.fetchall()
        out = []
        for vkey, payload, dist in rows:
            data = self._payload(payload)
            out.append({**data, "_key": vkey, "_distance": float(dist)})
        return out

    def get(self, ns: str, key: str,
            scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = f"""
            SELECT payload FROM {self.table}
             WHERE ns = :ns AND vkey = :vkey
               AND (scope IS NULL OR scope = :scope)
        """
        with self._cursor() as cur:
            cur.execute(sql, ns=ns, vkey=key, scope=scope)
            row = cur.fetchone()
        if not row:
            return None
        return self._payload(row[0])

    def count(self, ns: str, scope: Optional[str] = None) -> int:
        sql = f"""
            SELECT COUNT(*) FROM {self.table}
             WHERE ns = :ns AND (scope IS NULL OR scope = :scope)
        """
        with self._cursor() as cur:
            cur.execute(sql, ns=ns, scope=scope)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def purge(self, ns: str) -> int:
        with self._cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE ns = :ns", ns=ns)
            n = cur.rowcount
        self._conn.commit()
        return int(n)

    # ---------------- lifecycle ----------------

    def close(self) -> None:
        """Close only if this store opened the connection itself."""
        if self._owns_conn:
            self._conn.close()

    def __enter__(self) -> "OracleStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
