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
    "trailing semicolon. Use only the tables and columns shown. If they "
    "cannot answer the question, reply with exactly: INSUFFICIENT"
)

#: Anything that is not a single read. Checked as whole words so a column
#: named `updated_at` or a table named `deleted_rows` is not mistaken for a
#: statement -- the first version of this rejected `SELECT updated_at ...`.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"attach|detach|pragma|vacuum|replace|merge|call|execute|commit|"
    r"rollback|savepoint)\b",
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


def generate_sql(provider, question: str, fragment: str,
                 dialect: str = "", max_tokens: int = 500) -> str:
    """Ask a provider for one SELECT. Raises ``UnsafeSQL`` if it is not one."""
    flavour = f" Target dialect: {dialect}." if dialect else ""
    reply = provider.complete(
        SYSTEM + flavour,
        f"Tables you may use:\n\n{fragment}\nQuestion: {question}\n",
        max_tokens=max_tokens,
    )
    if _strip_fences(reply).upper() == "INSUFFICIENT":
        raise UnsafeSQL(
            "the model said the selected tables cannot answer this. That is "
            "usually selection, not the model: try --top-k higher, or a hint."
        )
    return check_read_only(reply)


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
