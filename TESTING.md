# What has actually been tested

This is the record. Every claim in the README traces back to something on
this page, and everything on this page runs from `pytest` or
`python tests/bench.py` unless it says otherwise.

## The six schemas

All invented. None derived from any real system. Each one exists because it
breaks selection in a way the others don't.

| schema | objects | what it's for | recall@6 | copy never beats real |
|---|---|---|---|---|
| Commerce | 42 | the baseline: orders, billing, HR, inventory; one decoy invoice table, one restricted HR table | 100% (12 q) · 50% on 6 business-word questions, 100% with descriptions | — |
| Clinical claims | 27 | a different vocabulary entirely; meaning lives in code-lookup tables one join away | 100% (10 q) | 1/1 |
| Claims warehouse (star) | 51 | the same fact at four grains, SCD2 member history, one date dimension joined five ways, bridges, 15 backup/staging copies | 100% (15 q) · 4 hard questions 50% → 100% with descriptions | 7/7 |
| Bank ledger and trading | 39 | ledger at three grains, trades vs positions vs settlements, FX as table and as-of view, lending, KYC/AML restricted | 100% (15 q) · 4 business-word questions 40% → 100% with descriptions | 7/7 |
| IoT fleet telemetry | 40 | readings at raw / 1-minute / hourly grains, six monthly partition tables, alarm lifecycle across three tables, device placement history | 100% (16 q) · 3 business-word questions 50% → 100% with descriptions | 5/5 |
| Hostile | 260 | four schemas, the same table name in three of them, an `_archive` and `_stg` copy of every table, an 8-deep FK chain, a reference cycle, composite keys, a 320-column table, 100-char identifiers, Spanish and Japanese names | 100% (12 q) · 2 cross-language questions 0% → 100% with descriptions | 3/3 |

The hostile schema also runs against a live PostgreSQL 16 with real schemas
and ALTER-added cycle constraints; every one of its 31 tests passes on both
engines.

## Bugs the schemas found, in the order they were found

Each of these was live in the code at the time and would have shipped.

1. **Embeddings changed between processes.** The hashed embedder took the
   sign of each feature from Python's builtin `hash()`, which is salted per
   process. Tests passed because index and query ran in one process. A
   persisted index would have silently returned noise. Found by running the
   embedder in three separate processes; fixed by deriving the sign from
   blake2b; pinned by a test that runs subprocesses under different
   `PYTHONHASHSEED` values.
2. **`hint()` did nothing to retrieval**, including in the README's own
   example. It set the hint but never marked the index stale. Fixed;
   `select()` now rebuilds when anything indexed has changed.
3. **A lazy export pointed at a module that did not exist.** `from schemagate
   import OracleStore` crashed with `ModuleNotFoundError`. The module exists
   now, and a test resolves every lazy export.
4. **Non-English schemas were unsearchable.** The tokeniser was
   `[a-z0-9]+`. `facturación` became `['facturaci', 'n']`; `売上明細` became
   nothing at all. Found by the hostile schema. Fixed with a Unicode-aware
   tokeniser, accent folding both ways, and character n-grams for CJK; a
   test asserts English tokenisation is byte-identical to before.
5. **A three-column backup copy beat the twenty-five-column table it was
   copied from, even with a hint on the real one.** Cosine similarity over
   normalised feature bags rewards short documents. Found by the warehouse.
   Fixed by ranking `_bkp`/`_old`/`_tmp`/`_v2`/`stg_`-style copies below the
   object they shadow, only when that object exists, only in the same
   schema, and never when the question names the copy outright. A name-only
   index was tried first, swept across all schemas, and removed because it
   helped nowhere. The naive ranking is kept reachable so a test can prove
   the fix does something.
6. **`dev_` and `test_` as shadow prefixes were false positives waiting to
   happen.** In the telemetry schema `dev_` means device. Found while wiring
   `stg_device` → `dev_device`. Both removed from the defaults, with a
   comment saying why; add them yourself if your naming is unambiguous.
7. **The MCP SDK shipped a 2.0 that renamed the server class.** Every test
   passed on the pinned 1.x while a fresh `pip install` got 2.x and crashed.
   Found by the clean-venv check. Fixed with a shim for both; the protocol
   tests now spawn a real subprocess over stdio, which is stable across
   versions and is the path Claude Desktop uses.
8. **The stdlib HTTP server's listen backlog is 5.** A page firing a few
   requests at once got connection resets. Raised to 128; 400 requests at
   32-way concurrency now complete with zero transport failures.

9. **A newline inside a database comment escaped its `--` line.** Multi-line
   table comments are routine in Oracle and PostgreSQL, and the second line
   would have landed in the DDL uncommented. Found by hypothesis (below).
   Comments, hints and descriptions are now collapsed to one line in both
   the Python and JavaScript renderers.
10. **Oracle and SQL function names were indexed as concepts.** A view using
    `NVL`, `DECODE` and `SYSDATE` was retrievable by those words. Found by
    the Oracle-shaped test. Ninety-odd function and keyword names across
    Oracle, PostgreSQL, SQL Server and MySQL are now stop-words for view
    indexing.

## Any schema: generated, not chosen

`tests/test_any_schema.py` uses Hypothesis to build catalogs nobody wrote:
random object counts, random names drawn from a pool that includes reserved
words (`select`, `order`, `user`), names with spaces, quotes, dashes, dollar
signs (`SYS$SESSION`), a leading digit, 128 characters, Spanish, Japanese,
Russian, German and an emoji; column types from `NUMBER(10,2)` and
`VARCHAR2(100 CHAR)` through `VECTOR(768, FLOAT32)`, `NVARCHAR(MAX)` and the
empty string; zero to twenty-five columns; foreign keys that point at real
tables, at nothing, and at themselves; random roles, hints and descriptions;
questions that are empty, 5,000 characters, a NUL byte, or an injection
string. Around 1,800 catalogs per run. The invariants: `select()` never
raises and honours `top_k`; expansion never exceeds the catalog or repeats
an object; the prompt fragment is balanced and no comment text ever escapes
onto an uncommented line; a caller without the role never sees a restricted
object, in the list or in the DDL; an anonymous caller sees only
unrestricted objects; every shadow points at a real, distinct, same-schema
base that is not itself a shadow; asking for a multi-word object by its
exact name finds it; the selection is JSON-serialisable. Then 1,000 and
3,000 generated objects, with timing bounds.

## Oracle-shaped, without an Oracle

`tests/test_oracle_shaped.py` drives `schemagate.introspect.reflect` against a
fake Inspector that returns exactly what the SQLAlchemy Oracle dialect
returns: uppercase owners and names, `NUMBER(12, 2)`, `VARCHAR2(200 CHAR)`,
`TIMESTAMP(6) WITH TIME ZONE`, `CLOB`, `NVARCHAR2`, the full list of system
users (`SYS`, `SYSTEM`, `AUDSYS`, `CTXSYS`, `MDSYS`, ...) that must be
skipped, the same table name under two owners, a materialised-view log
(`MLOG$_ORDERS`), multi-line table comments, `PK_`/`FK_` constraint names,
and view definitions using `NVL`, `DECODE`, `TRUNC`, `SYSDATE`, `ROWNUM`,
`TO_CHAR` and `ADD_MONTHS`. Sixteen tests cover reflection, lowercase
questions against uppercase names, meaning that lives only in view SQL,
shadow demotion of `ORDERS_BKP`, scoping, and the Select AI `object_list`
shape. This is not a substitute for a live run; it is everything that can
be tested before one.

## Things that were checked and found to be the database, not schemagate

- PostgreSQL truncates identifiers to 63 bytes at CREATE time. A test asserts
  schemagate keeps whatever the engine kept and adds no second cap.
- PostgreSQL rejects a foreign key to a table that does not exist yet.
  SQLite allows it. The hostile fixture builds its reference cycle with
  inline FKs on SQLite and with ALTER on everything else.
- ATTACHed SQLite databases are per-connection; reflecting them needs the
  attach on every connect. The test fixture does that, and a comment says
  why, because getting it wrong reflects only the default schema silently.

## Dialects

| dialect | how it was verified |
|---|---|
| SQLite | every test, every run |
| PostgreSQL 16 | live instance: the dialect suite, the certification script (10/10), the full 260-object hostile suite, the studio serving it, and the MCP server surviving the database being stopped underneath it |
| Oracle | not run live. 26 static tests pin the SQL, bind types and scope predicate; every statement `OracleStore` emits parses under an independent Oracle SQL parser; the `VECTOR(dimensions, format)` argument order was checked against Oracle's documentation because that parser gets it wrong |
| SQL Server, MySQL | not run live. Reflection uses only SQLAlchemy's dialect-agnostic Inspector, a test fails if any non-universal call appears, and `scripts/certify_dialect.py` certifies an engine in one command |

## Identity scoping

Tested on every schema: a restricted object is absent from the selected
list *and* from the rendered DDL text for a caller without the role, present
for a caller with it, cannot be pulled in by foreign-key expansion, and an
anonymous caller is treated as having no roles. In the MCP server a
restricted object and a missing one return the same error. The shadow
penalty is only applied while the shadowed object is visible to the caller,
so scoping can never make a copy vanish for a reason the caller can't see.

## The AI layer

Everything runs offline against fake providers; no key, no network, no
cost. What is pinned: only schema metadata is ever sent and `ObjectDoc` has
no field a row could live in; a failing provider skips that object and
cataloguing continues; a human hint outranks a generated description;
results cache by content and a changed table is re-described; `model` is
required on every provider because model IDs churn; `import schemagate` loads
none of the provider SDKs, checked in a fresh interpreter.

Measured lift from descriptions, per schema, is in the table at the top.
The descriptions used in tests are hand-written stand-ins for what a
competent model returns; they are labelled as such wherever they appear.

## The MCP server

125 kinds of garbage thrown at every tool in-process, then the same through
a real client over stdio; the next good request must still work. Runs on
MCP SDK 1.x and 2.x. HTTP transport verified on both. The database was
stopped underneath a running server and it kept answering from the last
good index. Passwords are redacted from every output.

## The Studio

The in-browser selector is a JavaScript port. 1,789 cases across all six
schemas — with and without hints, three caller identities, three
`top_k`/expansion settings, explicit shadow naming, and a described-object
case — produce identical object lists, identical reasons, and scores equal
to 1e-9 between Python and JavaScript. blake2b output was compared
byte-for-byte. Python's round-half-to-even was reproduced in the token
estimator so the numbers match.

The local `schemagate studio` was driven by a real Chromium: scoping flips when a
role chip is clicked, the CJK question finds the CJK table, the AI toggle
moves `v_stock_shortfall` from missing to first.

## Provenance

A test scans the tree for machine-local paths and for an email address in
the packaging metadata. `scripts/check_names.py` runs as a test and a
planted term is asserted to fail it. The two hand-written fixtures are
asserted to share under 10% of their vocabulary, so a "second domain" that
is secretly the first domain again cannot be added. Every fixture must
declare itself synthetic.

## Counts

470+ tests. Python 3.10, 3.12, 3.13 run in full; 3.9 is checked for syntax
compatibility. Node is needed only for the parity test, which skips
without it.
