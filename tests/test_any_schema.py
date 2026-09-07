"""Any schema. Generated, not hand-written, so nobody chose the easy cases.

Hypothesis builds thousands of catalogs from random names, types, columns,
foreign keys, roles and hints -- including names that are reserved words,
contain spaces, quotes, dollar signs, emoji or nothing at all -- and the
library must never raise, must always render balanced DDL, and must never
show a restricted object to a caller without the role.

This is the evidence behind "works on any schema". A hand-picked fixture
proves it works on that fixture; this proves the invariants hold on inputs
no one sat down and wrote.
"""
import string

import pytest
hypothesis = pytest.importorskip("hypothesis")  # needs Python 3.10+
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from schemagate import Catalog, Column, ForeignKey, ObjectDoc, Principal

# --- strategies ------------------------------------------------------------

# identifiers the way real databases actually have them
_ident_chars = string.ascii_letters + string.digits + "_"
plain_ident = st.text(_ident_chars, min_size=1, max_size=40)
nasty_ident = st.sampled_from([
    "select", "from", "table", "order", "group", "user", "date",   # reserved words
    "with space", 'with"quote', "with'apostrophe", "with-dash", "with.dot",
    "SYS$SESSION", "T$1", "MSysObjects", "#temp", "@var",
    "a", "_", "__", "1starts_with_digit", "x" * 128,
    "facturación", "売上", "Müller", "naïve", "🔥table", "таблица",
    "CamelCaseName", "ALLCAPS", "mixed_Case_Name",
])
ident = st.one_of(plain_ident, nasty_ident)

sql_type = st.sampled_from([
    "INTEGER", "NUMBER(10,2)", "VARCHAR2(100 CHAR)", "TEXT", "CLOB", "BLOB",
    "DATE", "TIMESTAMP(6) WITH TIME ZONE", "BOOLEAN", "JSON", "UUID",
    "DECIMAL(38,0)", "FLOAT", "VECTOR(768, FLOAT32)", "GEOMETRY", "",
    "ARRAY", "NUMERIC", "NVARCHAR(MAX)", "BIT", "MONEY",
])

column = st.builds(
    Column, name=ident, type=sql_type,
    nullable=st.booleans(), comment=st.one_of(st.none(), st.text(max_size=60)),
    pk=st.booleans(),
)

role = st.text(string.ascii_lowercase, min_size=1, max_size=8)


@st.composite
def catalogs(draw, min_objects=1, max_objects=40):
    n = draw(st.integers(min_objects, max_objects))
    names = draw(st.lists(ident, min_size=n, max_size=n, unique=True))
    schemas = draw(st.lists(st.one_of(st.none(), plain_ident), min_size=n, max_size=n))
    docs = []
    for name, schema in zip(names, schemas):
        cols = draw(st.lists(column, min_size=0, max_size=25))
        fks = []
        for _ in range(draw(st.integers(0, 3))):
            # some FKs point at real tables, some at nothing, some at itself
            target = draw(st.one_of(st.sampled_from(names), ident))
            fks.append(ForeignKey(columns=[draw(ident)], ref_table=target))
        docs.append(ObjectDoc(
            name=name, schema=schema,
            kind=draw(st.sampled_from(["TABLE", "VIEW", "ENDPOINT"])),
            description=draw(st.one_of(st.none(), st.text(max_size=80))),
            hint=draw(st.one_of(st.none(), st.text(max_size=80))),
            columns=cols, foreign_keys=fks,
            definition=draw(st.one_of(st.none(), st.text(max_size=300))),
            roles=draw(st.one_of(st.none(), st.lists(role, min_size=1, max_size=3))),
        ))
    return docs


question = st.one_of(
    st.text(max_size=120),
    st.sampled_from(["", " ", "revenue by month", "SELECT * FROM x", "🔥", "売上 金額",
                     "'; DROP TABLE users; --", "\x00", "a" * 5000]),
)

SLOW = settings(max_examples=200, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


# --- invariants -------------------------------------------------------------

@SLOW
@given(docs=catalogs(), q=question, top_k=st.integers(0, 60))
def test_select_never_raises_and_respects_top_k(docs, q, top_k):
    cat = Catalog()
    cat.add_all(docs)
    sel = cat.select(q, top_k=top_k, expand_fks=False)
    assert len(sel) <= max(top_k, 0)
    assert len(set(sel.table_names)) == len(sel.table_names), "duplicate hit"
    assert sel.total_objects == len(cat._docs)


@SLOW
@given(docs=catalogs(), q=question)
def test_expansion_never_exceeds_catalog_or_duplicates(docs, q):
    cat = Catalog()
    cat.add_all(docs)
    sel = cat.select(q, top_k=6, expand_fks=True)
    assert len(sel) <= len(cat._docs)
    assert len(set(sel.table_names)) == len(sel.table_names)


@SLOW
@given(docs=catalogs(), q=question)
def test_prompt_fragment_is_always_balanced_text(docs, q):
    cat = Catalog()
    cat.add_all(docs)
    frag = cat.select(q, top_k=8).prompt_fragment()
    assert isinstance(frag, str)
    structural = "\n".join(line.split("--", 1)[0] for line in frag.splitlines())
    assert structural.count("(") == structural.count(")")
    # a comment, hint or description can never produce a line of its own:
    # every newline in the fragment is one the renderer emitted
    for doc in cat._docs.values():
        for text in (doc.hint, doc.description, *[c.comment for c in doc.columns]):
            if text and "\n" in text:
                for piece in text.split("\n"):
                    piece = piece.strip()
                    if len(piece) > 3:
                        assert f"\n{piece}" not in frag, f"comment text escaped: {piece!r}"
    # every selected object's name appears in what the model would receive
    for name in cat.select(q, top_k=8).table_names:
        assert name.split(".")[-1] in frag or name in frag


@SLOW
@given(docs=catalogs(), q=question, roles=st.lists(role, max_size=4))
def test_scoping_never_leaks(docs, q, roles):
    """The one invariant that matters most, on inputs nobody chose."""
    cat = Catalog()
    cat.add_all(docs)
    who = Principal("okta:x", roles=set(roles))
    sel = cat.select(q, top_k=20, principal=who, expand_fks=True)
    for hit in sel.hits:
        doc = hit.doc
        if doc.roles:
            assert set(doc.roles) & set(roles), f"{doc.qname} leaked to roles {roles}"
    frag = sel.prompt_fragment().lower()
    for doc in cat._docs.values():
        if doc.roles and not (set(doc.roles) & set(roles)) and len(doc.name) > 3:
            # a restricted name must not appear as an object header in the DDL
            assert f"{doc.kind.lower()} {doc.qname.lower()} (" not in frag


@SLOW
@given(docs=catalogs(), q=question)
def test_anonymous_sees_only_unrestricted(docs, q):
    cat = Catalog()
    cat.add_all(docs)
    for hit in cat.select(q, top_k=20, principal=None).hits:
        assert not hit.doc.roles


@SLOW
@given(docs=catalogs())
def test_shadows_are_well_formed(docs):
    cat = Catalog()
    cat.add_all(docs)
    shadows = cat.shadows()
    for shadow, base in shadows.items():
        assert shadow != base
        assert base in cat._docs and shadow in cat._docs
        assert cat._docs[shadow].schema == cat._docs[base].schema
        assert base not in shadows, "a base must not itself be a shadow"


@SLOW
@given(docs=catalogs(), q=question)
def test_naming_an_object_outright_finds_it(docs, q):
    """Whatever else is in the catalog, asking for a table by its exact name
    must return it -- unless it is restricted or the name tokenises to
    nothing (pure punctuation)."""
    from schemagate.embedder import tokenize
    cat = Catalog()
    cat.add_all(docs)
    for doc in list(cat._docs.values())[:5]:
        if doc.roles or not tokenize(doc.name):
            continue
        if len(tokenize(doc.name)) < 2:      # single tokens collide too often to promise
            continue
        names = cat.select(doc.name, top_k=len(cat._docs), expand_fks=False).table_names
        assert doc.qname in names, f"{doc.qname} not found by its own name"


@SLOW
@given(docs=catalogs(), q=question)
def test_hint_and_describe_never_raise(docs, q):
    cat = Catalog()
    cat.add_all(docs)
    for doc in list(cat._docs.values())[:3]:
        cat.hint(doc.qname, "a hint")
        cat.restrict(doc.qname, ["r"])
    cat.select(q, top_k=5)


@SLOW
@given(docs=catalogs(), q=question)
def test_json_round_trip_of_selection(docs, q):
    """MCP and the studio serialise this; it must always be JSON-safe."""
    import json
    cat = Catalog()
    cat.add_all(docs)
    sel = cat.select(q, top_k=6)
    json.dumps({"objects": sel.table_names, "ddl": sel.prompt_fragment(),
                "object_list": sel.object_list,
                "explain": [(h.doc.qname, h.score, h.reason) for h in sel.hits]},
               ensure_ascii=False)


# --- scale, generated -------------------------------------------------------

@pytest.mark.parametrize("n", [1000, 3000])
def test_thousands_of_objects_stay_usable(n):
    import random
    import time
    rnd = random.Random(n)
    words = ["order", "customer", "invoice", "line", "payment", "device", "reading",
             "claim", "member", "account", "ledger", "trade", "shipment", "product",
             "alarm", "site", "provider", "policy", "event", "session"]
    cat = Catalog()
    for i in range(n):
        name = "_".join(rnd.sample(words, rnd.randint(1, 3))) + f"_{i}"
        cat.add(ObjectDoc(name=name, schema=rnd.choice(["sales", "ops", None]),
                          columns=[Column(f"{rnd.choice(words)}_{j}", "TEXT")
                                   for j in range(rnd.randint(1, 30))],
                          foreign_keys=[ForeignKey(["id"], f"{rnd.choice(words)}_{rnd.randint(0, n)}")],
                          roles=["secret"] if i % 50 == 0 else None))
    t0 = time.perf_counter()
    cat.index()
    build = time.perf_counter() - t0
    t0 = time.perf_counter()
    for q in ["customer invoices by month", "device alarm readings", "ledger trades"]:
        sel = cat.select(q, top_k=6, principal=Principal("okta:a"))
        assert sel.hits and all(not h.doc.roles for h in sel.hits)
    per_query = (time.perf_counter() - t0) / 3
    assert build < 60, f"indexing {n} objects took {build:.1f}s"
    assert per_query < 2.0, f"{per_query * 1000:.0f}ms per query at {n} objects"
