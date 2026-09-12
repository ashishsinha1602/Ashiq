"""Catalog: identity-scoped schema selection.

Retrieval is hybrid. Vector similarity alone under-performs badly on schema
text because identifiers are not sentences; BM25 alone misses paraphrase
("owe us money" -> balance). Ranks from both are fused with Reciprocal Rank
Fusion, then foreign-key expansion pulls in join tables the question never
names -- the single biggest cause of unrunnable generated SQL.
"""
from __future__ import annotations

import re
import math
from collections import Counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .embedder import (Embedder, HashingEmbedder, tokenize, expand_joins,
                       expand_acronyms)
from .identity import Principal
from .models import ObjectDoc, Scored, Selection, allowed, ForeignKey
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

#: How much the name field counts for in fusion, against 1.0 for the body and
#: 1.0 for vectors. Equal: a question that names an object should reach it on
#: the name alone, and a question that describes one should still reach it on
#: the description alone.
NAME_WEIGHT = 1.0

#: What it is worth for the question to spell an object's name out. Naming a
#: thing is the strongest evidence a question can carry -- stronger than any
#: similarity -- and rank fusion cannot express that on its own: RRF turns
#: every score into 1/(60+rank), so the object the user actually named
#: arrives a hair above the ones that merely share a word with it, and the
#: other two channels can out-vote it. "show the contacts of xmagnet" put
#: `contacts_audit` and `auto_warmup_contacts` ahead of `contacts`.
#:
#: Deliberately multiplicative on an already-fused score, so it orders the
#: named objects among themselves rather than flattening them, and so an
#: object that is named but otherwise irrelevant still cannot beat one that
#: is named AND matches.
NAMED_BOOST = 4.0

#: A question word has to be informative before its absence is worth a slot.
#: Measured against the name field, where the domain's own nouns stay rare:
#: "tenant" is in 30 names of 1,245, "the" is in none. Low enough to catch a
#: common-ish entity, high enough that filler never buys a table.
_COVERAGE_MIN_IDF = 2.0

#: At most this many added for coverage, so a long question cannot quietly
#: double the prompt it was meant to keep small.
_COVERAGE_MAX = 3


#: What a description is worth, against 1.0 for the name and 1.0 for the body.
#: Below them on purpose: a description is generated, it is the part most
#: likely to be wrong or generic, and it should be able to help a question
#: that uses no schema words without being able to overturn a question that
#: names an object outright.
#: Equal to the others. An earlier version starved this channel to keep bad
#: descriptions from doing damage; that was the wrong lever and it cost
#: "per member per month cost" -> v_pmpm. Isolation is what contains a weak
#: catalogue -- prose can only ever win the prose channel -- so the weight can
#: be honest about how useful a good description is.
PROSE_WEIGHT = 1.0


def _prose_text(doc) -> str:
    """Everything written *about* the object: hint, description, comments."""
    parts = [doc.hint or "", doc.description or ""]
    parts.extend(c.comment or "" for c in doc.columns)
    return " ".join(x for x in parts if x)


def _name_text(doc) -> str:
    """Just the identifiers: schema, name, and the name split on underscores."""
    return " ".join(x for x in (doc.schema or "", doc.name or "",
                                (doc.name or "").replace("_", " ")) if x)

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


#: Words that appear inside identifiers as glue, never as meaning.
_BOOST_STOP = frozenset("of by as at in on to for and or per the a an is are was".split())


def _stem(t: str) -> str:
    """Just enough to let a plural meet its singular. Not a stemmer."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("es") and not t.endswith("ses"):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


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
        # Stemmed, both sides. A question says "how many claims"; the object
        # says "one row per claim". Without this they are different words and
        # the match never happens -- which is not a quirk of one schema, it is
        # every plural anyone types: contacts/contact, orders/order,
        # users/user. Only the lexical layer stems; `tokenize` itself is left
        # alone because the embedder's vectors are a published, pinned
        # guarantee and changing them would break every stored index.
        self.docs = [[_stem(t) for t in tokenize(d)] for d in docs]
        self.len = [len(d) for d in self.docs]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        self.tf = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: str) -> List[float]:
        toks = tokenize(query)
        q = [_stem(t) for t in expand_joins(toks)]
        q.extend(t for t in expand_acronyms(toks, self.idf) if t not in q)
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
        # Read the joins the schema implies but never declared, before the
        # index is built -- they are part of what an object *is*.
        self.infer_foreign_keys()
        self.index()
        return self

    def infer_foreign_keys(self) -> int:
        """Add the joins the schema implies but never declared.

        Application schemas carry their relationships in column names and
        leave the constraints off -- migrations are faster without them, and
        ORMs do the joining. On a real 1,245-object schema: `contacts`
        declares `company_id -> companies` and `user_id -> users` but not
        `tenant_id`, though `tenants` is right there; across the schema 2,291
        columns name a table that exists and are not declared as keys.

        That matters because a question like "the contacts of xmagnet" needs
        `tenants` to resolve the name, and nothing put it in front of the
        model: FK expansion only follows declared edges, so the model was
        shown `contacts` alone and correctly said it could not answer.

        Conservative on purpose. An edge is added only when the column ends
        in `_id`, its stem names an object in the same schema (singular or
        plural), that object has a single-column primary key, and no declared
        constraint already covers the column. Everything added is marked
        `inferred` so `render_ddl` can label it.
        """
        # Every way a table might be named for the same thing. Conventions
        # are not universal and one schema often holds several: `tenants`,
        # `CRM_CUSTOMER`, `dim_member`, `tbl_order` all name the object a key
        # column points at. So index each object under its name, its name
        # without a leading domain or layer word, and the singular/plural of
        # both -- then a stem is looked up rather than guessed at.
        def variants(word: str):
            out = {word}
            if word.endswith("ies") and len(word) > 4:
                out.add(word[:-3] + "y")
            if word.endswith("es") and len(word) > 3:
                out.add(word[:-2])
            if word.endswith("s") and len(word) > 2:
                out.add(word[:-1])
            else:
                out.add(word + "s")
                if word.endswith("y") and len(word) > 2:
                    out.add(word[:-1] + "ies")
                else:
                    out.add(word + "es")
            return out

        by_key: Dict[tuple, set] = {}
        for d in self._docs.values():
            name = (d.name or "").lower()
            if not name:
                continue
            keys = set(variants(name))
            # `crm_customer` answers to `customer`, and so does `dim_customer`
            # -- which is exactly why an ambiguous stem is left alone below.
            if "_" in name:
                keys |= variants(name.rsplit("_", 1)[-1])
            for k in keys:
                by_key.setdefault((d.schema, k), set()).add(d.qname)

        def target(stem: str, schema) -> Optional[ObjectDoc]:
            hits = set()
            for v in variants(stem):
                hits |= by_key.get((schema, v), set())
            if len(hits) != 1:
                # Nothing, or more than one plausible table. A wrong join is
                # worse than a missing one: the model writes it confidently
                # and the rows come back quietly incorrect.
                return None
            doc = self._docs[next(iter(hits))]
            return doc if len([c for c in doc.columns if c.pk]) == 1 else None

        def stem_of(name: str):
            """The object a key column points at, however the shop spells it.

            Conventions are per-database, per-team, and often mixed inside
            one schema: PostgreSQL and MySQL write `customer_id`, Oracle
            commonly writes `ID_CUSTOMER`, warehouses write `member_key`.
            Reading one spelling infers nothing at all on half the databases
            this runs against.
            """
            for suffix in ("_id", "_key", "_fk"):
                if name.endswith(suffix) and len(name) > len(suffix):
                    return name[:-len(suffix)]
            for prefix in ("id_", "fk_"):
                if name.startswith(prefix) and len(name) > len(prefix):
                    return name[len(prefix):]
            return None

        added = 0
        for doc in self._docs.values():
            declared = {c.lower() for fk in doc.foreign_keys for c in fk.columns}
            for col in doc.columns:
                name = (col.name or "").lower()
                stem = stem_of(name)
                if stem is None or name in declared:
                    continue
                ref = target(stem, doc.schema)
                if ref is None or ref is doc:
                    continue
                pk = [c.name for c in ref.columns if c.pk]
                doc.foreign_keys.append(ForeignKey(
                    columns=[col.name], ref_table=ref.name,
                    ref_columns=pk, inferred=True))
                declared.add(name)
                added += 1
        if added:
            self._stale = True
        return added

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
        # The name, scored as its own field.
        #
        # One flat document per object is what made a catalogued schema worse
        # at the questions cataloguing is for. Every description repeats the
        # domain's words, so on a 1,245-object schema "contact" fell to idf
        # 0.15 -- the word the user typed became the least informative token
        # in the index -- while the table literally called `contacts` was
        # pushed down by length normalisation for carrying 55 columns, a
        # description and a row of alias words. Measured on that schema:
        # `contacts` ranked 3rd before cataloguing and below 40th after it.
        #
        # A field of its own fixes both halves. Its idf is computed over names
        # alone, where "contact" appears in 17 of 1,245 and is rare again; and
        # its length is the name's length, which no description can inflate.
        self._bm25_name = _BM25([_name_text(self._docs[q]) for q in self._order])
        # And the written text -- description, hint, comments -- in a third.
        #
        # Fields do not just protect the name from length; they contain a bad
        # catalogue. A weak model writes the same generic sentence about every
        # object ("Stores data about users and their settings"), and in one
        # flat document that is indistinguishable from signal: the words it
        # repeats become common everywhere, every object looks a bit like
        # every question, and retrieval gets worse the more of the schema you
        # describe. Kept apart, useless prose can only make the prose channel
        # useless. The name and the columns are untouched, so the floor is
        # "no better than before cataloguing" instead of "worse".
        self._bm25_prose = _BM25([_prose_text(self._docs[q]) for q in self._order])
        self._shadows = self._find_shadows()
        vecs = self.embedder.embed(texts)
        self.store.purge(self._ns)
        for qname, vec in zip(self._order, vecs):
            self.store.upsert(self._ns, qname, vec, {"qname": qname})
        self._stale = False
        return self

    #: The few suffixes that mean "a backup" with no room for doubt, with or
    #: without a date stamp, and with or without the original still in the
    #: schema. Deliberately NOT `_old`, `_copy`, `_archive`, `_prev`: a lone
    #: `account_old` is often a real table (the tests say so), and those keep
    #: needing a base. But `ecm_template_link_bak_20260722` is never the table
    #: anyone wants, and with its original gone it out-ranked the live tables
    #: on a 1,245-object schema because its columns matched the question just
    #: as well. Honours `shadow_suffixes=()` like the base rule does.
    _STANDALONE_SHADOW = re.compile(
        r"_(?:bak|bkp|backup)(?:_?\d{4}_?\d{2}_?\d{2}|_\d{6,8})?$", re.I)

    #: Table partitions: `contacts_p0217`, `events_2026_07`, `sales_y2025m03`.
    #: Postgres and Oracle both expose them as ordinary tables, and each one
    #: carries the parent's exact columns in a shorter document -- so on a
    #: partitioned schema the partitions out-rank the parent for every
    #: question that names it. Demoted only when the parent is present in
    #: the same schema, like every other suffix rule.
    _PARTITION = re.compile(
        r"_(?:p\d+|\d{4}(?:_?\d{2}){0,2}|y\d{4}(?:m\d{2})?(?:d\d{2})?)$", re.I)

    def _find_shadows(self) -> Dict[str, str]:
        found = self._find_shadows_by_base()
        if not self.shadow_suffixes:
            return found
        by_schema: Dict[tuple, str] = {}
        for q, d in self._docs.items():
            by_schema[(d.schema, (d.name or "").lower())] = q
        for q, d in self._docs.items():
            if q in found:
                continue
            m = self._PARTITION.search((d.name or "").lower())
            if not m:
                continue
            parent = by_schema.get((d.schema, (d.name or "").lower()[:m.start()]))
            if parent and parent != q and parent not in found:
                found[q] = parent
        for q, d in self._docs.items():
            if q in found:
                continue
            m = self._STANDALONE_SHADOW.search(d.name or "")
            if m:
                # No base to point at: the stem is recorded so shadows() still
                # says what it is a copy of, and the penalty below treats a
                # base that is not in the catalog as "always demote".
                found[q] = (d.name or "")[:m.start()] or d.name
        return found

    def _find_shadows_by_base(self) -> Dict[str, str]:
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

        # The joined forms confirmed against the index vocabulary go to the
        # vector side as well. Without this `myconvo` scored on BM25 alone and
        # rank fusion let a table that merely said "campaign" win.
        _vocab = self._bm25.idf if self._bm25 is not None else None
        _max_idf = max(self._bm25.idf.values()) if (self._bm25 is not None and self._bm25.idf) else 0.0
        _base = tokenize(question)
        _joins = [t for t in expand_joins(_base, vocab=_vocab) if t not in _base]
        qvec = self.embedder.embed([question + (" " + " ".join(_joins) if _joins else "")])[0]
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
        name_rank = _rank(self._bm25_name)
        prose_rank = _rank(self._bm25_prose)
        q_tokens = expand_joins(tokenize(question), vocab=_vocab)
        _q_stems = {_stem(t) for t in q_tokens if len(t) > 2 and t not in _BOOST_STOP}

        fused: Dict[str, float] = {}
        # Which objects the question actually names, keeping only the longest
        # match where one name is a token-prefix of another.
        _named = {q: tokenize(self._docs[q].name)
                  for q in allowed if _named_in(q_tokens, self._docs[q])}
        _named_best = {
            q for q, toks in _named.items()
            if not any(other is not toks and len(other) > len(toks)
                       and other[:len(toks)] == toks
                       for other in _named.values())}

        for q in allowed:
            s = 0.0
            if q in vec_rank:
                s += vector_weight / (_RRF_K + vec_rank[q] + 1)
            if q in lex_rank:
                s += lexical_weight / (_RRF_K + lex_rank[q] + 1)
            if q in name_rank:
                s += NAME_WEIGHT / (_RRF_K + name_rank[q] + 1)
            if q in prose_rank:
                s += PROSE_WEIGHT / (_RRF_K + prose_rank[q] + 1)
            # A backup or staging copy carries the same name words in a
            # shorter document, and cosine similarity rewards exactly that.
            # Demote it -- only while the object it shadows is visible to
            # this caller, so scoping can never make a shadow vanish along
            # with its base.
            if (s and q in self._shadows
                    and (self._shadows[q] in allowed_set
                         or self._shadows[q] not in self._docs)
                    and not _named_in(q_tokens, self._docs[q])):
                s *= SHADOW_PENALTY
            # A rare question word that is *in the object's name* is the
            # strongest signal there is, and rank fusion under-weights it:
            # `myconvo` appears in 5 names out of 1,245, matched the question
            # exactly, and the table still sat seventh behind six that merely
            # said "campaign". Weighted by IDF so a word in every other name
            # earns nothing, capped so it reorders the neighbourhood rather
            # than the page, and a little more when the whole name is the
            # word -- "users" should find `users` before `ai_reporter_users`.
            # (An IDF boost for question words appearing in a name used to sit
            # here. It was patching this same dilution from the outside, and
            # it looked up the idf of the *stem* -- so "contacts" was scored
            # as the flooded "contact" and the boost collapsed to nothing.
            # The name field does the job properly.)
            # The user typed this object's name. Nothing else in the query is
            # as reliable -- but only the most specific match counts. Asking
            # for `fact_claim_line_v2` also spells out `fact_claim_line`, and
            # boosting both handed it to the shorter one, which is the table
            # the question went out of its way not to ask for.
            if s and q in _named_best:
                s *= NAMED_BOOST
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

        # Cover every thing the question named, not just the best-scoring ones.
        #
        # A question that needs a join names two things -- "campaigns … and
        # the tenant name" -- and ranking answers only the first. Every slot
        # went to a campaign table, `tenants` was absent at top_k=30, and the
        # model correctly reported that the tables it was shown could not
        # answer the question. More top_k does not help: it deepens the
        # concept that was already winning.
        #
        # So after ranking, each informative word the question used that no
        # chosen object carries in its name gets the best object that does.
        # Bounded, and only ever additive -- it cannot displace a ranked pick.
        uncovered = []
        if self._bm25_name is not None and self._bm25_name.idf:
            covered = set()
            for sc in chosen:
                covered.update(tokenize(_name_text(sc.doc)))
            # In question order, and de-duplicated by hand. Building this
            # from a set made the order arbitrary, so which token got the
            # last coverage slot could change between runs -- and did differ
            # from the JS twin on one case in 1,789.
            covered_stems = {_stem(c) for c in covered}
            uncovered = []
            for t in q_tokens:
                if (len(t) > 2 and t not in _BOOST_STOP
                        and self._bm25_name.idf.get(t, 0.0) >= _COVERAGE_MIN_IDF
                        and t not in covered and _stem(t) not in covered_stems
                        and t not in uncovered):
                    uncovered.append(t)
        for token in uncovered[:_COVERAGE_MAX]:
            best = None
            for q, sc_val in ranked:
                if q in taken:
                    continue
                if token in tokenize(_name_text(self._docs[q])):
                    best = (q, sc_val)
                    break                      # `ranked` is already sorted
            if best is None:
                continue
            # Inside the budget, not beyond it: drop the weakest ranked pick
            # rather than grow the answer. A caller that asked for six objects
            # gets six, and the prompt it was sizing stays the size it sized.
            if len(chosen) >= top_k:
                for i in range(len(chosen) - 1, -1, -1):
                    if chosen[i].reason not in ("pinned", "covers"):
                        taken.discard(chosen[i].doc.qname)
                        del chosen[i]
                        break
                else:
                    break                      # nothing droppable; leave it be
            chosen.append(Scored(self._docs[best[0]], best[1], "covers"))
            taken.add(best[0])

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
