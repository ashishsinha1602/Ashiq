# Changelog

## Unreleased

- Descriptions without an API key: `Catalog.describe_prompt()` renders one
  prompt for any chat window; `Catalog.describe()` accepts the JSON reply as a
  plain dict; `schemagate describe` does both from the shell and saves into the
  `describe` block of a catalog config, which `select`, `studio` and the MCP
  server all read (`schemagate.config`).
- `Catalog.__len__` and `Catalog.objects()`.
- `scripts/certify_dialect.py` takes driver options via `SCHEMAGATE_CONNECT_ARGS`
  (wallets don't fit in a URL).
- README hero image, badges, SECURITY/CONTRIBUTING/CITATION, MCP registry
  manifest.

## 0.1.0

First release. (Published for one day as `ashiq` 0.1.0 before the rename;
that name now installs this package and warns.)

- Identity-scoped schema selection: reflect any SQLAlchemy dialect, retrieve
  with BM25 + vector fusion, expand foreign keys, filter by caller before the
  model sees anything.
- Offline default embedder, deterministic across processes and machines.
- Optional AI cataloguing through Claude, GPT, Gemini or any callable; nothing
  is sent unless you ask, and never row data.
- `OracleStore` on Oracle 23ai native VECTOR (statically verified; live run
  pending).
- `schemagate` command line with a no-database demo.
- MCP server (`python -m schemagate.mcp_server`) and a LangChain retriever.
- Unicode identifiers, including CJK, with accent folding.
- Backup and staging copies (`_bkp`, `_old`, `_v2`, `stg_` ...) are ranked
  below the object they shadow, unless named outright.
- `schemagate studio`: a local page on your own database, and a hosted demo of the
  same page running a JavaScript port of the selector (parity-tested).
- Claims-warehouse star schema fixture: grain, SCD2, role-playing dates,
  bridges, fifteen backup copies.
- Six benchmark schemas (commerce, clinical, claims warehouse, bank ledger,
  IoT telemetry, hostile), 430+ tests, dialect certification script,
  TESTING.md.
