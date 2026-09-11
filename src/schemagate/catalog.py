"""Catalog: identity-scoped schema selection.

Retrieval is hybrid. Vector similarity alone under-performs badly on schema
text because identifiers are not sentences; BM25 alone misses paraphrase
("owe us money" -> balance). Ranks from both are fused with Reciprocal Rank
Fusion, then foreign-key expansion pulls in join tables the question never
names -- the single biggest cause of unrunnable generated SQL.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .embedder import Embedder, HashingEmbedder, tokenize
from .identity import Principal
from .models import ObjectDoc, Scored, Selection, allowed
from .stores.memory import MemoryStore

_RRF_K = 60

#: Name suffixes that, in practice, mean "a copy of the table without this
#: suffix". An object is treated as a shadow only when the un-suffixed base
#: object also exists in the catalog, so a standalone ``orders_v2`` with no
#: ``orders`` is never touched. Override with ``Catalog(shadow_suffixes=...)``
#: or disable with an empty sequence.
DEFAULT_SHADOW_SUFFIXES = (
    "_bkp", "_backup", "_bak", "_old", "_tmp", "_temp", "_new", "_copy",
    "_archive", "_arch", "_hist", "_stg", "_staging", "_v1", "_v2", "_v3",
    "_prev", "_orig",
)
# Deliberately absent: "_test" and "_dev" (and "test_", "dev_" below). In a
# clinical schema lab_test is a real table and in a telemetry schema dev_
# means device; demoting a whole domain by accident is worse than missing
# a copy. Add them yourself if your naming convention is unambiguous.

#: How much a shadow's fused score is multiplied by. 0.5 is enough to put it
#: below its base when they would otherwise tie, and not enough to hide it
#: from a question that names it outright ("the v2 claim line table").
SHADOW_PENALTY = 0.5

#: Prefixes that mark a staging or scratch copy of some other object. An
#: object is a shadow only when another, non-shadow object shares its stem
#: once layer prefixes (dim_, fact_, v_ ...) are stripped: ``stg_member`` is
#: demoted because ``dim_member`` exists; a lone ``stg_events`` is not.
DEFAULT_SHADOW_PREFIXES = (
    "stg_", "staging_", "tmp_", "temp_", "bkp_", "backup_", "old_",
    "copy_", "scratch_", "wip_",
)

#: Layer prefixes ignored when matching a staging copy to its real object.
_LAYER_PREFIXES = ("dim_", "fact_", "fct_", "f_", "d_", "v_", "vw_", "view_",
                   "bridge_", "br_", "tbl_", "t_", "agg_", "mv_")


def _named_in(question_tokens: List[str], doc: ObjectDoc) -> bool:
    """True if the question spells out this object's name.

    A heuristic must never override what the user literally typed: if they
    ask for ``fact_claim_line_v2`` by name, the shadow penalty does not
    apply. Name tokens must appear contiguously, so "claim line" does not
    count as naming ``fact_claim_line_v2``.
    """
    name = tokenize(doc.name)
    if not name or len(name) > len(question_tokens):
        return False
    n = len(name)
    return any(question_tokens[i:i + n] == name
               for i in range(len(question_tokens) - n + 1))


class _BM25:
    """Small in-memory BM25. Rebuilt on bootstrap; schemas are not big."""

    def __init__(self, docs: Sequence[str], k1: float = 1.4, b: float = 0.72):
        self.k1, self.b = k1, b
        self.docs = [tokenize(d) for d in docs]
        self.len = [len(d) for d in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        self.tf = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> List[float]:
        q = tokenize(query)
        out = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1))
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / denom
            out.append(s)
        return out


class Catalog:
    """Reflect a schema once, then select a small relevant subset per question."""

    def __init__(self, embedder: Optional[Embedder] = None, store=None,
                 name: str = "default",
                 shadow_suffixes: Sequence[str] = DEFAULT_SHADOW_SUFFIXES,
                 shadow_prefixes: Sequence[str] = DEFAULT_SHADOW_PREFIXES):
        self.embedder = embedder or HashingEmbedder()
        self.store = store or MemoryStore()
        self.name = name
        self.shadow_suffixes = tuple(x.lower() for x in shadow_suffixes)
        self.shadow_prefixes = tuple(x.lower() for x in shadow_prefixes)
        self._docs: Dict[str, ObjectDoc] = {}
        self._order: List[str] = []
        self._bm25: Optional[_BM25] = None
        self._shadows: Dict[str, str] = {}      # shadow qname -> base qname
        self._stale = True

    # ---------------- build ----------------

    def add(self, doc: ObjectDoc) -> None:
        self._docs[doc.qname] = doc
        self._stale = True

    def add_all(self, docs: Iterable[ObjectDoc]) -> None:
        for d in docs:
            self.add(d)

    def hint(self, table: str, text: str) -> None:
        """Human correction. Beats any generated description, and is the
        cheapest accuracy lever in the whole library.

        A hint is part of the indexed text, so it marks the index stale;
        the next ``select()`` rebuilds automatically. Call ``index()``
        yourself only if you want to pay that cost at a chosen moment.
        """
        for qname, doc in self._docs.items():
            if qname == table or doc.name == table:
                doc.hint = text
                self._stale = True
                return
        raise KeyError(f"{table!r} not in catalog")

    def describe(self, describer, only_missing: bool = True) -> int:
        """Fill in ``description`` for catalog objects using a describer.

        Entirely optional: without it the catalog uses whatever comments the
        database already carries. See ``schemagate.ai`` for describers backed by
        Anthropic, OpenAI or Gemini, or pass anything with a
        ``describe(docs) -> {qname: text}`` method.

        ``only_missing=True`` (default) skips objects that already have a
        database comment or a human hint, so you only pay for the objects
        that need help. Returns how many descriptions were written.

        Human hints set with ``hint()`` outrank descriptions everywhere, so
        a wrong description is corrected without regenerating anything.
        """
        targets = [d for d in self._docs.values()
                   if not (only_missing and (d.description or d.hint))]
        if not targets:
            return 0
        if isinstance(describer, Mapping):
            # No key, no SDK: descriptions you already have -- from
            # describe_prompt() pasted into any chat, from a colleague's
            # file, from anywhere. Keys may be qualified or bare names.
            by_name = {d.name: d.qname for d in targets}
            by_qname = {d.qname for d in targets}
            written = {}
            for key, text in describer.items():
                q = key if key in by_qname else by_name.get(key)
                if q and str(text).strip():
                    written[q] = str(text).strip()
        else:
            written = describer.describe(targets)
        for qname, text in written.items():
            if qname in self._docs:
                self._docs[qname].description = text
        if written:
            self._stale = True
        return len(written)

    def describe_prompt(self, only_missing: bool = True, max_columns: int = 30) -> str:
        """A single prompt you can paste into any chat -- ChatGPT,
        Gemini, a local model -- to get descriptions without an API key.

        The reply is JSON mapping object name to a one-sentence description;
        feed it back with ``describe(json.loads(reply))`` or
        ``schemagate describe --apply reply.json``. Only metadata is in the
        prompt: names, types, comments, foreign keys. Never rows.
        """
        from .ai.describe import _SYSTEM, _render
        targets = [d for d in self._docs.values()
                   if not (only_missing and (d.description or d.hint))]
        if not targets:
            return ""
        parts = [_SYSTEM, "",
                 "Do this for every object below. Reply with ONLY a JSON object "
                 "mapping each object's full name (exactly as written, e.g. "
                 f'"{targets[0].qname}") to its one-sentence description. '
                 "No markdown fences, no commentary.", ""]
        for d in targets:
            parts.append(_render(d, max_columns))
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    def restrict_column(self, table: str, column: str,
                        roles: Sequence[str]) -> None:
        """Make one column visible only to principals holding one of ``roles``.

        For the case the object-level rule cannot express: the table is the
        right answer and one column in it is not -- salary on an employee
        table, a national insurance number on a patient. Restricting the whole
        object would make the question unanswerable; leaving it open puts the
        column in the prompt.

        Raises ``KeyError`` for an unknown table or column rather than
        succeeding quietly. A typo in an ACL that reports success is a
        restriction that silently is not there.
        """
        for qname, doc in self._docs.items():
            if qname == table or doc.name == table:
                for col in doc.columns:
                    if col.name == column:
                        col.roles = list(roles)
                        # Values sampled before the restriction was applied
                        # would otherwise sit on the column, reachable by
                        # anything that reads `Column.values` directly. The
                        # DDL already omits a restricted column, but the data
                        # should not survive the restriction either.
                        col.values = None
                        return
                raise KeyError(f"{table!r} has no column {column!r}")
        raise KeyError(f"{table!r} not in catalog")

    def restrict(self, table: str, roles: Sequence[str]) -> None:
        """Make an object visible only to principals holding one of ``roles``.

        Visibility is applied at select time, not at index time, so this
        takes effect immediately and needs no reindex.
        """
        for qname, doc in self._docs.items():
            if qname == table or doc.name == table:
                doc.roles = list(roles)
                return
        raise KeyError(f"{table!r} not in catalog")

    def bootstrap(self, engine_or_url=None, include=None, exclude=None,
                  schemas=None, include_views=True,
                  sample_values: bool = False,
                  sample_budget: float = 30.0) -> "Catalog":
        """``sample_values`` reads a little data as well as the catalog: for
        short string columns holding only a handful of distinct values, it
        puts those values in the prompt. Off by default -- everything else
        here reads metadata only."""
        if engine_or_url is not None:
            from .introspect import reflect
            self.add_all(reflect(engine_or_url, include=include, exclude=exclude,
                                 schemas=schemas, include_views=include_views,
                                 sample_values=sample_values,
                                 sample_budget=sample_budget))
        self.index()
        return self

    def index(self) -> "Catalog":
        store_dim = getattr(self.store, "dim", None)
        if store_dim is not None and store_dim != self.embedder.dim:
            raise ValueError(
                f"store expects {store_dim}-dim vectors but "
                f"{self.embedder.name!r} produces {self.embedder.dim}; "
                "recreate the store with dim= matching the embedder"
            )
        self._order = list(self._docs)
        texts = [self._docs[q].embed_text() for q in self._order]
        self._bm25 = _BM25(texts)
        self._shadows = self._find_shadows()
        vecs = self.embedder.embed(texts)
        self.store.purge(self._ns)
        for qname, vec in zip(self._order, vecs):
            self.store.upsert(self._ns, qname, vec, {"qname": qname})
        self._stale = False
        return self

    def _find_shadows(self) -> Dict[str, str]:
        """Map each backup/staging-style object to the real object it shadows.

        Two patterns, both requiring the real object to exist:

        * suffix: ``fact_claim_line_bkp`` -> ``fact_claim_line``
        * prefix: ``stg_member`` -> ``dim_member`` (stems match once layer
          prefixes are stripped)

        Same schema only: ``billing.account_old`` shadows ``billing.account``,
        never ``crm.account``.
        """
        def stems_of(name: str) -> List[str]:
            """The name itself, the name minus a known layer prefix, and the
            name minus its first segment -- so ``stg_trade`` can find
            ``trd_trade`` and ``stg_member`` can find ``dim_member``, while
            a name with no underscore only matches itself."""
            out = [name]
            for prefix in _LAYER_PREFIXES:
                if name.startswith(prefix) and len(name) > len(prefix):
                    out.append(name[len(prefix):])
                    break
            head, sep, tail = name.partition("_")
            if sep and len(tail) >= 4 and tail not in out:
                out.append(tail)
            return out

        by_schema: Dict[Optional[str], Dict[str, str]] = {}
        stems: Dict[Optional[str], Dict[str, str]] = {}
        for q, d in self._docs.items():
            name = d.name.lower()
            by_schema.setdefault(d.schema, {})[name] = q
            if not name.startswith(self.shadow_prefixes):
                for stem in stems_of(name):
                    stems.setdefault(d.schema, {}).setdefault(stem, q)

        shadows: Dict[str, str] = {}
        for q, d in self._docs.items():
            name = d.name.lower()
            for suffix in self.shadow_suffixes:
                if name.endswith(suffix) and len(name) > len(suffix):
                    base = by_schema[d.schema].get(name[: -len(suffix)])
                    if base and base != q:
                        shadows[q] = base
                        break
            if q in shadows:
                continue
            for prefix in self.shadow_prefixes:
                if name.startswith(prefix) and len(name) > len(prefix):
                    rest = name[len(prefix):]
                    base = stems.get(d.schema, {}).get(rest)
                    if base and base != q:
                        shadows[q] = base
                        break
        return shadows

    def shadows(self) -> Dict[str, str]:
        """Objects ranked below a same-named base object, and which base."""
        if self._stale or not self._order:
            self.index()
        return dict(self._shadows)

    def __len__(self) -> int:
        return len(self._docs)

    def objects(self) -> List[ObjectDoc]:
        """Every indexed object, in insertion order."""
        return list(self._docs.values())

    @property
    def _ns(self) -> str:
        return f"catalog:{self.name}"

    # ---------------- select ----------------

    def _visible(self, doc: ObjectDoc, principal: Optional[Principal]) -> bool:
        return allowed(doc.roles, principal)

    def select(self, question: str, top_k: int = 6,
               principal: Optional[Principal] = None,
               expand_fks: bool = True, pin: Optional[Sequence[str]] = None,
               vector_weight: float = 1.0, lexical_weight: float = 1.0,
               reranker=None, rerank_candidates: int = 20) -> Selection:
        """``reranker`` is any provider with ``.complete(system, prompt)``.

        Given one, the maths still runs first and still decides which objects
        are even eligible -- it just narrows the field to ``rerank_candidates``
        and lets the model order those. That ordering is where identifier
        matching is weakest: measured recall@6 is 100% when a question uses
        schema words and 50% when it uses business words, and no weighting
        fixes that, because "doctors" and `provider` share no characters.

        The model never sees an object this principal cannot, because it is
        handed the already-filtered list. And a model that fails leaves the
        maths order untouched, so this can only help.
        """
        if self._stale or not self._order:
            self.index()

        allowed = [q for q in self._order if self._visible(self._docs[q], principal)]
        allowed_set = set(allowed)

        qvec = self.embedder.embed([question])[0]
        vec_hits = self.store.search(self._ns, qvec, k=len(self._order) or 1,
                                     max_distance=2.0)
        vec_rank = {h["qname"]: i for i, h in enumerate(
            [h for h in vec_hits if h["qname"] in allowed_set])}

        def _rank(index: Optional[_BM25]) -> Dict[str, int]:
            if index is None:
                return {}
            scores = index.scores(question)
            pairs = sorted(
                ((self._order[i], s) for i, s in enumerate(scores)
                 if self._order[i] in allowed_set and s > 0),
                key=lambda p: -p[1])
            return {q: i for i, (q, _) in enumerate(pairs)}

        lex_rank = _rank(self._bm25)
        q_tokens = tokenize(question)

        fused: Dict[str, float] = {}
        for q in allowed:
            s = 0.0
            if q in vec_rank:
                s += vector_weight / (_RRF_K + vec_rank[q] + 1)
            if q in lex_rank:
                s += lexical_weight / (_RRF_K + lex_rank[q] + 1)
            # A backup or staging copy carries the same name words in a
            # shorter document, and cosine similarity rewards exactly that.
            # Demote it -- only while the object it shadows is visible to
            # this caller, so scoping can never make a shadow vanish along
            # with its base.
            if (s and q in self._shadows and self._shadows[q] in allowed_set
                    and not _named_in(q_tokens, self._docs[q])):
                s *= SHADOW_PENALTY
            if s:
                fused[q] = s

        ranked = sorted(fused.items(), key=lambda p: -p[1])
        chosen: List[Scored] = []
        taken = set()

        for name in (pin or []):
            for q, d in self._docs.items():
                if (q == name or d.name == name) and q in allowed_set and q not in taken:
                    chosen.append(Scored(d, 1.0, "pinned"))
                    taken.add(q)

        # The model reorders what the maths shortlisted, and only that. It is
        # handed `ranked`, which is already filtered by principal, so it can
        # promote a table but never introduce one this caller may not see.
        if reranker is not None and ranked:
            from .rerank import rerank as _rerank
            shortlist = [self._docs[q] for q, _ in ranked if q not in taken]
            reordered = _rerank(reranker, question, shortlist, top_k=top_k,
                                candidates=rerank_candidates)
            score_of = dict(ranked)
            ranked = [(d.qname, score_of.get(d.qname, 0.0)) for d in reordered]

        for q, s in ranked:
            if len(chosen) >= top_k:
                break
            if q not in taken:
                reason = "hybrid" if (q in vec_rank and q in lex_rank) else (
                    "vector" if q in vec_rank else "lexical")
                chosen.append(Scored(self._docs[q], s, reason))
                taken.add(q)

        if expand_fks:
            for sc in list(chosen):
                for fk in sc.doc.foreign_keys:
                    for q in allowed:
                        d = self._docs[q]
                        if d.name.lower() == fk.ref_table.lower() and q not in taken:
                            chosen.append(Scored(d, 0.0, "fk"))
                            taken.add(q)

        return Selection(question=question, hits=chosen,
                         total_objects=len(self._order), principal=principal)
