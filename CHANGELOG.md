# Changelog

## 0.1.4

An audit of the OCI stack — which had never been applied — found that its
headline feature never ran and that a default deploy could not have worked.

- **Fixed: `describe --provider oci` ignored instance-principal auth.** On an
  OCI VM there is no `~/.oci/config`; the machine authenticates as itself. The
  CLI built `OCIGenAIProvider` with the default `auth="config"`, failed with
  `ConfigFileNotFound`, and the stack's `|| true` swallowed it — so the
  first-boot cataloguing the README promised silently never happened. The CLI
  now honours `OCI_CLI_AUTH`, as every other OCI tool does.
- **The stack now creates a service gateway.** The database's access-control
  list admits the VCN, and Oracle only honours a VCN entry when traffic arrives
  through a service gateway. Without one the database refused the VM's
  connections — the stack applied cleanly and never worked.
- The DSN is now selected as the LOW, server-authentication profile rather than
  `profiles[0]`, which can be a mutual-TLS profile that thin-mode
  python-oracledb cannot use without a wallet.
- `adb_version` defaults to 19c: Always Free offers it in every home region,
  while 26ai and 23ai exist in only a few and 23ai stops being a valid value in
  December 2026.
- Database, dynamic-group and policy names are suffixed from the compartment,
  so a second deploy in one tenancy no longer collides.
- The image lookup asserts it found one instead of indexing an empty list.
- `allowed_cidr` and `ssh_cidr` are now separate and have **no defaults** — the
  MCP endpoint has no authentication of its own, so the stack refuses
  `0.0.0.0/0` rather than shipping an internet-facing schema browser.
- Password validation matches Oracle's actual rule, including its rejection of
  passwords containing "admin".
- cloud-init: online `firewall-cmd` instead of `firewall-offline-cmd`, an env
  file the documented re-run command can actually read, and cataloguing output
  captured to `/var/log/schemagate-catalog.log` instead of discarded.
- The stack README now states plainly that it has not been applied end to end,
  and lists the home-region, tenancy-admin and credential-exposure caveats.

## 0.1.2

One click onto Oracle Cloud, and the OCI provider no longer truncates.

- **Deploy to Oracle Cloud.** Resource Manager accepts a stack from a zip URL,
  so the button in the README gives the same one-click install a Marketplace
  listing would, with no partner membership, supplier registration or separate
  tenancy involved. Each release now carries `schemagate-oci-stack.zip` with the
  Terraform at the zip root, which is what Resource Manager reads.
- `oci/quickstart.sh`: schemagate against an existing Autonomous Database from
  OCI Cloud Shell in about a minute — no VM, no Terraform, no API key.
- `oci/stack/` catalogues on first boot: a dynamic group and policy let that one
  instance call OCI Generative AI through its instance principal, so
  descriptions are written with no key and no prompt leaving the tenancy.
- **Fixed: descriptions arrived truncated from OCI.** `_oci_text` read only the
  first content part of a reply, so every Gemini description on a live run was
  cut off mid-sentence at about ten tokens and business-language recall fell
  twenty points with nothing logged. It now walks every response shape the
  service returns.
- A reply that stops mid-clause is retried with real headroom, and anything
  still short is collected on `SchemaDescriber.truncated` and raised as one
  `RuntimeWarning`. A provider that caps output can no longer degrade a catalog
  in silence.

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
