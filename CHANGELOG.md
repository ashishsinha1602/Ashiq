# Changelog

## Unreleased

Stack only — no library change. Four applies against a live tenancy, each one
finding something no `terraform plan` can catch.

- **Fixed: the database could not be created.** One-way TLS on a public
  Autonomous Database requires an access-control list, and 0.1.7 had removed
  it. The list has to name the instance's public IP, so that address is now
  reserved up front and attached to the VNIC; the database is created after
  the instance, and `/opt/resolve-db.sh` looks the connection descriptor up at
  boot through the instance principal rather than Terraform baking it into
  cloud-init (which would be a dependency cycle).
- **Fixed: `LaunchInstance` returned 404-NotAuthorizedOrNotFound.** The stack
  took `availability_domains[0]`; a live Phoenix tenancy offers
  `VM.Standard.E2.1.Micro` in AD-3 only. It now asks each domain which shapes
  it offers and picks one that has the shape.
- **Fixed: `verify.sh` tore down a successful plan.** `wait_job` printed its
  progress on stdout, which the caller was capturing, so the state it returned
  was `ACCEPTED\r   IN_PROGRESS\rSUCCEEDED` and never matched. It also split
  the job log on commas instead of decoding it, dropping the `Suggestion:` and
  `Request Target:` lines that say what actually failed.
- **The first boot is faster, and the endpoint check waits long enough.** The
  fourth run applied cleanly and then failed the endpoint check. Oracle spends
  most of ten minutes provisioning the database, and the lookup that waits for
  it was running *after* the pip install rather than underneath it — the two
  waits added up. The lookup now starts at the top of `runcmd` and blocks on a
  marker the install writes, so the install is off the critical path; the
  lookup uses the OCI Python SDK already in the venv instead of installing the
  CLI; pip no longer upgrades itself first, prefers wheels, and skips a cache
  nothing reads twice. `verify.sh` waits fifteen minutes and says what it is
  waiting for.
- **The install no longer byte-compiles.** A fifth run applied cleanly and the
  endpoint still had not answered eighteen minutes into the boot. The database
  was `AVAILABLE` before the apply even returned, so the wait was not the
  database -- it was pip compiling the OCI SDK's thousands of modules on one
  burstable OCPU. `--no-compile` removes that; Python compiles what it imports,
  and this venv imports a fraction of it. The SDK is also now installed only
  when something needs it, which takes it off the boot path entirely for a
  stack pointed at your own database. `verify.sh` takes `SHAPE`, `OCPUS` and
  `MEMORY_GBS`, and on a timeout it prints what the instance was doing before
  tearing it down.
- **Fixed: a second run in the same tenancy could not apply.** The dynamic
  group, the policy and the database name were all derived from
  `md5(compartment_ocid)`, which is the same on every run in that compartment.
  One leftover from an earlier run -- a destroy that did not finish -- and the
  next apply failed with *"DynamicResourceGroup with the same displayName
  already exists"* and *"a database named sg... already exists"*. All three are
  now keyed on the VCN's OCID, which is unique to the apply. The database's
  display name is too, which also stops the boot-time lookup resolving an
  abandoned database instead of this one. `verify.sh` reports leftovers before
  it starts, and the README says how to remove them.
- **Fixed: cloud-init died before it ever reached the install.** Seven applies
  in, an SSH into a still-running instance showed why the endpoint never
  answered: no venv marker, no resolve log, no units -- but `/etc/schemagate.env`
  written, cloud-init dead inside `update_package_sources` on a signal, and a
  load average of 6.3 on a single core. `write_files` had run and `runcmd` never
  started. VM.Standard.E2.1.Micro is one burstable OCPU and one gigabyte of RAM,
  the Oracle Linux image ships no swap, and dnf's metadata refresh alone can
  exceed that. The boot now creates a 2 GB swapfile before any package work and
  no longer refreshes every repository's metadata to install one package that is
  in the base repository. Every earlier fix on this boot path had never once
  executed.
- `tests/test_oci_stack.py` pins each of these. Seventeen invariants now, and
  every one of them came from a failure a live apply produced.

## 0.1.7

**Applying the stack in a real tenancy for the first time found a bug that no
`terraform plan` can catch, because only the API rejects it. The apply still
does not complete** — the fix below moved the failure rather than removing it;
see *Still broken* at the end of this entry.

- **Fixed: the stack could never apply.** 0.1.4 added a service gateway so the
  database's access-control list could name the VCN — Oracle honours a VCN
  entry only when traffic arrives through one. The OCI API refuses a route
  table that holds both a service gateway for all services and an internet
  gateway default route: *"Internet Gateway target cannot be used together
  with Service Gateway target for All Services in the same routing table"*.
  The instance needs the internet gateway to install anything at all, so the
  service gateway is gone. Every apply since 0.1.4 would have failed here,
  after provisioning the database and before creating the instance.
- With the service gateway goes the VCN-scoped access-control list: naming the
  instance's public IP instead is circular, since cloud-init already carries
  the database's connection descriptor. The demo database this stack creates
  is therefore reachable over TLS with the ADMIN password and nothing else —
  it is created empty and destroyed with the stack, and the README says so
  plainly. New `adb_allowed_cidrs` narrows it, and `create_adb = false`
  against your own database remains the path for real data.
- **Fixed: `verify.sh` reported nothing when a job failed.** It waited with
  `oci resource-manager job get --wait-for-state`, which that subcommand does
  not accept; the non-zero exit tripped `set -e` and ran the teardown trap
  before any diagnostic printed, so a failed plan looked like the script
  silently skipping three steps. Every wait is now an explicit poll that
  prints the state it sees, and both failure paths dump the job's error lines.
- `tests/test_oci_stack.py` pins the routing rule and asserts `schema.yaml` and
  `variables.tf` declare the same variables.

**Still broken after this release.** Removing the access-control list made the
database itself invalid: with `is_mtls_connection_required = false` and no
list, Oracle rejects the create with *"One-way TLS connections require a
private endpoint or a public IP with an ACL"*. The apply now gets as far as the
database and fails there. Restoring the list needs the instance's public IP,
and the instance cannot precede the database because its cloud-init carries the
connection descriptor — so this needs a real change, not a parameter. Use the
Cloud Shell route in `oci/README.md` until then.

## 0.1.6

The stack's keyless cataloguing raced the permission that authorises it, and
there is now a script that proves a deployment works rather than asserting it.

- **Fixed: first-boot cataloguing raced its own IAM policy.** The dynamic
  group's matching rule needs the instance OCID, so Terraform cannot create the
  group or the Generative AI policy until the instance exists — by which time
  the instance is already running `catalog-once.sh`. Authorisation also takes
  minutes to propagate after the policy is written. The single attempt on first
  boot usually lost that race and failed with `NotAuthorizedOrNotFound`, which
  the stack logged and moved past, leaving a catalog with no descriptions in it.
  Cataloguing now retries for twelve minutes and restarts the server when it
  succeeds.
- The MCP endpoint now comes up *before* cataloguing rather than after, so the
  retry window no longer holds the service down.
- `oci/stack/verify.sh`: end-to-end certification of the stack in your own
  tenancy, through Resource Manager — the same path the Deploy button takes. It
  uploads the released zip, plans, applies, then checks the machine rather than
  the plan: the MCP port answers, the instance can reach the database through
  the service gateway, and cataloguing genuinely called OCI Generative AI
  through the instance principal. Destroys everything afterwards. It generates
  its own key and password and narrows both CIDRs to the calling shell.
- `tests/test_oci_stack.py` pins the cloud-init template contract and the
  ordering above; nothing else in the suite would have noticed either.

## 0.1.5

**PostgreSQL users on 0.1.1-0.1.4 should upgrade: their catalog was empty.**

- **Fixed: PostgreSQL reflected nothing at all.** 0.1.1 added an
  internal-schema filter for Oracle Autonomous Database and put `public` on
  the list, because `PUBLIC` is a pseudo-schema on Oracle. On PostgreSQL
  `public` is the user's entire database, so every schema was filtered out and
  `Catalog.bootstrap()` returned zero objects. Caught by running
  `scripts/certify_dialect.py` against a live PostgreSQL 16, which failed 7 of
  10 checks.
- Internal-schema detection now lives behind a per-dialect hook
  (`schemagate.dialects.is_internal_schema`) and applies only to the engine
  that defines it. A test asserts no cross-dialect list exists in
  `introspect.py`, because that is what caused this.
- **PostgreSQL 16 re-certified live**: certify script 10/10, plus the dialect
  and 260-object hostile suites.
- The OCI stack installs the driver matching your database URL instead of
  always Oracle, so `create_adb = false` with a PostgreSQL, SQL Server or MySQL
  URL now works rather than failing at import.
- Stack: `shape_config` for Flex shapes (A1.Flex is the other Always Free
  option and would 400 without it), a selectable availability domain for the
  "out of host capacity" case, GenAI policy scoped to the compartment instead
  of the tenancy, and preconditions that catch a missing password or database
  URL at plan time.

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
