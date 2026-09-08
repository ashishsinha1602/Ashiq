"""AI-generated catalog descriptions.

Retrieval quality is limited by how much meaning the schema text carries.
``CUST_ORD_LN_T`` with columns ``ID``, ``QTY``, ``AMT`` tells a retriever
almost nothing, which is why the offline embedder scores 50% on questions
phrased in business words rather than identifier words.

A ``SchemaDescriber`` asks a model to write one sentence per object saying
what it holds and when to use it, and stores that on
``ObjectDoc.description``. That text is already part of ``embed_text()``,
so descriptions improve both vector and BM25 retrieval with no other change.

**Only schema metadata is sent.** Object names, column names, types,
nullability, existing comments, and foreign keys. No rows, no sample values,
no query results, no credentials -- ``ObjectDoc`` does not carry row data,
and ``_render`` cannot reach any. There is a test asserting this.

**A human hint always wins.** ``Catalog.hint()`` is applied after
descriptions and outranks them everywhere, so a wrong AI description can be
corrected without regenerating anything.

Descriptions cost money, so results are cached by content: an object is
re-described only when its structure or the model changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Sequence

from ..models import ObjectDoc
from .providers import Provider, ProviderError

_SYSTEM = (
    "You document database schemas for a SQL-generating assistant. "
    "Given one table or view definition, reply with a single sentence, at "
    "most 25 words, saying what the object holds and what question it "
    "answers. Use the business meaning, not the column list. Do not repeat "
    "the object name. Do not speculate about data you cannot see. "
    "Then write ' | ' and 8 to 12 everyday words or two-word phrases a "
    "non-technical person might use when asking about this data -- plain "
    "synonyms and colloquial terms, not column names (for a low-battery "
    "view: flat, dying, dead, charge, power, running out). "
    "No preamble, no markdown, no quotes -- the sentence, a pipe, the words."
)

#: Separates the sentence (shown in prompts) from the everyday words
#: (indexed for retrieval only). One string keeps every existing path --
#: paste-in JSON, --config files, caches -- exactly as it was.
ALIAS_SEP = " | "


def split_description(text):
    """``('sentence', 'word, word, ...')`` from a stored description."""
    if not text or ALIAS_SEP not in text:
        return (text or "").strip(), ""
    head, _, tail = text.partition(ALIAS_SEP)
    return head.strip(), tail.strip()


def _render(doc: ObjectDoc, max_columns: int = 30) -> str:
    """The only thing ever sent to a provider. Metadata, never rows."""
    lines = [f"{doc.kind} {doc.qname}"]
    if doc.description:
        lines.append(f"existing comment: {doc.description}")
    for col in doc.columns[:max_columns]:
        bits = f"  {col.name} {col.type}"
        if col.pk:
            bits += " PK"
        if col.comment:
            bits += f"  -- {col.comment}"
        lines.append(bits)
    if len(doc.columns) > max_columns:
        lines.append(f"  ...{len(doc.columns) - max_columns} more columns")
    for fk in doc.foreign_keys:
        lines.append(f"  FK {','.join(fk.columns)} -> {fk.ref_table}")
    return "\n".join(lines)


def _fingerprint(doc: ObjectDoc, model: str) -> str:
    """Cache key: changes when the object's structure or the model changes."""
    payload = json.dumps({
        "q": doc.qname, "k": doc.kind, "m": model,
        "c": [[c.name, c.type, c.pk, c.comment] for c in doc.columns],
        "f": [[fk.columns, fk.ref_table] for fk in doc.foreign_keys],
        "d": doc.description,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class SchemaDescriber:
    """Generate one-sentence descriptions for catalog objects.

    Parameters
    ----------
    provider
        Any object with ``complete(system, prompt, max_tokens)``.
    cache_path
        JSON file for generated descriptions. Highly recommended: it makes
        re-runs free and keeps a diffable record of what the model wrote.
    workers
        Parallel requests. Keep it modest; providers rate-limit.
    strict
        ``False`` (default) skips objects whose call failed and carries on,
        because a partial catalog still selects. ``True`` re-raises.
    """

    def __init__(self, provider: Provider, cache_path: Optional[str] = None,
                 max_columns: int = 30, workers: int = 4,
                 max_tokens: int = 120, strict: bool = False):
        self.provider = provider
        self.cache_path = pathlib.Path(cache_path) if cache_path else None
        self.max_columns = max_columns
        self.workers = max(1, int(workers))
        self.max_tokens = max_tokens
        self.strict = strict
        self.failures: List[str] = []
        self._cache: Dict[str, str] = self._load_cache()

    # ---------------- cache ----------------

    def _load_cache(self) -> Dict[str, str]:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}   # a corrupt cache must never break cataloguing

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._cache, indent=2, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass    # a read-only disk is not a reason to lose the run

    # ---------------- generation ----------------

    def _describe_one(self, doc: ObjectDoc) -> Optional[str]:
        key = _fingerprint(doc, getattr(self.provider, "name", "?"))
        if key in self._cache:
            return self._cache[key]
        try:
            text = self.provider.complete(_SYSTEM, _render(doc, self.max_columns),
                                          max_tokens=self.max_tokens)
        except ProviderError:
            self.failures.append(doc.qname)
            if self.strict:
                raise
            return None
        text = " ".join((text or "").split()).strip().strip('"')
        if not text:
            self.failures.append(doc.qname)
            return None
        self._cache[key] = text
        return text

    def describe(self, docs: Iterable[ObjectDoc]) -> Dict[str, str]:
        """Return ``{qname: description}``. Objects that failed are absent."""
        docs = list(docs)
        self.failures = []
        if not docs:
            return {}
        if self.workers == 1:
            results = [self._describe_one(d) for d in docs]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(self._describe_one, docs))
        self._save_cache()
        return {d.qname: t for d, t in zip(docs, results) if t}

    # what the provider would receive, for review before spending money
    def preview(self, doc: ObjectDoc) -> str:
        return _render(doc, self.max_columns)

    def estimate_calls(self, docs: Sequence[ObjectDoc]) -> int:
        """How many billed calls ``describe()`` would make right now."""
        name = getattr(self.provider, "name", "?")
        return sum(1 for d in docs if _fingerprint(d, name) not in self._cache)
