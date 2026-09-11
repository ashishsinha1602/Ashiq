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


# --- the TEXT bug ----------------------------------------------------------
# `_candidates` used to skip unbounded TEXT as "assume prose". That silently
# disabled --values for every string column in SQLite, where type affinity
# declares almost everything TEXT, and for the many PostgreSQL schemas that
# use `text` by convention rather than `varchar(n)`. Those users got nothing
# and no indication why.

@pytest.fixture
def mixed(tmp_path):
    path = tmp_path / "m.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE claim_vc   (id INTEGER PRIMARY KEY, status VARCHAR(30));
      CREATE TABLE claim_text (id INTEGER PRIMARY KEY, status TEXT, note TEXT);
    """)
    # Enough rows that the cardinality floor is not what decides these: the
    # question here is TEXT versus VARCHAR, so the tables have to be big
    # enough for a two-value column to read as a category rather than as a
    # small table.
    prose = ["the claim was rejected because prior authorisation was never obtained",
             "settled in full by bacs on the third of the month without adjustment",
             "coding does not match the recorded diagnosis for this encounter"]
    for i in range(200):
        con.execute("INSERT INTO claim_vc VALUES (?,?)",
                    (i, ("denied", "paid")[i % 2]))
        con.execute("INSERT INTO claim_text VALUES (?,?,?)",
                    (i, ("denied", "paid")[i % 2], prose[i % 3]))
    con.commit(); con.close()
    return f"sqlite:///{path}"


def _col(docs, table, name):
    return next(c for d in docs if d.name == table
                for c in d.columns if c.name == name)


def test_an_unbounded_text_column_yields_values_too(mixed):
    docs = reflect(mixed, sample_values=True)
    assert _col(docs, "claim_text", "status").values == ["denied", "paid"]


def test_a_declared_varchar_still_yields_values(mixed):
    docs = reflect(mixed, sample_values=True)
    assert _col(docs, "claim_vc", "status").values == ["denied", "paid"]


def test_long_prose_in_a_text_column_is_still_dropped(mixed):
    """Judged on what came back rather than what was declared: an unbounded
    column holding four short codes is a category, one holding a paragraph is
    prose, and the paragraph is what says so."""
    docs = reflect(mixed, sample_values=True)
    assert _col(docs, "claim_text", "note").values is None


# --- PII: --values put real row data in the model prompt -------------------
# Reproduced on a live PostgreSQL before the guards existed:
#   hr.employee full_name    TEXT -> ['A Patel', 'B Osei']
#   hr.employee home_address TEXT -> ['12 Main St', '9 Kings Rd']
# Names and home addresses to whichever provider the user configured, with no
# guard of any kind -- the only filters were string type, width and distinct
# count.

@pytest.fixture
def people(tmp_path):
    path = tmp_path / "p.db"
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE employee (
        id INTEGER PRIMARY KEY, full_name TEXT, home_address TEXT,
        email TEXT, work_phone TEXT, date_of_birth TEXT, status VARCHAR(20));
    """)
    # Enough rows that the cardinality floor is not what blocks the PII
    # columns -- otherwise this would pass for the wrong reason.
    for i in range(200):
        con.execute("INSERT INTO employee VALUES (?,?,?,?,?,?,?)",
                    (i, f"Person {i % 3}", f"{i % 3} Main St", f"p{i % 3}@x.com",
                     f"0700000{i % 3}", f"19{70 + i % 3}-01-01",
                     ("active", "leaver")[i % 2]))
    con.commit(); con.close()
    return f"sqlite:///{path}"


def _values(url, **kw):
    return {c.name: c.values for d in reflect(url, sample_values=True, **kw)
            for c in d.columns}


@pytest.mark.parametrize("col", ["full_name", "home_address", "email",
                                 "work_phone", "date_of_birth"])
def test_a_personal_column_is_never_sampled(people, col):
    assert _values(people)[col] is None


def test_a_plain_category_column_is_still_sampled(people):
    """The guard must not be so broad that the feature stops working -- the
    whole point is that a model should not have to guess whether a status
    reads 'denied' or 'DENIED'."""
    assert _values(people)["status"] == ["active", "leaver"]


def test_the_deny_list_is_overridable(people):
    """It is a deny-list, so it is wrong by construction: it misses
    `nachname`, `nino`, `mrn`. A caller who knows their schema must be able to
    say so."""
    assert _values(people, deny_columns=["status"])["status"] is None
    assert _values(people, deny_columns=["status"])["full_name"] is not None


def test_a_nearly_unique_column_is_an_identifier_not_a_category(tmp_path):
    """Three distinct values in a three-row table says only that the table is
    small. Without a floor relative to the row count, a tiny employee table
    hands over every name in it."""
    path = tmp_path / "tiny.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, nickname TEXT);")
    con.executemany("INSERT INTO t VALUES (?,?)",
                    [(1, "ada"), (2, "grace"), (3, "alan")])
    con.commit(); con.close()
    assert _values(f"sqlite:///{path}")["nickname"] is None


def test_restricting_a_column_clears_values_already_sampled():
    """The DDL omits a restricted column, but the sampled data should not
    survive the restriction either -- anything reading `Column.values`
    directly would still see it."""
    from schemagate import Catalog
    from schemagate.models import Column, ObjectDoc

    cat = Catalog()
    cat.add(ObjectDoc(name="t", columns=[Column("secret", "TEXT",
                                                values=["a", "b"])]))
    cat.restrict_column("t", "secret", ["x"])
    assert cat._docs["t"].columns[0].values is None


def test_a_restricted_column_is_not_sampled_when_reflection_knows(tmp_path):
    """Reflection usually runs before `restrict_column`, but when a caller
    reflects into a catalog that already carries roles this is the cheaper
    stop -- do not read the data at all."""
    from schemagate.introspect import _candidates

    raw = [{"name": "salary", "type": "VARCHAR(20)", "roles": ["payroll"]},
           {"name": "grade", "type": "VARCHAR(20)"}]
    assert _candidates(raw, set()) == ["grade"]


def test_a_value_list_is_not_left_looking_unfinished():
    """Rendered into a column list, the DDL's own comma used to land after the
    comment: `one of: 'Acme', 'Globex',` reads as a list that continues. The
    comma belongs to the declaration, so it goes before the note."""
    from schemagate.models import Column, ObjectDoc

    ddl = ObjectDoc(name="t", columns=[
        Column("customer", "VARCHAR(30)", values=["Acme", "Globex"]),
        Column("id", "INT")]).render_ddl()
    assert "one of: 'Acme', 'Globex'\n" in ddl + "\n"
    assert "'Globex'," not in ddl
    assert "  customer VARCHAR(30),  -- " in ddl
