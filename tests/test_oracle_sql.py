"""Static checks on the SQL OracleStore emits.

No Oracle instance is needed here. These tests drive OracleStore against a
recording fake connection and assert on the statements and bind values it
produces. They cannot prove the SQL runs -- only a real database does that,
via tests/test_store_conformance.py with SCHEMAGATE_ORACLE_DSN set -- but they
do pin the things most likely to rot silently: the scope predicate, the
parameterisation, and the vector binding format.
"""
import array

import pytest

from schemagate import HashingEmbedder
from schemagate.stores.oracle import OracleStore


class FakeCursor:
    def __init__(self, log, rows):
        self._log, self._rows = log, rows
        self.rowcount = 0

    def execute(self, sql, **binds):
        self._log.append((" ".join(sql.split()), binds))
        self.rowcount = len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows=()):
        self.log, self.rows, self.commits = [], list(rows), 0

    def cursor(self):
        return FakeCursor(self.log, self.rows)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture
def conn():
    return FakeConnection()


@pytest.fixture
def store(conn):
    return OracleStore(connection=conn, table="SCHEMAGATE_TEST", dim=8)


VEC = [0.1] * 8


# --- construction ---------------------------------------------------------

def test_requires_a_connection_or_dsn():
    with pytest.raises(ValueError, match="connection|dsn"):
        OracleStore()


def test_rejects_unsafe_table_name(conn):
    """The table name is interpolated into DDL, so it must be an identifier."""
    for bad in ["users; DROP TABLE x", "a b", "x'--", "tbl(1)"]:
        with pytest.raises(ValueError, match="unsafe table name"):
            OracleStore(connection=conn, table=bad)


def test_rejects_unknown_distance_metric(conn):
    with pytest.raises(ValueError, match="unsupported distance"):
        OracleStore(connection=conn, distance="JACCARDISH")


def test_accepts_documented_distance_metrics(conn):
    for metric in ["COSINE", "EUCLIDEAN", "DOT", "MANHATTAN"]:
        assert OracleStore(connection=conn, distance=metric).distance == metric


# --- DDL ------------------------------------------------------------------

def test_ddl_declares_vector_dimensions_then_format(store, conn):
    """Oracle's order is VECTOR(dimensions, format) -- not the reverse."""
    store.create_schema(with_index=False)
    ddl = conn.log[0][0]
    assert "VECTOR(8, FLOAT32)" in ddl
    assert "PRIMARY KEY (ns, vkey)" in ddl
    assert "payload CLOB CHECK (payload IS JSON)" in ddl


def test_create_schema_is_idempotent(conn):
    class AlreadyExists(FakeConnection):
        def cursor(self):
            cur = FakeCursor(self.log, self.rows)
            real = cur.execute

            def execute(sql, **b):
                real(sql, **b)
                raise Exception("ORA-00955: name is already used by an object")
            cur.execute = execute
            return cur

    c = AlreadyExists()
    OracleStore(connection=c, table="T", dim=8).create_schema()  # must not raise


def test_create_schema_reraises_real_errors(conn):
    class Broken(FakeConnection):
        def cursor(self):
            cur = FakeCursor(self.log, self.rows)
            cur.execute = lambda sql, **b: (_ for _ in ()).throw(
                Exception("ORA-01031: insufficient privileges"))
            return cur

    with pytest.raises(Exception, match="ORA-01031"):
        OracleStore(connection=Broken(), table="T", dim=8).create_schema()


# --- binding --------------------------------------------------------------

def test_vector_is_bound_as_float32_array(store, conn):
    store.upsert("ns", "k", VEC, {"qname": "k"})
    _, binds = conn.log[0]
    assert isinstance(binds["emb"], array.array)
    assert binds["emb"].typecode == "f", "VECTOR(*, FLOAT32) needs array('f')"
    assert len(binds["emb"]) == 8


def test_upsert_rejects_wrong_dimension(store):
    with pytest.raises(ValueError, match="dims"):
        store.upsert("ns", "k", [0.1] * 7, {})


def test_values_are_bound_never_interpolated(store, conn):
    """A namespace or key containing SQL must not change the statement."""
    nasty = "ns'; DELETE FROM SCHEMAGATE_TEST --"
    store.upsert(nasty, nasty, VEC, {"q": nasty})
    sql, binds = conn.log[0]
    assert "DELETE" not in sql
    assert binds["ns"] == nasty and binds["vkey"] == nasty


def test_upsert_uses_merge_so_reindex_does_not_duplicate(store, conn):
    store.upsert("ns", "k", VEC, {})
    sql = conn.log[0][0]
    assert sql.startswith("MERGE INTO SCHEMAGATE_TEST")
    assert "WHEN MATCHED THEN UPDATE" in sql
    assert "WHEN NOT MATCHED THEN" in sql


def test_writes_are_committed(store, conn):
    store.upsert("ns", "k", VEC, {})
    assert conn.commits == 1


# --- the security-relevant predicate --------------------------------------

@pytest.mark.parametrize("call", [
    lambda s: s.search("ns", VEC, k=3, scope="abc"),
    lambda s: s.get("ns", "k", scope="abc"),
    lambda s: s.count("ns", scope="abc"),
])
def test_every_read_filters_scope_in_sql(store, conn, call):
    """Scoping must happen in the database, not after the rows arrive."""
    call(store)
    sql, binds = conn.log[0]
    assert "(scope IS NULL OR scope = :scope)" in sql
    assert binds["scope"] == "abc"


def test_search_scope_predicate_is_inside_the_scored_subquery(store, conn):
    """A row the caller cannot see must never be scored or returned."""
    store.search("ns", VEC, k=3, scope="abc")
    sql = conn.log[0][0]
    inner = sql[sql.index("("):sql.rindex(")")]
    assert "(scope IS NULL OR scope = :scope)" in inner


def test_search_computes_distance_once(store, conn):
    store.search("ns", VEC, k=3)
    assert conn.log[0][0].count("VECTOR_DISTANCE") == 1


def test_search_binds_k_and_max_distance(store, conn):
    store.search("ns", VEC, k=4, max_distance=0.75)
    sql, binds = conn.log[0]
    assert "FETCH FIRST :k ROWS ONLY" in sql
    assert binds["k"] == 4 and binds["maxd"] == 0.75


def test_purge_is_scoped_to_one_namespace(store, conn):
    store.purge("ns")
    sql, binds = conn.log[0]
    assert sql == "DELETE FROM SCHEMAGATE_TEST WHERE ns = :ns"
    assert binds["ns"] == "ns"


# --- result decoding ------------------------------------------------------

def test_search_decodes_payload_and_distance():
    conn = FakeConnection(rows=[("billing_invoice", '{"qname": "billing_invoice"}', 0.25)])
    store = OracleStore(connection=conn, table="T", dim=8)
    hit = store.search("ns", VEC, k=1)[0]
    assert hit == {"qname": "billing_invoice", "_key": "billing_invoice",
                   "_distance": 0.25}


def test_search_decodes_clob_payload():
    class Lob:
        def read(self):
            return '{"qname": "x"}'

    conn = FakeConnection(rows=[("x", Lob(), 0.5)])
    store = OracleStore(connection=conn, table="T", dim=8)
    assert store.search("ns", VEC, k=1)[0]["qname"] == "x"


def test_get_missing_returns_none():
    store = OracleStore(connection=FakeConnection(rows=[]), table="T", dim=8)
    assert store.get("ns", "nope") is None


# --- integration with Catalog --------------------------------------------

def test_catalog_rejects_embedder_store_dimension_mismatch():
    from schemagate import Catalog, Column, ObjectDoc

    store = OracleStore(connection=FakeConnection(), table="T", dim=64)
    cat = Catalog(embedder=HashingEmbedder(dim=512), store=store)
    cat.add(ObjectDoc(name="t", columns=[Column("c", "NUMBER")]))
    with pytest.raises(ValueError, match="512"):
        cat.index()


def test_store_does_not_close_a_borrowed_connection():
    """If the app passed its own pooled connection, we must not close it."""
    closed = []

    class Conn(FakeConnection):
        def close(self):
            closed.append(True)

    store = OracleStore(connection=Conn(), table="T", dim=8)
    store.close()
    assert not closed, "a connection we did not open is not ours to close"


# --- independent syntax validation ----------------------------------------
# No Oracle server is reachable from CI, so the next best evidence is an
# independent Oracle SQL parser. sqlglot cannot yet parse Oracle's VECTOR
# column type (it expects the format before the dimension count, while
# Oracle documents VECTOR(dimensions, format)), so that one type is
# substituted before parsing. Everything else is checked as written.

def _statements_emitted():
    conn = FakeConnection()
    store = OracleStore(connection=conn, table="SCHEMAGATE_SYNTAX", dim=8)
    store.create_schema()
    store.upsert("ns", "k", [0.1] * 8, {"a": 1})
    store.search("ns", [0.1] * 8, k=3, scope="s")
    store.get("ns", "k", scope="s")
    store.count("ns", scope="s")
    store.purge("ns")
    return [" ".join(sql.split()) for sql, _ in conn.log]


def test_every_statement_parses_as_oracle_sql():
    sqlglot = pytest.importorskip("sqlglot")
    import re

    statements = _statements_emitted()
    assert len(statements) >= 7, "a statement stopped being emitted"
    for statement in statements:
        probe = re.sub(r"VECTOR\(\d+,\s*FLOAT32\)", "RAW(2000)", statement)
        try:
            sqlglot.parse_one(probe, dialect="oracle")
        except Exception as e:      # pragma: no cover - failure path
            pytest.fail(f"not valid Oracle SQL: {statement[:120]}\n{e}")


def test_vector_type_matches_oracle_documented_argument_order():
    """Oracle documents VECTOR(number_of_dimensions, format).

    sqlglot expects the opposite order, which is a bug in sqlglot rather
    than in this DDL -- pinned here so the next person does not "fix" it
    the wrong way round.
    """
    ddl = _statements_emitted()[0]
    assert "VECTOR(8, FLOAT32)" in ddl


def test_payload_decodes_every_shape_the_driver_returns():
    """Live on 26ai, python-oracledb 4 returned the JSON payload already
    decoded as a dict; json.loads(dict) raised TypeError in get()."""
    from schemagate.stores.oracle import OracleStore
    class Lob:
        def read(self): return '{"a": 1}'
    assert OracleStore._payload(Lob()) == {"a": 1}
    assert OracleStore._payload('{"a": 1}') == {"a": 1}
    assert OracleStore._payload(b'{"a": 1}') == {"a": 1}
    assert OracleStore._payload({"a": 1}) == {"a": 1}
    assert OracleStore._payload(None) == {}
    assert OracleStore._payload("") == {}
