"""One contract, every backend.

A Store is only useful if all implementations behave identically -- otherwise
swapping MemoryStore for OracleStore silently changes retrieval. These tests
run against every available backend.

MemoryStore always runs. OracleStore runs only when a database is reachable:

    export SCHEMAGATE_ORACLE_DSN='user/password@host:1521/FREEPDB1'
    pytest tests/test_store_conformance.py -v

That single command is what certifies the Oracle backend. Until it has been
run against a real 23ai instance, OracleStore is implemented but uncertified,
and the README says exactly that.
"""
import os
import uuid

import pytest

from schemagate import HashingEmbedder, MemoryStore

DIM = 64
_E = HashingEmbedder(dim=DIM)


def vec(text):
    return _E.embed([text])[0]


ORACLE_DSN = os.environ.get("SCHEMAGATE_ORACLE_DSN")


def _oracle_store():
    from schemagate.stores.oracle import OracleStore
    user = os.environ.get("SCHEMAGATE_ORACLE_USER")
    pw = os.environ.get("SCHEMAGATE_ORACLE_PASSWORD")
    store = OracleStore(dsn=ORACLE_DSN, user=user, password=pw,
                        table="SCHEMAGATE_CONFORMANCE", dim=DIM)
    store.create_schema()
    return store


@pytest.fixture(params=["memory", "oracle"])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    if not ORACLE_DSN:
        pytest.skip("set SCHEMAGATE_ORACLE_DSN to certify the Oracle backend")
    s = _oracle_store()
    try:
        yield s
    finally:
        s.drop_schema()
        s.close()


@pytest.fixture
def ns():
    """A unique namespace per test, so a shared database stays isolated."""
    return f"conformance:{uuid.uuid4().hex[:12]}"


# --- basic round trip -----------------------------------------------------

def test_upsert_then_get(store, ns):
    store.upsert(ns, "billing_invoice", vec("billing invoice total"),
                 {"qname": "billing_invoice", "kind": "TABLE"})
    got = store.get(ns, "billing_invoice")
    assert got["qname"] == "billing_invoice"
    assert got["kind"] == "TABLE"


def test_get_missing_returns_none(store, ns):
    assert store.get(ns, "nope") is None


def test_upsert_is_idempotent_and_updates(store, ns):
    store.upsert(ns, "k", vec("first"), {"v": 1})
    store.upsert(ns, "k", vec("second"), {"v": 2})
    assert store.count(ns) == 1
    assert store.get(ns, "k")["v"] == 2


def test_count_and_purge(store, ns):
    for i in range(5):
        store.upsert(ns, f"k{i}", vec(f"object number {i}"), {"i": i})
    assert store.count(ns) == 5
    assert store.purge(ns) == 5
    assert store.count(ns) == 0


def test_namespaces_are_isolated(store, ns):
    other = ns + ":other"
    store.upsert(ns, "k", vec("alpha"), {"where": "a"})
    store.upsert(other, "k", vec("alpha"), {"where": "b"})
    assert store.get(ns, "k")["where"] == "a"
    assert store.get(other, "k")["where"] == "b"
    store.purge(ns)
    assert store.get(other, "k") is not None, "purge must not cross namespaces"
    store.purge(other)


# --- search ---------------------------------------------------------------

def test_search_orders_by_distance(store, ns):
    store.upsert(ns, "invoice", vec("billing invoice total gross"), {"n": "invoice"})
    store.upsert(ns, "warehouse", vec("inventory warehouse stock level"), {"n": "warehouse"})
    hits = store.search(ns, vec("billing invoice total gross"), k=2, max_distance=2.0)
    assert hits[0]["n"] == "invoice"
    assert hits[0]["_distance"] <= hits[1]["_distance"]


def test_search_respects_k(store, ns):
    for i in range(10):
        store.upsert(ns, f"k{i}", vec(f"table number {i}"), {"i": i})
    assert len(store.search(ns, vec("table"), k=3, max_distance=2.0)) == 3


def test_search_respects_max_distance(store, ns):
    store.upsert(ns, "k", vec("billing invoice"), {"n": "k"})
    assert store.search(ns, vec("billing invoice"), k=5, max_distance=2.0)
    assert store.search(ns, vec("billing invoice"), k=5, max_distance=-1.0) == []


def test_search_empty_namespace(store, ns):
    assert store.search(ns, vec("anything"), k=5, max_distance=2.0) == []


def test_search_returns_key_and_distance(store, ns):
    store.upsert(ns, "billing_invoice", vec("invoice"), {"qname": "billing_invoice"})
    hit = store.search(ns, vec("invoice"), k=1, max_distance=2.0)[0]
    assert hit["_key"] == "billing_invoice"
    assert isinstance(hit["_distance"], float)


# --- identity scoping: the security-relevant contract ---------------------

def test_scoped_row_hidden_from_other_scope(store, ns):
    store.upsert(ns, "secret", vec("payroll salary"), {"n": "secret"}, scope="aaaa1111")
    assert store.search(ns, vec("payroll salary"), k=5, max_distance=2.0,
                        scope="bbbb2222") == []
    assert store.get(ns, "secret", scope="bbbb2222") is None


def test_scoped_row_visible_to_its_own_scope(store, ns):
    store.upsert(ns, "secret", vec("payroll salary"), {"n": "secret"}, scope="aaaa1111")
    hits = store.search(ns, vec("payroll salary"), k=5, max_distance=2.0,
                        scope="aaaa1111")
    assert [h["n"] for h in hits] == ["secret"]
    assert store.get(ns, "secret", scope="aaaa1111")["n"] == "secret"


def test_unscoped_row_visible_to_everyone(store, ns):
    store.upsert(ns, "public", vec("country reference data"), {"n": "public"})
    for scope in (None, "aaaa1111", "bbbb2222"):
        hits = store.search(ns, vec("country reference data"), k=5,
                            max_distance=2.0, scope=scope)
        assert [h["n"] for h in hits] == ["public"]


def test_no_scope_argument_does_not_expose_scoped_rows(store, ns):
    """Fail closed: omitting scope must not act as a master key."""
    store.upsert(ns, "public", vec("public table"), {"n": "public"})
    store.upsert(ns, "secret", vec("public table"), {"n": "secret"}, scope="aaaa1111")
    names = {h["n"] for h in store.search(ns, vec("public table"), k=5,
                                          max_distance=2.0, scope=None)}
    assert names == {"public"}


def test_count_is_scope_aware(store, ns):
    store.upsert(ns, "public", vec("a"), {})
    store.upsert(ns, "secret", vec("b"), {}, scope="aaaa1111")
    assert store.count(ns, scope=None) == 1
    assert store.count(ns, scope="aaaa1111") == 2
    assert store.count(ns, scope="bbbb2222") == 1
