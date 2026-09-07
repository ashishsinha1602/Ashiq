# Contributing

Bug reports with a schema that breaks selection are the most useful thing you
can send. The six test schemas in `tests/schema_fixture_*.py` exist because
each one broke something; yours probably will too.

## Setup

    git clone https://github.com/ashishsinha1602/schemagate
    cd schemagate
    pip install -e '.[dev]'
    pytest -q                      # ~430 tests, a few minutes
    python tests/bench.py          # recall and token gates the README quotes

Node is only needed for `tests/test_js_parity.py` (the browser port); it skips
without it. The Studio is rebuilt with `python studio/build.py`.

## Rules the tests enforce

- No vendor-specific SQL in reflection; only SQLAlchemy's Inspector.
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

## Pull requests

One change per PR, tests included, CHANGELOG line added. Keep the README in
the voice it has; no marketing adjectives.
