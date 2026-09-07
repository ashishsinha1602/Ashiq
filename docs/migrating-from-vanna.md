# Coming from Vanna

Vanna's repository was archived on 29 March 2026 and is read-only. If you
built on it, this page is about one specific thing it never did, and how to
add that thing to whatever you migrate to — including keeping your Vanna
code as-is.

## What Vanna did with identity, and what it didn't

Vanna 2.0 resolves a `User` with `group_memberships`, gates *tools* on those
groups, and applies row-level security *when the SQL runs*:

```
Execute SQL tool (user-aware) → Apply row-level security → Filtered results
```

That protects the data. It does not protect the answer. The model still sees
the whole schema, still writes valid SQL against a table this user can't
read, RLS strips every row, and the user is told "no records found" — a
wrong answer delivered with confidence. Nothing in that chain can tell the
difference between "there is no data" and "you are not allowed to see it."

schemagate works one step earlier. It decides which tables the model is shown,
per caller, before any SQL exists. A restricted table is not de-ranked; it is
absent from the prompt.

## The mapping

| Vanna | schemagate |
|---|---|
| `vn.train(ddl=...)` (1.x) / tools reading a configured DB (2.x) | `cat = Catalog().bootstrap("postgresql://…")` — reflects the schema once |
| `vn.train(documentation=...)` | `cat.hint("orders", "…")` — a human note that outranks everything |
| `User(id=…, group_memberships=[…])` | `Principal("okta:jdoe", roles={…})` |
| a tool's `access_groups` | `cat.restrict("hr_compensation", ["payroll"])` — on the *table*, not the tool |
| `vn.ask(question)` / `chat_sse` | `sel = cat.select(question, principal=p)`; put `sel.prompt_fragment()` in your SQL prompt |
| `vn.generate_sql(...)` | not schemagate's job — keep whatever model call you have |

schemagate is not an agent, a chat server, or a SQL generator. It is the schema
selection step. Keep your Vanna agent, LangChain chain, or hand-rolled loop;
replace the part that decides what DDL goes in the prompt.

## Minimal example

```python
from schemagate import Catalog, Principal

cat = Catalog().bootstrap("postgresql://localhost/app")
cat.restrict("hr_compensation", ["payroll"])          # once, at startup

def build_prompt(question, user):                      # per request
    p = Principal(f"okta:{user.id}", roles=set(user.group_memberships))
    sel = cat.select(question, top_k=6, principal=p)
    return f"Schema:\n{sel.prompt_fragment()}\n\nQuestion: {question}"
```

Your `User` object drops straight in: `id` becomes the principal's subject,
`group_memberships` become its roles. Whatever generated SQL before still
does — it just never sees `hr_compensation` unless the caller holds
`payroll`.

## If you're using an agent framework

Claude Desktop, Cursor, or any MCP client: run `python -m schemagate.mcp_server`
and the agent calls `select_schema(question, principal, roles)` as a tool.
LangChain: `SchemagateRetriever` is a `BaseRetriever`. Both are in the README.

## What you lose, honestly

Vanna trained on question/SQL pairs and learned from feedback. schemagate does
not learn; it reflects and ranks. If your accuracy came from a large
question–SQL memory, keep that memory and use schemagate only for the identity
gate. If it came from schema documentation, `cat.hint()` and
`cat.describe()` do the same job with less machinery.
