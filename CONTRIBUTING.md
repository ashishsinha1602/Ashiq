# Contributing

Bug reports with a schema that breaks selection are the most useful thing you
can send. The six test schemas in `tests/schema_fixture_*.py` exist because
each one broke something; yours probably will too.

## Setup

    git clone https://github.com/ashishsinha1602/schemagate
    cd schemagate
    pip install -e '.[dev]'
    pytest -q                      # ~660 tests, about a minute
    python tests/bench.py          # recall and token gates the README quotes

Node is only needed for `tests/test_js_parity.py` (the browser port); it skips
without it. The Studio is rebuilt with `python studio/build.py`.

## Rules the tests enforce

- No vendor-specific SQL in reflection; only SQLAlchemy's Inspector. Vendor
  SQL lives in `dialects/` or `grants.py`.
- Anything touching visibility fails closed. There is one rule,
  `models.allowed`, and both the object and column checks use it. Two copies
  of a visibility rule drift, and a drifted ACL is the failure this library
  exists to prevent.
- Nothing reads rows unless asked. `--values` is the single exception and it
  is off by default: reading rows is a different promise to make about
  someone's database than reading metadata.
- A restricted object must be absent from both the list and the DDL for a
  caller without the role. `tests/test_identity.py` and `test_isolation.py`.
- If you change ranking in Python, change `studio/schemagate.js` too; the
  parity test compares scores to 1e-9.
- Every fixture declares itself synthetic; no real schema names.
- `scripts/check_names.py` must stay clean.

## Adding a schema

Copy the shape of `tests/schema_fixture_finance.py`: DDL, hints, a restricted
object, golden questions with the objects they must retrieve, and a
description dict labelled as hand-written. Add it to `tests/bench.py`.

## Vendor SQL is not tested until an instance has run it

The grant readers in `grants.py` were written, unit-tested against fakes, and
then found wrong twice the first time they met a live PostgreSQL. One bug
would have silently disabled the PUBLIC rule; the other expanded nested roles
in the wrong direction and **over**-granted. Neither was reachable without a
server.

So if you touch `grants.py` or `dialects/`, say in the pull request which
engine and version you ran against. These are enough:

    docker run -d --name orafree -p 1521:1521 -e ORACLE_PASSWORD=secret \
      gvenzl/oracle-free:23-slim
    docker run -d --name pg16 -p 5432:5432 -e POSTGRES_PASSWORD=secret postgres:16

## Pull requests

One change per PR, tests included, CHANGELOG line added. Keep the README in
the voice it has; no marketing adjectives.
