"""Turn a selection into an answer.

Selection is the part this library is careful about; answering is the part
everyone actually wants, and leaving it out made the pipeline look like it
stopped halfway. It genuinely does stop halfway -- a model has to write the
SQL -- so this module is the thin, explicit piece that closes the loop:

    question -> select() -> SQL from a model -> rows

Two things are deliberate here.

The SQL only ever sees the objects ``select()`` returned. That is what makes
the identity boundary mean something: a table the caller may not see is not
in the prompt, so the model cannot write SQL against it, and there is nothing
to filter out afterwards.

And the SQL is checked before it runs. A model asked for a query usually
writes a query, but "usually" is not a basis for handing generated text to a
database, so anything that is not a single read is refused rather than run.
"""
from __future__ import annotations

import re
from typing import Any, List, Sequence, Tuple

__all__ = ["UnsafeSQL", "check_read_only", "sql_prompt", "generate_sql", "run_sql"]

SYSTEM = (
    "You write SQL and nothing else. You are given the only tables you may "
    "use. Answer with one SELECT statement, no prose, no code fences, no "
    "trailing semicolon. Use only the tables and columns shown. "
    # Without this the model answered from one table and called the rest
    # insufficient, with the join tables sitting in the prompt. "One
    # statement" constrains statements, not complexity: a CTE, a window
    # function and a five-table join are all still one SELECT.
    "One statement does not mean one table: join as many of the tables shown "
    "as the question needs, and use CTEs (WITH), window functions, subqueries "
    "and aggregates freely. "
    # The DDL marks the edges the database does not enforce. They are still
    # the intended join, and saying so is what stopped "the contacts of
    # xmagnet" being refused with `contacts` and `tenants` both in the prompt.
    "The -- FK lines give you the joins. A line marked inferred was read from "
    "the column name rather than a declared constraint; it is still the "
    "intended join, so use it. "
    # Asked to rank suppliers by the stock they supply, against a schema whose
    # product table has no supplier column at all, the model wrote a confident
    # three-table query joining on a column it made up. The database caught
    # that one; a hallucinated column that happens to exist would not be
    # caught by anything, and would answer the wrong question quietly.
    "Never invent a column, a table or a join. Every column you name must "
    "appear above, under the table you attach it to. If the tables shown do "
    "not contain the link the question needs, that is not a question you can "
    "answer. "
    "If the tables genuinely cannot answer the question, reply with exactly: "
    "INSUFFICIENT"
)

#: Anything that is not a single read. Checked as whole words so a column
#: named `updated_at` or a table named `deleted_rows` is not mistaken for a
#: statement -- the first version of this rejected `SELECT updated_at ...`.
_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"attach|detach|pragma|vacuum|merge|call|execute|commit|"
    r"rollback|savepoint)\b"
    # REPLACE is the one word in this list that is also an ordinary scalar
    # function, in every dialect: REPLACE(email, '@old', '@new'). Refusing it
    # stopped the model normalising a string inside a join or a GROUP BY --
    # exactly the sort of query someone wants complex SQL for. As a statement
    # it reads `REPLACE INTO t ...` or `REPLACE t ...`, where a table name
    # follows and never an open bracket, so the bracket separates the two.
    # (Those two forms are already refused a line earlier for not starting
    # with SELECT or WITH; this keeps the word barred in the middle as well.)
    r"|\breplace\b(?!\s*\()",
    re.I,
)


class UnsafeSQL(RuntimeError):
    """Generated SQL that would do something other than read."""


def _strip_fences(sql: str) -> str:
    s = sql.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s.strip())
    return s.strip().rstrip(";").strip()


def check_read_only(sql: str) -> str:
    """Return ``sql`` if it is a single read, otherwise raise ``UnsafeSQL``.

    Conservative on purpose. This runs on text a model produced, so the
    question is not "is this probably fine" but "is there any reading of it
    that writes". Comments are stripped first: `-- ` and `/* */` are the
    obvious place to hide a second statement from a naive scan.
    """
    s = _strip_fences(sql)
    if not s:
        raise UnsafeSQL("the model returned nothing")

    bare = re.sub(r"--[^\n]*", " ", s)
    bare = re.sub(r"/\*.*?\*/", " ", bare, flags=re.S)

    # A string literal can legitimately contain any word at all, so blank
    # literals out before looking for statement keywords.
    scan = re.sub(r"'(?:[^']|'')*'", "''", bare)

    if ";" in scan.strip().rstrip(";"):
        raise UnsafeSQL("more than one statement")
    if not re.match(r"^\s*(select|with)\b", scan, re.I):
        raise UnsafeSQL(f"not a SELECT: {s.split()[0][:20]!r}")
    bad = _FORBIDDEN.search(scan)
    if bad:
        raise UnsafeSQL(f"contains {bad.group(0).upper()}")
    return s


def sql_prompt(question: str, fragment: str, dialect: str = "") -> str:
    """The prompt to paste into any chat window when there is no API key."""
    flavour = f" Target dialect: {dialect}." if dialect else ""
    return (
        f"{SYSTEM}{flavour}\n\n"
        f"Tables you may use:\n\n{fragment}\n"
        f"Question: {question}\n"
    )


#: How many times to ask before believing "these tables cannot answer this".
#:
#: The model is not deterministic and cannot be made so -- claude-sonnet-5
#: rejects a `temperature` parameter outright -- so the same prompt over the
#: same tables answers once and declines the next time. Measured against a
#: 1,245-object schema: twenty runs of four questions that all have answers,
#: 35% refused, and one of them produced three different queries in five runs.
#:
#: A refusal is evidence, not a verdict. Asking twice more costs a second on
#: the rare path and turns "it does not work" into "it works", which is the
#: difference between a tool someone keeps and one they close.
_ASK_ATTEMPTS = 3


#: Room for the reply, and for whatever the model does before the reply.
#:
#: 500 was sized for the SQL alone, and that is not what the budget pays for
#: any more. claude-sonnet-5 answers with a `thinking` block first: on a hard
#: nine-table question it spent all 500 tokens thinking, returned no text at
#: all, and `check_read_only` reported "the model returned nothing" -- which
#: reads as a broken model rather than a budget. The same question with 1,500
#: finished in 822. The failure is silent, it only hits the complicated
#: questions, and the tokens are charged either way, so the budget is set
#: where a long query plus its reasoning fits.
_SQL_TOKENS = 2000


def generate_sql(provider, question: str, fragment: str,
                 dialect: str = "", max_tokens: int = _SQL_TOKENS,
                 attempts: int = _ASK_ATTEMPTS) -> str:
    """Ask a provider for one SELECT. Raises ``UnsafeSQL`` if it is not one.

    A model that answers INSUFFICIENT is asked again, up to `attempts` times.
    Nothing else is retried: a reply that is not a SELECT, or that contains a
    write, is raised at once. Those are not flakiness, and re-rolling them
    would be asking a model repeatedly until it gets past a safety check.
    """
    flavour = f" Target dialect: {dialect}." if dialect else ""
    prompt = "Tables you may use:\n\n%s\nQuestion: %s\n" % (fragment, question)
    tries = max(1, attempts)
    for _ in range(tries):
        reply = provider.complete(SYSTEM + flavour, prompt, max_tokens=max_tokens)
        if _strip_fences(reply).upper() != "INSUFFICIENT":
            return check_read_only(reply)
    raise UnsafeSQL(
        "the model said the selected tables cannot answer this, %d times. "
        "That is usually selection, not the model: try --top-k higher, or a "
        "hint." % tries
    )


def run_sql(engine, sql: str, limit: int = 50
            ) -> Tuple[List[str], List[Sequence[Any]]]:
    """Execute a checked read and return ``(column names, rows)``.

    ``check_read_only`` runs again here rather than trusting the caller: this
    is the function that hands text to a database, so it is the one that has
    to be sure.
    """
    from sqlalchemy import text

    sql = check_read_only(sql)
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        cols = list(result.keys())
        rows = result.fetchmany(limit)
    return cols, [tuple(r) for r in rows]


def format_rows(cols: Sequence[str], rows: Sequence[Sequence[Any]],
                width: int = 28) -> str:
    """A small fixed-width table. No dependency, and readable in a terminal."""
    if not cols:
        return "(no columns)"
    def cell(v: Any) -> str:
        s = "" if v is None else str(v)
        return s if len(s) <= width else s[: width - 1] + "…"
    head = [cell(c) for c in cols]
    body = [[cell(v) for v in r] for r in rows]
    widths = [max(len(head[i]), *(len(r[i]) for r in body)) if body else len(head[i])
              for i in range(len(head))]
    out = ["  ".join(h.ljust(w) for h, w in zip(head, widths)).rstrip(),
           "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in body]
    if not body:
        out.append("(no rows)")
    return "\n".join(out)
