---
description: Publish the schemagate launch post to Medium, dev.to and LinkedIn
argument-hint: "[medium|devto|linkedin] — omit to do all three in order"
---

Publish the launch post. Everything you need is in this file; do not rewrite
the copy unless the user asks.

## Order matters — dev.to FIRST

**Publish dev.to first, then import it into Medium.** This is the opposite of
the obvious order and it is deliberate:

Medium closed its API to new integrations in March 2023. No new tokens are
issued, the docs repo is archived, and nothing can post there programmatically.
Its editor also parses no markdown and renders no tables, so a manual paste
means reformatting by hand every time.

Medium's **Import a story** tool (medium.com/p/import) sidesteps all of it. Give
it the published dev.to URL and it pulls the whole article across — headings,
code blocks, the table — and sets the canonical URL back to dev.to on its own,
which is the correct SEO direction anyway.

So: **dev.to → Medium import → LinkedIn.** LinkedIn links to whichever of the
two the user prefers as the public face; default to the Medium URL, since it
reaches a wider non-developer audience and canonical already points home.

If the user explicitly asks for Medium first, the manual paste route is in the
Medium section below — but tell them the import route exists and is less work.

## Facts you must not overstate

The OCI stack reached `6/6 CERTIFIED` on a live tenancy — that is true and the
post says it. But the systemd deadlock fix has not yet completed an *unattended*
run; the certified run needed a manual `systemctl kill`. The post does not claim
hands-off deployment, and `oci/stack/README.md` states the gap in the same
paragraph as the claim. Keep it that way. If asked, that is the honest answer.

The `$7,200 vs $200` figure is a worked example, not an invoice: the token
counts are reproducible (`python tests/bench.py`), the volume and price are
illustrative. Never let it be quoted as "saves $7,000/month" without that.

---

# 1. MEDIUM — via import (preferred)

Once the dev.to post is live, go to **medium.com/p/import**, paste the dev.to
URL, and let Medium pull it in. Check the result, then publish. Medium sets
`canonical_url` back to dev.to automatically.

Add before publishing:

- Cover image, under the subtitle:
  `https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/docs/media/before-after.png`
- Tags (5 max, first is weighted most): `SQL`, `Large Language Models`,
  `Data Engineering`, `Oracle Cloud`, `Python`

## Manual paste — only if the import is refused

Medium does not parse pasted markdown and **cannot render tables at all**.

Type these two lines first — Medium styles line 1 as the title and line 2 as the
subtitle automatically. Type them, do not paste them:

```
Six Tables Out of 260
```

```
Most text-to-SQL systems paste the whole schema into the prompt. Here is what it takes to send six tables instead — and to describe them without an API key.
```

Then paste the body from the dev.to block below, with two changes:
**convert the markdown table into a monospace code block**, and keep every code
line under 55 characters or Medium adds a horizontal scrollbar and hides the
rest.

Check both links resolve and no code block scrolls sideways before publishing.

# 2. DEV.TO

dev.to parses markdown directly, so paste the block below verbatim into the
markdown editor (the `</>` toggle at dev.to/new). The front matter sets title,
tags and cover image — leave it in place.

It is set `published: false`. Save draft, preview, then publish.

This is the canonical home, so it needs no `canonical_url` — Medium's importer
will point back here by itself.

dev.to allows a maximum of 4 tags, which is why its list is shorter than
Medium's.

````markdown
---
title: "Six Tables Out of 260"
published: false
description: "Most text-to-SQL systems paste the whole schema into the prompt. Here is what it takes to send six tables instead — and to describe them without an API key."
tags: sql, ai, python, database
cover_image: https://raw.githubusercontent.com/ashishsinha1602/schemagate/main/docs/media/before-after.png
---

Every text-to-SQL call pays for the schema in the prompt. Dump the whole thing and you pay for every table on every question.

On a 260-object schema, that is **16,095 tokens** before the user's question is even considered.

schemagate sends **444**.

| schema | objects | full schema | schemagate | cut |
|---|---:|---:|---:|---:|
| Commerce | 42 | 2,483 | 604 | 76% |
| Clinical claims | 27 | 1,568 | 543 | 65% |
| Claims warehouse | 51 | 3,312 | 880 | 73% |
| Bank ledger | 39 | 2,255 | 637 | 72% |
| IoT telemetry | 40 | 2,125 | 448 | 79% |
| Hostile | 260 | 16,095 | 444 | **97%** |

The last row is the only one that matters. The selection stays around six tables no matter how large the schema is, so the saving **grows with the schema** — real production databases are the last row, not the first.

At 5,000 questions a day and $3 per million input tokens, that is roughly **$7,200 a month versus $200**. And the selector itself never calls a model, so the selection costs nothing at all.

## How it picks six

**Reflect through SQLAlchemy.** No vendor SQL anywhere, so the same code runs on Postgres, Oracle, SQL Server and MySQL.

**Index the view definitions, not just the names.** This matters more than it sounds. A view exposes only its output columns, so `v_stock_shortfall` looks like it is about "shortfall" when the term you would actually search for — `reorder_point` — is buried in its SELECT. Index the definition and the view becomes findable by what it is really made of.

**Fuse BM25 with vector similarity** using reciprocal-rank fusion. Neither is enough alone. Vectors miss exact identifiers. BM25 misses *"owe us money"* → `balance`.

**Walk foreign keys** to pull in join tables the question never mentions. In my experience this is the single biggest cause of generated SQL that parses and then won't run. Without this step recall drops from 100% to 93.8%.

BM25 plus a hashed n-gram embedder, offline, milliseconds, byte-identical on every machine. One runtime dependency: SQLAlchemy.

## Names are not enough — so catalogue it

Retrieval on identifiers alone hits a ceiling, and I can tell you exactly where it is. Across six test schemas, recall@6 is 100% on five of them. On the sixth — the 42-object schema with every question rephrased in business words rather than table words — it is **50%**.

I left that number in the README rather than dropping the schema. It is the honest boundary of an offline retriever working from identifiers, and it is the reason cataloguing exists.

A catalogue is a one-line description per object, written once:

```bash
schemagate describe --url $URL --all --provider anthropic
```

Any provider works, and so does none: `--provider none` prints a prompt you paste into whatever chat window you already pay for, then paste the answer back. No API key required to get the benefit.

## Cataloguing on Oracle, with no key at all

On OCI there is a third option, and it is the one I am proudest of. A one-click Resource Manager stack stands up a VM and an Autonomous Database, and the instance catalogues the schema through **OCI Generative AI using its instance principal**. No API key is created, stored, or passed. Nothing leaves the tenancy.

Here is Oracle's own model describing a table in the demo schema, unedited:

```
admin.hr_comp: This holds employee base pay and bonus details,
answering how much staff are paid. | salary, wages,
```

Sixteen objects, all described that way, in my tenancy, with no credential of mine involved anywhere. That description is what turns a question phrased as *"how much do we pay people"* into the right table.

## The same step that picks tables can hide them

Once selection is a real step in the pipeline, something else becomes possible that I think is underrated.

Row-level security runs when the query runs. Schema selection runs before that — so an identity-unaware selector will hand the model a table the caller cannot read. The model writes correct SQL, RLS filters every row, and the user is told there are no records. That is a wrong answer in a confident tone, not an access-denied message.

Because schemagate already ranks the catalogue, scoping it by identity is the same step:

```python
cat.restrict("hr_compensation", ["payroll"])

sel = cat.select(
    "salary by employee",
    principal=Principal("okta:jdoe", roles={"finance"}),
)
```

Without the `payroll` role, `hr_compensation` is **absent** — not ranked low. Its name does not appear anywhere in the text the model sees.

> Vanna applied identity when the SQL ran. schemagate applies it before the model sees the schema.

Vanna was archived in March 2026 and people are looking for somewhere to go; a `User` maps to a `Principal` in one line.

## How it is tested

Six schemas ship with the library. The sixth is 260 objects of deliberate sabotage: an `_archive` and `_stg` copy of every table, the same name in three schemas, an 8-deep foreign-key chain, a reference cycle, a 320-column table, names in Spanish and Japanese.

Across the schemas there are 19 cases where a real table competes directly with its own backup copy. It picks the real one 19 times out of 19.

There is also a browser demo running the same selector as a JavaScript port, and a test runs both implementations over 1,789 cases and requires identical rankings. I did not want a demo that flattered the thing it was demonstrating.

## What the Oracle stack cost me

The keyless cataloguing above is certified end to end. Getting there took **nine applies**, and every one found something no `terraform plan` can catch, because only the API rejects it.

**A public Autonomous Database with one-way TLS requires an ACL** — and the ACL has to name the instance's public IP. That is a dependency cycle unless you reserve the address up front and resolve the connection descriptor at boot.

**`LaunchInstance` returned 404-NotAuthorizedOrNotFound** because the stack took `availability_domains[0]`, and that tenancy offers the Always Free shape in AD-3 only. A 404 meaning *"this domain doesn't have that shape"* is a rough thing to debug.

**Three tenancy-scoped names were derived from the compartment OCID** — stable, which was the point, and therefore identical on every run. One leftover from an earlier apply and every later one collided with it.

And then the one that hid all the others for a full day. The MCP endpoint never answered. On any run. I fixed the install. I fixed the boot ordering. I added swap. I was confident each time. Then I finally SSHed into a live instance instead of reasoning from timings:

```
KeyError: getgrnam(): name not found: 'opc'
Running module write_files ... failed
```

One cloud-init file was declared `owner: "root:opc"`. `write_files` runs **before** `users-groups`, so the group does not exist yet. cloud-init threw on the first file and aborted the module — silently discarding every file after it. No scripts, no systemd units, nothing to serve.

> Every fix I had made that day was to code that had never once executed.

Thirty seconds of `ssh` produced the right answer. A day of reasoning from elapsed time produced four plausible, well-argued, entirely wrong ones. All twenty-one tests in the stack suite came from a real apply failing in a real tenancy. Not one from imagination.

## Try it

```bash
pip install schemagate
schemagate demo "which customers owe us money"
```

That runs against a bundled 42-object schema — no database, no key, nothing to configure. Or in the browser, no install and no server: [ashishsinha1602.github.io/schemagate](https://ashishsinha1602.github.io/schemagate/)

Apache-2.0. Python 3.9+. One dependency. Source: [github.com/ashishsinha1602/schemagate](https://github.com/ashishsinha1602/schemagate)

If you are running text-to-SQL against a schema large enough that you have wondered what the prompt costs you, I would like to know what your numbers look like.
````

---

# 3. LINKEDIN

Post this yourself, tagging **Oracle Cloud Infrastructure** as a company
mention. Replace `[MEDIUM LINK]` with the real URL.

This is what makes any Oracle outreach work: people check a profile before
replying, and a technical post about their own product doing something real is
the best thing for them to find.

---

I spent a day getting a one-click Oracle Cloud deployment to work, and the last
bug was one line of YAML.

The thing being deployed is schemagate — it picks the handful of tables an LLM
actually needs before writing SQL. On a 260-object schema that is 16,095 prompt
tokens down to 444. On OCI it also catalogues the schema through OCI Generative
AI using the instance principal. No API key. Nothing leaves the tenancy.

Here is Oracle's own model describing a table in the demo schema:

  admin.hr_comp: This holds employee base pay and bonus details, answering
                 how much staff are paid.

That is a good description. It took nine applies to see it, and every one found
something no `terraform plan` can catch, because only the API rejects it:

→ A public Autonomous Database with one-way TLS requires an ACL, and the ACL has
  to name the instance's public IP. That is a dependency cycle unless you
  reserve the address up front and resolve the descriptor at boot.

→ LaunchInstance returned 404-NotAuthorizedOrNotFound because the stack took
  availability_domains[0], and that tenancy offers the Always Free shape in AD-3
  only. A 404 meaning "this domain doesn't have that shape" is a rough first
  experience.

→ And the one that hid the rest for a day: a cloud-init file declared
  owner: "root:opc". write_files runs before users-groups, so cloud-init threw
  getgrnam(): name not found: 'opc' and aborted the module — silently discarding
  every file after it. No systemd units. Nothing to serve. I'd made four
  confident fixes to code that had never once executed.

All twenty-one tests in the repo's stack suite came from a real apply failing in
a real tenancy. Not one from imagination.

Full write-up: [MEDIUM LINK]
Source: github.com/ashishsinha1602/schemagate

#OracleCloud #OCI #Terraform #AI #SQL

---

## After publishing

Report the three URLs back to the user, and note anything you had to change so
the source files here can be updated to match.
