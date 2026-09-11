"""Value sampling against a database that is not on this machine.

This exists because of one connection: an Autonomous Database wallet, a schema
of ordinary width, and a Studio that sat on "Connecting..." long enough to look
hung. Nothing was wrong with the query -- there were simply a great many of
them, one per candidate column, each paying a network round trip that costs
nothing against a local file.
"""
import sqlite3
import time

from sqlalchemy import create_engine, event

from schemagate.introspect import reflect


def _db(tmp_path, tables=12, rows=200):
    path = tmp_path / "s.db"
    c = sqlite3.connect(path)
    for i in range(tables):
        c.execute(f"CREATE TABLE t{i}(id INTEGER PRIMARY KEY, status TEXT, code TEXT)")
        c.executemany(f"INSERT INTO t{i}(status, code) VALUES (?,?)",
                      [("open" if r % 2 else "closed", "AA" if r % 3 else "BB")
                       for r in range(rows)])
    c.commit()
    c.close()
    return create_engine(f"sqlite:///{path}")


def _count_connects(engine):
    seen = []
    event.listen(engine, "connect", lambda *a: seen.append(1))
    return seen


def test_sampling_shares_one_connection(tmp_path):
    """Worth asserting, but not the fix it looks like: SQLAlchemy pools, so
    the per-table `engine.connect()` this replaced was already handing back
    the same DBAPI connection. Measured, not assumed -- twelve per-table opens
    against a held connection produce one DBAPI connect, not twelve."""
    engine = _db(tmp_path, tables=12)
    seen = _count_connects(engine)
    docs = reflect(engine, sample_values=True)
    assert len(docs) == 12
    assert len(seen) <= 3, f"opened {len(seen)} connections for 12 tables"


def test_sampling_still_finds_the_values(tmp_path):
    """The bounds must not cost the feature they bound."""
    docs = reflect(_db(tmp_path, tables=2), sample_values=True)
    status = [c for d in docs for c in d.columns if c.name == "status"]
    assert status and all(c.values == ["closed", "open"] for c in status)


def test_a_budget_that_has_already_expired_stops_the_sampling(tmp_path):
    """Past the deadline reflection still returns a catalog -- values are an
    enrichment, and a connect that never finishes is not."""
    docs = reflect(_db(tmp_path, tables=6), sample_values=True,
                   sample_budget=0.000001)
    assert len(docs) == 6
    assert all(c.values is None for d in docs for c in d.columns)


def test_the_budget_is_wall_clock_not_a_column_count(tmp_path):
    """A slow link is what runs the clock down, and nothing in the schema says
    how slow the link is."""
    engine = _db(tmp_path, tables=40)

    slow = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _delay(conn, cursor, stmt, params, ctx, many):
        if "SELECT DISTINCT" in stmt.upper() or "count(" in stmt.lower():
            slow["n"] += 1
            time.sleep(0.01)

    t = time.monotonic()
    docs = reflect(engine, sample_values=True, sample_budget=0.25)
    elapsed = time.monotonic() - t
    assert len(docs) == 40
    # without a budget this is 40 tables * (1 count + 2 columns) * 10ms
    assert elapsed < 3.0, f"took {elapsed:.1f}s despite a 0.25s budget"


def test_zero_means_no_limit(tmp_path):
    docs = reflect(_db(tmp_path, tables=3), sample_values=True, sample_budget=0)
    assert any(c.values for d in docs for c in d.columns)


def test_sampling_off_opens_nothing_extra(tmp_path):
    engine = _db(tmp_path, tables=5)
    seen = _count_connects(engine)
    reflect(engine, sample_values=False)
    assert len(seen) <= 2
