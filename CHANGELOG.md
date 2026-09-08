# Changelog

## 0.1.1

Oracle, certified live on Autonomous Database 26ai — and the AI cataloging
that makes retrieval work on real, cryptic schema names.

- **Oracle certified live** on Oracle AI Database 26ai (Autonomous Database):
  `certify_dialect.py` 10/10, the native `VECTOR(512, FLOAT32)` store
  conformance suite, and the dialect suite. Stress-tested against a live
  127-object, 3-domain schema with ~7M rows.
- `SCHEMAGATE_CONNECT_ARGS` reaches every entry point — `introspect.reflect()`,
  the CLI, `Catalog.bootstrap()`, the MCP server and `OracleStore` — so a
  wallet-protected ATP connects everywhere, not just in the certify script
  (`introspect.connect_args_from_env` / `engine_from_url`).
- Per-dialect reflection hooks (`schemagate.dialects`): on Oracle, Autonomous
  Database service schemas (`ORACLE_MAINTAINED`, APEX/ORDS/OML/ODI) are left
  out of reflection, and column types SQLAlchemy reports as `NULL`
  (XMLTYPE, JSON, VECTOR, object types) are recovered from the data dictionary.
- Fixed on live 26ai: the JSON payload `OracleStore` reads back can arrive
  already decoded by the driver.
- **Provider matrix for AI cataloging.** `OCIGenAIProvider` — OCI Generative AI
  with no API key (`~/.oci/config`, resource or instance principal); Cohere and
  generic chat shapes, dedicated endpoints, embeddings. `LocalProvider` — a
  small instruct model via `transformers`, fully offline. `schemagate describe
  --provider oci | local` join `anthropic | openai | gemini | auto` and the
  keyless paste-into-any-chat flow.
- Descriptions without an API key: `Catalog.describe_prompt()` renders one
  prompt for any chat window; `Catalog.describe()` accepts the JSON reply as a
  plain dict; `schemagate describe` does both from the shell and saves into the
  `describe` block of a catalog config, which `select`, `studio` and the MCP
  server all read (`schemagate.config`).
- The `describe` prompt now also asks for the everyday words a person would use;
  they are indexed for retrieval and kept out of the prompt DDL. Blind
  benchmark across six schemas: business-language recall rises from ~56% on
  identifiers alone to ~92% with descriptions (`tests/test_business_language.py`).
- `Catalog.__len__` and `Catalog.objects()`.
- `python -m schemagate` works.
- `pip install schemagate[ai]` no longer drags in the ~100 MB `oci` SDK; that
  lives in `schemagate[oci]`.
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
