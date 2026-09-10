"""Let a model make the final choice, after the maths has narrowed the field.

Selection on identifiers is strong when the question shares vocabulary with
the schema and halves when it does not: measured recall@6 is 100% across five
test schemas and 50% on the one where every question is phrased in business
words rather than table words. That is the honest ceiling of matching names,
and no amount of tuning BM25 gets past it -- "doctors" and `provider` share no
characters at all.

A model does get past it, and it does not need to read the whole database to
do so. The maths already puts the right table in the top twenty almost always;
what it gets wrong is the order within those twenty. So this reranks a short
candidate list rather than replacing retrieval:

    219 objects  --maths-->  20 candidates  --model-->  6

which costs one small call on a list of one-line summaries, not a prompt
containing the schema. It is also why this cannot leak: reranking only ever
sees candidates that ``select()`` already filtered by principal, so a table
the caller may not see is not in the list to be promoted.

Failure is never worse than not using it. A model that errors, times out or
answers with something unparseable leaves the original order untouched.
"""
from __future__ import annotations

import re
from typing import List, Sequence

__all__ = ["SYSTEM", "build_prompt", "parse_choice", "rerank"]

#: A list index, not any digit anywhere. A bare `\d+` reads `main.t3` as the
#: number 3 and `tbl_183` as 183, so a model that helpfully echoes the table
#: names alongside its choice -- which they do -- produces a completely
#: different answer than the one it gave. Found by a test written from a real
#: reply shape; the schema this was measured on has 180 tables named
#: `stg_feed_007` and `audit_event_042`, so every one of them was a live
#: mis-parse waiting to happen.
_INDEX = re.compile(r"(?<![\w.])\d+(?!\w)")

SYSTEM = (
    "You pick the database tables needed to answer a question. You are given "
    "a numbered list of candidates, each with a one-line summary. Reply with "
    "only the numbers of the tables needed, most relevant first, comma "
    "separated, at most {k}. No prose, no explanation. Include a table only "
    "if a query answering the question would read it, and include lookup or "
    "join tables the question does not name if they are needed."
)


def _summary(doc, width: int = 150) -> str:
    """One line per candidate: what the model ranks on.

    The hint or description if there is one -- that is the whole point of
    cataloguing -- and otherwise the column names, which at least carry the
    vocabulary the identifier match was already using.
    """
    text = (getattr(doc, "hint", None) or getattr(doc, "description", None) or "")
    text = " ".join(str(text).split())
    if not text:
        cols = [c.name for c in getattr(doc, "columns", [])[:12]]
        text = "columns: " + ", ".join(cols) if cols else ""
    return text[:width]


def build_prompt(question: str, docs: Sequence, top_k: int) -> str:
    lines = [f"{i + 1}. {d.qname} -- {_summary(d)}" for i, d in enumerate(docs)]
    return ("Candidates:\n" + "\n".join(lines)
            + f"\n\nQuestion: {question}\nReply with at most {top_k} numbers.")


def parse_choice(reply: str, n: int, top_k: int) -> List[int]:
    """The numbers a model actually sent, in order, cleaned up.

    Models reply with "3, 7, 12", "3,7,12", "**3**, 7", a numbered list on
    separate lines, or a sentence with the numbers in it. All of those are
    the same answer, so take the integers in the order they appear and drop
    anything out of range -- a hallucinated `47` in a list of 20 is dropped
    rather than treated as an error, because the rest of the reply is still
    usable.
    """
    seen, out = set(), []
    for tok in _INDEX.findall(reply or ""):
        i = int(tok)
        if 1 <= i <= n and i not in seen:
            seen.add(i)
            out.append(i)
        if len(out) >= top_k:
            break
    return out


def rerank(provider, question: str, docs: Sequence, top_k: int = 6,
           candidates: int = 20, max_tokens: int = 120) -> List:
    """Reorder ``docs`` by asking a model, falling back to the order given.

    ``docs`` must already be the caller's allowed set, in the order the maths
    produced. Only the first ``candidates`` are shown to the model; anything
    below that keeps its place, so a reply that omits a table demotes it
    rather than dropping it from the catalog.
    """
    head = list(docs[:candidates])
    tail = list(docs[candidates:])
    if len(head) <= 1:
        return list(docs)

    try:
        reply = provider.complete(SYSTEM.format(k=top_k),
                                  build_prompt(question, head, top_k),
                                  max_tokens=max_tokens)
        picked = parse_choice(reply, len(head), top_k)
    except Exception:
        return list(docs)           # never worse than the maths alone
    if not picked:
        return list(docs)

    chosen = [head[i - 1] for i in picked]
    rest = [d for d in head if d not in chosen]
    return chosen + rest + tail
