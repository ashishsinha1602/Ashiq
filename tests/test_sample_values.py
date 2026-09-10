"""Column values in the prompt.

This feature exists because of one wrong answer, and the tests are written
around it: a model was handed `status VARCHAR(30)`, wrote
`WHERE status = 'DENIED'`, and got nothing back because the rows say
`denied`. The SQL was correct in every way a schema can express. An empty
result reads as "there are none", not as a mistake, which makes it the worst
shape of wrong answer available.
"""
import sqlite3

import pytest
from sqlalchemy import create_engine

from schemagate import Catalog
from schemagate.introspect import reflect
from schemagate.models import Column


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE claim (
        claim_id INTEGER PRIMARY KEY,
        status VARCHAR(30),
        denial_reason VARCHAR(300),
        notes VARCHAR(4000),
        payer_ref VARCHAR(20));
      INSERT INTO claim VALUES (1,'denied','duplicate claim','a lot of prose','P1');
      INSERT INTO claim VALUES (2,'paid',NULL,'more prose','P2');
      INSERT INTO claim VALUES (3,'denied','not covered','and more','P3');
    """)
    for i in range(4, 200):                 # a high-cardinality column
        con.execute("INSERT INTO claim VALUES (?,?,?,?,?)",
                    (i, "open", None, "prose", f"P{i}"))
    con.commit()
    con.close()
    return f"sqlite:///{path}"


def test_a_short_column_with_few_values_carries_them(db):
    docs = reflect(db, sample_values=True)
    claim = next(d for d in docs if d.name == "claim")
    status = next(c for c in claim.columns if c.name == "status")
    assert status.values == ["denied", "open", "paid"]
    assert "one of: 'denied', 'open', 'paid'" in status.render()


def test_the_values_reach_the_prompt_the_model_sees(db):
    """Rendering them on the Column is not enough -- they have to survive
    into the DDL fragment, which is the only thing the model is given."""
    cat = Catalog().bootstrap(db, sample_values=True)
    frag = cat.select("which claims were denied", top_k=3).prompt_fragment()
    assert "'denied'" in frag


def test_reading_values_is_off_by_default(db):
    """Everything else in this library reads metadata only. Reading rows is
    a different promise, so it must be asked for."""
    docs = reflect(db)
    claim = next(d for d in docs if d.name == "claim")
    assert all(c.values is None for c in claim.columns)


def test_a_high_cardinality_column_is_dropped_not_dumped(db):
    """payer_ref has ~200 distinct values. Listing them would bloat the very
    prompt this library exists to shrink."""
    docs = reflect(db, sample_values=True, max_distinct=25)
    claim = next(d for d in docs if d.name == "claim")
    assert next(c for c in claim.columns if c.name == "payer_ref").values is None


def test_a_wide_column_is_never_queried(db):
    """A VARCHAR(4000) is prose. Asking for its distinct values is a scan
    that returns nothing worth having, so it is not a candidate at all."""
    docs = reflect(db, sample_values=True)
    claim = next(d for d in docs if d.name == "claim")
    assert next(c for c in claim.columns if c.name == "notes").values is None


def test_a_primary_key_is_not_a_category(db):
    docs = reflect(db, sample_values=True)
    claim = next(d for d in docs if d.name == "claim")
    assert next(c for c in claim.columns if c.name == "claim_id").values is None


def test_a_column_that_cannot_be_read_does_not_fail_the_reflection():
    """Sampling is an enrichment. No privilege on one column, or a view that
    will not scan, must not take the whole catalog down with it."""
    from schemagate.introspect import _sample_values

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise RuntimeError("ORA-01031")

    class Eng:
        def connect(self): return Conn()

    raw = [{"name": "status", "type": "VARCHAR(30)", "primary_key": False}]
    assert _sample_values(Eng(), None, "t", raw, "TABLE", 25) is None


def test_values_render_alongside_a_comment_not_instead_of_it():
    c = Column(name="status", type="VARCHAR(30)", comment="claim state",
               values=["denied", "paid"])
    out = c.render()
    assert "one of: 'denied', 'paid'" in out and "claim state" in out
