# Changelog

## 0.1.16

- **Fixed: connecting to a database left the demo on screen.** The page is
  built around the bundled schema, and its hints, its restricted object and
  its example questions are baked in. After connecting they stayed -- so the
  header read "connected to your database" while the rail still said
  `hr_compensation needs payroll`, about a database with no such table. Not a
  cosmetic problem: the page was stating an access rule that did not exist, in
  the one panel someone would check to find out what the access rules are.

  Connecting now replaces the rail with the live catalog's own -- empty, for a
  database nobody has catalogued yet -- clears the question box and the last
  result, and hides the two pieces that only ever applied to the demo: the
  "running entirely in this browser tab" intro, and the pre-written AI
  descriptions toggle.

  Cataloguing follows the connection, which is the half worth testing rather
  than assuming: the describe prompt now names the tables of the database that
  was connected, and none of the demo's.

- **Added: the command that reproduces the connection.** Connecting in a page
  is how someone tries this; a command is how they use it. After a successful
  connect the Studio shows the `schemagate select ... --url ...` line and the
  Python equivalent, carrying the same schemas and flags that were actually
  used -- otherwise it would be a different, quieter connection that happens
  to reach the same database.

  Passwords are placeholders, and the placeholder is `$DB_PASSWORD` rather
  than `***` because the point is a command someone can paste and run. A
  command with a live password in it ends up in a screenshot, a chat message
  and shell history, which are the three places a password is hardest to
  recall from. A wallet connection carries its `SCHEMAGATE_CONNECT_ARGS`,
  since a wallet is not a URL and a command with only `--url` in it would
  connect to nothing.

  Everything stays on the machine that runs it: the server is local, the page
  is local, and nothing is written to disk.

743 tests.

## 0.1.15

Two things 0.1.14 got wrong the moment someone read it.

- **Renamed: `--allow-connect`.** It shipped as `--allow-remote-connect`,
  which reads as "let someone control this machine remotely". It does not do
  that -- it lets the Connect panel hand the server a database URL, and the
  server opens it. A flag guarding a real risk has to describe the real risk,
  or people turn it on to find out what it does. The old spelling still works
  and always will; it is in a published release.

- **Added: `pip install "schemagate[all]"`.** Choosing the extra that matches
  your database is a step nobody should have to take to try something. One
  command now brings every driver, the model SDKs and MCP; `[databases]` is
  the four drivers alone.

  Not the default, and that is deliberate rather than cautious: the base
  install is one dependency, so `import schemagate` cannot fail over a driver
  nobody is using -- there is a test that fails if the core import ever grows
  another. `[all]` also leaves out `huggingface` (a torch download) and `oci`
  (thousands of modules), which are large enough that someone should ask for
  them by name.

734 tests.

## 0.1.14

Connect to a database from the Studio, plus a Windows bug that made the Studio unquittable, a leak in `--values`, and two
things in the repo that were quietly lying.

- **Added: connect from the page.** `POST /api/connect` reflects a database
  into the running Studio, optionally taking visibility from its GRANTs and
  reading column values; `POST /api/run-sql` runs one read-only statement
  through the same guard as `--sql`. A Connect panel and a Run SQL box drive
  both, so pointing Studio at a database no longer means restarting it with a
  different `--url`.

  Connecting is **off** unless the server was started with
  `--allow-remote-connect`. It is the one thing on the page that reaches
  outside the process: with it on, anyone who can reach the Studio can make
  the server connect anywhere it can see, using whatever credentials they
  type. A failed connection reports only the exception type, because driver
  errors quote the URL they were handed and a URL carries a password -- and
  the page clears the URL box on success for the same reason.

  `schemagate studio` also takes `--restrict-from-grants` and `--values`.


- **Added: `schemagate.connect`.** The Connect box asked for a SQLAlchemy URL
  and most people do not have one -- an Oracle team has a wallet zip and a TNS
  alias, a SQL Server team has a JDBC string from a config file.
  `resolve(spec)` returns `(url, connect_args)` from any of: a SQLAlchemy URL,
  a JDBC URL for PostgreSQL, Oracle, SQL Server, MySQL or SQLite, an Oracle
  wallet (directory or the zip as downloaded), or host/port/database fields.
  The Studio picks the kind from a dropdown and shows only the fields that
  kind needs.

  Oracle's service-name form is the one that needed care: JDBC writes
  `@//host:port/service` and SQLAlchemy wants `?service_name=`. The obvious
  translation treats the last segment as a SID, which connects to nothing and
  reports a login failure -- so the symptom points at the password rather than
  at the URL.

  A wallet zip is extracted next to itself, not into a temporary directory:
  the driver re-reads `sqlnet.ora` and the wallet on every reconnect, so a
  directory that vanishes gives a connection that works exactly once. Both the
  flat layout the OCI console ships and a re-zipped one with a folder inside
  are handled, and a zip that writes outside its own directory is refused.

- **Fixed: Ctrl+C did not stop the Studio on Windows.** `main()` waited with
  `threading.Event().wait(3600)`. Python runs a signal handler only between
  bytecodes in the main thread, and Windows has no EINTR to cut a wait short,
  so Ctrl+C sat unhandled for up to an hour and the only way out was killing
  python.exe. On Linux and macOS the wait is interrupted immediately, which is
  why it survived: it was never broken on the machines it was written on. It
  now waits in short slices and closes the listening socket on the way out, so
  an immediate restart does not fail with "address already in use".

- **Fixed: `--values` sent personal data to the model.** It put real row
  values in the prompt with no guard beyond string type, width and distinct
  count. On a live PostgreSQL that meant names and home addresses going to
  whichever provider was configured:

      hr.employee full_name    -> ['A Patel', 'B Osei']
      hr.employee home_address -> ['12 Main St', '9 Kings Rd']

  Three guards. A column already restricted with `restrict_column` is never
  sampled, and restricting one now clears anything sampled earlier. Column
  names that read as personal -- name, address, email, phone, ssn, dob, birth,
  passport -- are skipped by default, and the list is overridable because a
  deny-list is wrong by construction: it misses `nachname`, `nino`, `mrn`. And
  a column must have distinct values well under the row count, so a two-row
  table never qualifies -- three distinct values in a three-row table says
  only that the table is small.

  The guards are a floor, not a boundary. What actually protects a column is
  `restrict_column`, which is exact, and the fact that `--values` is off by
  default.

- **Fixed: a value list looked unfinished.** Rendered into a column list, the
  DDL's own comma landed after the comment -- `one of: 'Acme', 'Globex',` reads
  as a list that continues. The comma belongs to the declaration, so it goes
  before the note. Every comment had the problem; value lists are where it
  showed.

- **Fixed: `studio/build.py` destroyed the Studio.** The template had fallen
  behind the shipped page -- no `mKey`, no `answerPane`, no `catPrompt` -- so
  running the documented build command silently deleted the entire model panel
  and took 25 tests with it. Nothing warned, because a string substitution
  succeeds whether or not the template still has the panel. The template is
  regenerated from the shipped page, the build now reproduces it byte for
  byte, and a test fails if the two ever disagree again.

- **Fixed: `schemagate certify` was a dead command.** It looked for
  `scripts/certify_dialect.py` three directories above the installed module --
  a path that exists in a git checkout and nowhere else. Every pip user got a
  link to GitHub instead of a certification run, and the comment explaining
  the fallback said the script was in the sdist, which it was not. The
  implementation moved into the package; `scripts/certify_dialect.py` still
  works and now calls it.

732 tests. Guards, render, Connect and Run SQL all verified against a
live PostgreSQL 16 -- Connect driven through the browser DOM: 219 objects
reflected, 218 restricted by GRANTs, a nested role expanded, four rows back
from a SELECT and a DROP refused.

## 0.1.13

Two things promised in public and not shipped for twelve releases, a live bug,
and the objection that ends evaluations.

- **Added: column-level restriction.** `Catalog.restrict_column(table, column,
  roles)`. The object-level rule could not express the common case -- the table
  is the right answer and one column in it is not. A restricted column is
  **absent** from the DDL, not masked: `ssn REDACTED` tells a model the table
  holds one, and a model that knows a column exists can ask about it, join on
  it, or mention it in an explanation. Any `-- FK` line naming a withheld
  column is dropped too, since it would put the identifier straight back.

  Object and column visibility now go through one function, `models.allowed`.
  Two copies of a visibility rule drift, and a drifted ACL is the failure this
  library exists to prevent.

- **Added: `Selection.to_dict()`.** The record an auditor asks for afterwards
  and the one thing that cannot be reconstructed later -- the catalog will
  have changed, the roles will have changed, and the question is gone. It
  carries the count of withheld columns, never their names: a log that lists
  what it withheld has disclosed it to everyone who can read the log.

- **Fixed: `--values` did nothing for TEXT columns.** `_candidates` skipped
  unbounded TEXT as "assume prose", which silently disabled the feature for
  every string column in SQLite, where type affinity declares almost
  everything TEXT, and for the many PostgreSQL schemas that use `text` by
  convention rather than `varchar(n)`. Those users got nothing and no
  indication why. Unbounded columns are now sampled and judged on the data
  that comes back rather than the width that was declared.

- **Added: `--restrict-from-grants`** and `schemagate.grants`. At forty tables
  a hand-written restrict map is fine; at four hundred it is a second copy of
  an ACL that already exists in the database, and two copies drift. Reads
  PostgreSQL and Oracle, flattens nested roles transitively, and leaves an
  object with no grant row untouched -- silence is not a denial, and
  restricting on absence would break a working catalog the first time a
  connection could not see everything.

  Two bugs in that SQL were found only by running it against live servers,
  which is why it was not shipped until they had. `pg_get_userbyid(0)` renders
  PUBLIC as `unknown (OID=0)`, so the PUBLIC rule never fired and every
  world-readable table came back restricted to a role nobody holds. And the
  role graph was inverted: it expanded upward to parent roles instead of
  downward to the roles that inherit the granted one, handing an object
  granted to a narrow role to every broader role above it. An over-grant is
  the direction that leaks rather than annoys.

- **Fixed:** `Scored.reason` documented `vector | lexical | fk | pinned` while
  `hybrid` was also emitted -- a value the type said could not occur.

- **Docs:** the README now opens with a command that returns rows rather than
  a list of table names, and the identity claim is two commands with no
  database. `CONTRIBUTING.md` records the rules the tests enforce, including
  that vendor SQL is not tested until an instance has run it.

657 tests, no warnings, ruff and mypy clean across 30 source files. Grant
readers verified against live PostgreSQL 16 and Oracle 26ai.

## 0.1.12

The version that stops needing a terminal, and the one where a model can help
pick the tables rather than only write the SQL.

- **Added: Studio does the whole job now.** It used to take a question and
  show you tables. It now has a model panel (provider, model, key, and
  switches for ranking and answering), an AI catalog panel, and an answer
  panel that shows the SQL and the rows. `schemagate` with no arguments opens
  it. The key is a password field, held in memory for the life of the
  process, never written to disk, and never returned by the API -- so a
  screenshot of Studio does not leak it. A blank key field means "keep the
  one you have", because making someone retype a key to flip a checkbox is
  the friction that ends with keys in shell history.

- **Added: `--rerank`, and the same switch in Studio.** Matching identifiers
  has a ceiling this project has always published: recall@6 is 100% across
  five test schemas and 50% on the one where questions use business words
  rather than table words. Nothing fixes that row by weighting, because
  "doctors" and `provider` share no characters. So the maths narrows hundreds
  of objects to twenty for free, and a model orders those twenty. One small
  call over one-line summaries -- no DDL in the prompt.

  It cannot widen access: the model is handed the list `select()` already
  filtered by principal, so a restricted table is not in the prompt to
  promote. And it cannot make things worse: a provider that raises, times out
  or answers with prose leaves the maths order untouched.

- **Added: cataloguing from the browser, with or without a key.** With a
  model it catalogues in place; without one it hands over the prompt and
  takes the JSON reply back, code fences and all. This is the step that
  raises accuracy most and the one people skip, and until now skipping it was
  the only option in the UI.

- **Fixed: `hidden` did not hide.** It loses to any element carrying an
  explicit display, and both `.pane` and `section` set one, so the answer
  panel sat on the page, empty, whether or not answering was switched on.
  Found by driving the real page in a browser rather than trusting the
  markup.

- **Fixed: parsing a model's reply mistook table names for choices.** A plain
  `\d+` reads `main.t3` as the number 3 and `tbl_183` as 183. The schema this
  was measured against has 180 tables named `stg_feed_007` and
  `audit_event_042`, so every one of them was a live mis-parse. Caught by
  writing the test from a real reply shape before it ever reached a paid
  model.

- **Cleaned:** fourteen unused imports, sixteen type errors, and a warning
  the suite emitted at itself on every run. None broke anything, which is why
  they survived -- but a run with a warning in it trains you to skip reading
  the output, and that is how a real one gets missed.

618 tests, no warnings, ruff and mypy clean across all 29 source files.
Verified live on Oracle 26ai and PostgreSQL 16 running the same 219-object
schema, and through the browser DOM: ask as an analyst, get `billing_payment`;
add the payroll role and one pasted description, and the pay table is first.

## 0.1.11

**Install this rather than 0.1.10 if you use Oracle.** 0.1.10 shipped a
regression that makes reflection hang on a large Oracle database. It was
found by a live apply against an Autonomous Database, where the endpoint
never came up.

- **Fixed: Oracle reflection scanned the whole data dictionary.** 0.1.10
  replaced a per-table type lookup with a single per-database one and dropped
  the `owner = :o` predicate in the process. That predicate is what makes it
  an indexed lookup; without it `all_tab_columns` is a scan across every
  schema the caller can see. Against a local Oracle with 219 objects the
  difference is invisible, which is why it shipped. Against an Autonomous
  Database exposing ~1,500 objects it is not, and it runs during reflection,
  which on the OCI stack happens at boot -- so the MCP endpoint simply never
  opened. It is now scoped per owner: bounded and indexed like the per-table
  query, asked once per schema like the per-database one. One schema of 219
  objects is still one query; ten schemas is ten, not two thousand.

- **Added: `--values`.** A model was handed `status VARCHAR(30)` and wrote
  `WHERE status = 'DENIED'`. The rows say `denied`. The SQL was correct in
  every way a schema can express and returned nothing, which reads as "there
  are no denied claims" rather than as a mistake -- the worst shape of wrong
  answer available. Nothing the model was given could have told it the casing
  of a value it had never seen. Reflection can now read the distinct values of
  short string columns that hold only a handful, and render them as
  `status VARCHAR(30)  -- one of: 'denied', 'paid'`.

  Off by default. Everything else in this library reads metadata; this reads
  rows, which is a different promise about someone's database. Candidates are
  chosen by declared width rather than by names like "status" or "type",
  because the interesting column on someone else's schema is called something
  this code has never heard of. Wide columns are never queried, primary keys
  are skipped, and a column with more distinct values than the cap costs one
  small query and is then dropped.

- **Changed: the stack pins the version it installs.** cloud-init installed
  `schemagate` unpinned, so a release published after someone downloaded the
  stack changed what booted on it. It now installs the version the zip was cut
  with, settable in the console form, with `""` still meaning newest. If the
  pin is not on PyPI yet it says so and installs the newest rather than
  leaving a machine with no schemagate and nothing explaining why.

- **Added:** `oci/stack/verify-release.sh`, one command that checks PyPI has
  the release before applying anything, runs the stack's own 6/6, and then
  checks what 6/6 does not -- the library that was installed on the instance.
  And `examples/oracle_local.py`, which starts Oracle in Docker, builds a
  219-object schema with rows in it, and answers questions, so trying this
  against Oracle does not start with three setup problems.

Verified on live Oracle 26ai and PostgreSQL 16 running the same 219-object
schema: one indexed catalog query, the same four denied claims from both, the
same table withheld from a caller without the role, and the same UPDATE
refused. 562 tests pass.

## 0.1.10

Mostly library. The reflection the server does once it is up changed on both
engines; the stack's behaviour did not, but its files did, and that is worth
stating precisely rather than waving at.

After 0.1.9 was tagged, `main.tf` was split into per-concern files
(`network.tf`, `database.tf`, `compute.tf`, ...) to match the layout Oracle's
quickstart templates use. Comparing the 0.1.9 tag against this release block
by block: 37 blocks then, 37 now, none added, none dropped, and every body
identical once comments and whitespace are normalised — except one `output`
description, reworded. So it is a pure reorganisation, and `terraform
validate` passes, `fmt` is clean, the zip still has its `.tf` at the root, and
`adb_version` still accepts 19c and 26ai while rejecting 23ai and 21c.

What that does *not* prove: the split has never been through a live apply.
0.1.9's `6/6 CERTIFIED` run was against the single-file version. The
reorganisation is as safe as static checking can make it, and it is still
untested against a running Autonomous Database.

Everything below was found by running against a real database. The unit tests
passed throughout; none of these were visible without one.

- **Fixed: a cached engine could hand a later engine the wrong database's
  column types.** The per-engine type cache was keyed on `id(engine)`, and
  CPython reuses the id of a collected object — so an engine opened after an
  earlier one was garbage collected could match that key and be served the
  previous database's types without issuing a query. Reproduced: a fresh
  engine whose column was `integer` came back `geometry(Point,4326)`. The
  cache is now keyed weakly on the engine itself, which also stops it growing
  without bound in a long-lived process.

- **Fixed: PostgreSQL extension tables inside `public` were catalogued as
  user data.** Excluding whole schemas is not enough when the extension is
  installed into `public`, which is the default. `spatial_ref_sys` alone is
  8,500 rows of map projections. A dialect can now name individual objects,
  not just schemas; PostGIS noise drops to zero.

- **Fixed: PostGIS's `topology` schema was missed.** An extension's schema is
  recorded on `pg_extension.extnamespace`; `pg_depend` alone only finds a
  schema the extension's script created separately. Both are read now.

- **Fixed: PostgreSQL enums, domains, geometry and geography reached the
  prompt as `NULL` or `VARCHAR(n)`.** The catalog query contained
  `LIKE 'pg_toast%'`, and psycopg3 reads `%` as a placeholder — the query
  raised and every type fill silently reverted. The query now contains no
  percent sign. `feeling` reads `mood ENUM('sad', 'ok', 'happy')` and `qty`
  reads `positive_int DOMAIN OVER integer`, which is what a model needs to
  write a correct `WHERE`.

- **Fixed: silent failure.** `unknown_types` wrapped its own work in
  `try/except Exception`, so the bug above looked like "no types to fill"
  rather than an error. The registry already guards a database that will not
  answer; the inner catch only hid programming errors, and had been hiding a
  `NameError` on every call. Removed.

- **Changed: Oracle asks for column types once per engine, not once per
  table.** Previously every table with an unrecognised column cost its own
  round trip to the Autonomous Database for an answer that is identical every
  time. Scoped by the same `ORACLE_MAINTAINED` signal the schema filter
  already uses, so it reads no catalog view this module was not already
  reading.

- **Added: `--answer`.** Selecting six tables and stopping there is half a
  pipeline. `--answer` writes the SQL from those six and runs it. The model
  stays yours: with no `--provider` it prints a prompt to paste into any chat
  and calls nobody, `--sql` runs what you paste back, and a provider is used
  only when you name one, with your own key. There is no default model id.
  Every run says which provider and model wrote the SQL, which matters under
  `--provider auto` -- that picks by whichever key is in the environment, and
  you should not have to guess which service received your schema.
  Generated SQL is refused unless it is a single SELECT or WITH, including
  when a second statement is hidden after a `--` or `/* */` comment.

- **Fixed: `describe --provider none` exited asking for a `--model`** it would
  never use -- so the one documented path for people without an API key was
  the one that did not work.

- **Changed: the stack pins the version it installs.** cloud-init installed
  `schemagate` unpinned, so a release published after someone downloaded the
  stack changed what booted on it -- a stack that worked last month failing
  with nothing in the zip to explain why. It now installs the version the zip
  was cut with, settable in the console form, and `""` still means newest. If
  the pin is not on PyPI yet it says so and installs the newest rather than
  leaving a machine with no schemagate and no explanation.

Measured on PostgreSQL 16 with PostGIS, 405 objects across 10 schemas: one
catalog query for the whole reflection (not 405), 2.8 s to bootstrap, 16 ms
median selection, and a 1,037-character prompt covering 6 of 405 objects.

Then verified again on the same 219-object hospital schema built twice, once
on Oracle 26ai and once on PostgreSQL 16, with the same commands against
both: the same four denied claims came back, the same table was withheld from
a caller without the role, and the same UPDATE was refused. The two engines
share nothing but SQLAlchemy and did not diverge.

## 0.1.9

Stack only — no library change.

**Certified unattended.** A run went from apply to a serving MCP endpoint with
nothing typed in between — the port answered 150 seconds after the apply
returned, cataloguing succeeded on its first attempt, and `verify.sh` reached
`6/6 CERTIFIED` and destroyed everything by itself. 0.1.8 got to 6/6 but needed
one manual `systemctl kill` to clear a deadlock; that is the caveat this
release removes.

- **Fixed: the two boot units deadlocked each other.** `resolve-db` wrote the
  connection descriptor and then blocked on `systemctl restart schemagate`.
  The server is ordered `After=` that unit, so systemd would not start it until
  resolve-db finished — and resolve-db could not finish until the restart
  returned. Each waited for the other and the port never opened. Both in-unit
  restarts are `--no-block` now, which queues the job and returns. The endpoint
  went from never answering to answering in 150 seconds.
- **`23ai` is no longer an accepted `adb_version`.** It stops being valid in
  December 2026, and a stack that still offered it would begin failing then
  rather than at a time the operator chose. `19c` remains the default — it is
  the version this stack has actually applied with, four times — and `26ai` is
  selectable for anyone who wants it, available in every commercial region
  except Bogota (BOG), Riyadh (RUH) and Singapore West (XSP). Making 26ai the
  default was tried and pulled: no apply with it set was seen through to
  completion, and a default that has never finished an apply is not a default.
- **PyPI is linked from the stack, which it never was.** The stack README and
  the Resource Manager form both say the instance installs the library from
  <https://pypi.org/project/schemagate/>, and that anyone with a database
  already can skip all of this with `pip install schemagate`. A new `next_step`
  output says the same thing at the end of an apply, where someone who just
  deployed will actually read it.
- `terraform fmt` applied — it had drifted on `main.tf` and `variables.tf` —
  and `terraform validate` passes.
- The README's caveat is narrowed rather than dropped: `verify.sh` reached
  `6/6 CERTIFIED`, and the one remaining gap is that no run has yet gone from
  apply to serving endpoint without the single manual `systemctl kill` that
  cleared the deadlock. The `--no-block` fix for it ships here.

## 0.1.8

Stack only — no library change.

**Certified on a live tenancy.** `verify.sh` ran to `6/6 CERTIFIED` — plan,
apply, the MCP endpoint answering, database reachability from the instance, and
keyless cataloguing through OCI Generative AI (16 objects described by the
instance principal, no API key). One caveat: the server was started by hand on
that run, because `resolve-db` deadlocked on a blocking `systemctl restart`. The
`--no-block` fix below removes the deadlock but has not yet completed an
unattended run; everything downstream of it is certified.

Nine applies to get there, each finding something no `terraform plan` can
catch, and each pinned by a test.

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
- **Fixed: one failed database lookup killed the endpoint permanently.** An
  instance ran for twelve hours with its database present and the MCP port
  dead. `/opt/resolve-db.sh` races an IAM policy that is created alongside the
  database and then has to propagate, and losing that race once was terminal:
  the oneshot failed, `Requires=` made that fatal for `schemagate.service`, and
  nothing retried either. The lookup now has `Restart=on-failure` and keeps
  trying, the server only `Wants=` it so a failure cannot block it for ever,
  and a successful lookup restarts the server rather than waiting for one.
- **`verify.sh` keeps its SSH key in `$HOME`.** It was in the work directory
  under `/tmp`, which Cloud Shell discards when it hands you a new machine
  after an idle disconnect. Twice that left an instance standing with no way
  back into it and no diagnosis. The apply now also prints the `ssh` line.
- **Fixed: the root cause. `write_files` aborted on the first file, every
  time.** `/etc/schemagate.env` was declared `owner: "root:opc"`. write_files
  runs *before* users-groups in cloud-init's init stage, so the opc group does
  not exist yet, and the module raised `getgrnam(): name not found: 'opc'` and
  stopped -- silently discarding every file after it. No `pick-extras.sh`, no
  `resolve-db.sh`, **no systemd units at all**, and then runcmd failed on files
  that were never written. This is why the MCP endpoint never answered on any
  run, on either shape, from the first apply to the eighth: nothing that would
  have served it was ever on disk. Ownership is set in runcmd now, where opc
  exists. Every other fix in this entry was to code that had never executed.
- **Fixed: the two units deadlocked each other.** With the files finally on
  disk, a live boot got further than ever and then stopped: `resolve-db`
  resolved the connection descriptor in 29 seconds and hung on the
  `systemctl restart schemagate` that followed it. `schemagate.service` is
  `After=` that unit, so systemd would not start the server until resolve-db
  finished -- and resolve-db could not finish until the restart returned.
  resolve-db sat in `activating (start)`, the server sat `inactive (dead)`, and
  the port never opened. Both in-unit restarts are `--no-block` now, which
  queues the job and returns.
- `tests/test_oci_stack.py` pins each of these. Twenty-one invariants now, and
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
