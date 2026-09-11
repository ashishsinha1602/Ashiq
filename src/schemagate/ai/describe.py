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
import re
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
    "Then write ' | ' and 12 to 16 everyday words or short phrases a "
    "non-technical person might use when asking about this data: informal "
    "names for the people involved (shoppers, buyers, staff, borrowers), "
    "the places (depot, branch, site, shop), the things and the actions "
    "(buy from, give back, running out, behind on, came back). Plain "
    "synonyms and colloquial terms, never column names. For a low-battery "
    "view: flat, dying, dead, charge, power, running out, units, boxes. "
    "No preamble, no markdown, no quotes -- the sentence, a pipe, the words."
)

#: Two worked examples, appended to the system prompt.
#:
#: A frontier model follows the paragraph above. A 1.5B instruct model running
#: on someone's laptop follows *examples* -- told the rules in prose it will
#: reliably produce "Sure! Here is a description:" followed by a bulleted list,
#: and drop the everyday words entirely, which is the half that does the work
#: for business-phrased questions. Showing the shape twice costs ~90 prompt
#: tokens and is the single largest accuracy win available to a local model.
_FEWSHOT = """
Two examples of exactly the required output:

table SALES.CUST_ORD_LN_T
  ID NUMBER PK
  ORD_ID NUMBER
  SKU VARCHAR2
  QTY NUMBER
  AMT NUMBER
  FK ORD_ID -> SALES.CUST_ORD_T
->
One row per item on a customer order, with quantity and line amount. | what people bought, basket, cart, items ordered, order lines, shopping, purchases, how many, line total, spend, per item, order detail

view OPS.V_BAT_LOW
  ASSET_ID VARCHAR2
  PCT NUMBER
  SEEN_AT DATE
->
Assets whose battery charge has fallen below the alert threshold, with when it was last seen. | flat, dying, dead battery, needs charging, running out of power, low power, going flat, units, boxes, devices, out of juice, nearly empty
"""

SYSTEM_PROMPT = _SYSTEM + "\n" + _FEWSHOT

#: Sent only when the first reply came back unusable, alongside the original.
#: Small models correct format far more reliably when told what was wrong than
#: when simply asked again at a higher temperature.
_REPAIR = (
    "That reply was not in the required format. Reply again with ONE sentence "
    "of at most 25 words, then ' | ', then 12 to 16 everyday words separated "
    "by commas. No preamble, no markdown, no bullet points, no quotes, and do "
    "not repeat the object name."
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


#: "Sure!", "Here is the description:", "Output:" -- the things a small model
#: says before it says the answer.
#: The separator class includes `!` and `.` so that "Sure! Here is the
#: description: ..." is peeled in two passes -- the interjection ends in `!`,
#: not a colon, and matching only `:` left it in place.
_PREAMBLE = re.compile(
    r"^\s*(?:sure|certainly|of course|okay|ok|absolutely|here(?:'s| is| are)"
    r"[^:\n]*|the\s+description|description|answer|output|response|result)"
    r"\s*[:\-–—!.]\s*", re.I)
#: Underscore is NOT in here. It is markdown emphasis in prose and a word
#: character in every identifier this tool handles, and stripping it turned
#: `cust_ord_ln_t` into `custordlnt` -- which then defeated the name-echo
#: removal below and corrupted the sentence it was meant to clean.
_MARKDOWN = re.compile(r"[*`#]+")
_BULLET = re.compile(r"^\s*[-*•\d.)]+\s+")


def _identifier_words(doc: ObjectDoc) -> set:
    """Every token that appears in the object's own identifiers.

    The everyday-words half exists to carry vocabulary the schema does NOT
    already contain -- that is the entire reason it improves retrieval on
    business-phrased questions. A model that fills it with `qty, amt, cust_id`
    has added nothing, because those tokens are already indexed from the
    column names, and has crowded out the words that would have helped.
    """
    words = set()
    for raw in [doc.qname, doc.name] + [c.name for c in doc.columns]:
        for part in re.split(r"[^A-Za-z]+", str(raw or "")):
            if len(part) > 2:
                words.add(part.lower())
    return words


def clean_description(text: str, doc: Optional[ObjectDoc] = None) -> str:
    """Repair a model reply into the stored `sentence | words` form.

    Everything here is a failure seen from a small local model, in order of
    how often it happens. A frontier provider trips almost none of it, so this
    is close to a no-op on Anthropic or OpenAI output -- it exists so that
    running the catalog locally produces the same *shape* of text, and
    therefore the same retrieval behaviour, rather than a quietly worse
    catalog that still looks like a catalog.
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    for _ in range(3):                       # "Sure! Here is the answer: ..."
        new = _PREAMBLE.sub("", text, count=1)
        if new == text:
            break
        text = new.strip()
    text = _BULLET.sub("", text)
    text = _MARKDOWN.sub("", text).strip().strip('"').strip("'").strip()

    head, _, tail = text.partition(ALIAS_SEP)
    if not tail and "|" in text:             # a bare pipe, no spaces around it
        head, _, tail = text.partition("|")

    # Deliberately NOT cut down to one sentence. The prompt asks for one, but
    # a reply of two has always been kept and stored whole, and truncating it
    # here would silently rewrite descriptions people already have cached.
    # Rambling is bounded by the word cap below instead.
    head = _PREAMBLE.sub("", head.strip(), count=1).strip().strip('"').strip()
    if doc is not None:
        # "CUST_ORD_LN_T holds ..." / "The sales.cust_ord_ln_t table stores ..."
        for name in sorted({doc.qname, doc.name}, key=len, reverse=True):
            # Short names are not worth matching: a table called `t` or `id`
            # would strip a legitimate opening word out of the sentence.
            if not name or len(str(name)) < 3:
                continue
            # Also eat the linking verb the name was the subject of, or
            # removing "sales.cust_ord_ln_t " from "... holds one row per
            # order line" leaves a sentence starting "Holds".
            head = re.sub(
                r"^(?:the\s+)?%s(?:\s+(?:table|view))?\s+"
                r"(?:holds|stores|contains|tracks|records|lists|represents|"
                r"captures|is|has)?\s*" % re.escape(str(name)),
                "", head, count=1, flags=re.I)
    words = head.split()
    if len(words) > 60:
        head = " ".join(words[:60]).rstrip(",;:") + "."
    # Do NOT "tidy" a missing full stop onto the end. Terminal punctuation is
    # the signal `looks_truncated` reads to tell a finished sentence from one
    # the provider cut off mid-clause, and adding it here makes every
    # truncated reply look complete -- silently restoring the OCI bug where
    # descriptions arrived at ten tokens, recall dropped twenty points, and
    # nothing anywhere reported a problem.
    head = head[:1].upper() + head[1:] if head else head

    banned = _identifier_words(doc) if doc is not None else set()
    aliases, seen = [], set()
    for chunk in re.split(r"[,;/]| \| ", tail):
        w = _MARKDOWN.sub("", chunk).strip().strip('"').strip("'").lower()
        w = " ".join(w.split())
        if not w or len(w) > 40 or len(w.split()) > 4:
            continue
        if w in seen:
            continue
        # Compare on the same tokenisation the banned set was built from, or
        # `ord_id` slips through a set that holds `ord` and `id` separately.
        # An entry made only of identifier tokens -- `qty`, `ord_id`,
        # `cust id` -- adds nothing the column names did not already index.
        toks = [p for p in re.split(r"[^A-Za-z]+", w) if len(p) > 2]
        if not toks or all(p in banned for p in toks):
            continue
        seen.add(w)
        aliases.append(w)
        if len(aliases) >= 16:
            break
    return head + (ALIAS_SEP + ", ".join(aliases) if aliases else "")


def is_usable(text: str, doc: Optional[ObjectDoc] = None,
              require_aliases: bool = False) -> bool:
    """Worth storing, or worth one more call?

    The everyday-words half is not part of this judgement by default, and that
    is a cost decision rather than a quality one: plenty of models return a
    good sentence and no aliases, every retry is a second billed call, and
    retrying all of them would roughly double the price of cataloguing a
    schema to chase a second-order retrieval gain.

    `require_aliases` flips that for a provider where calls are free -- a
    local model -- and even then asks only that the format was attempted at
    all, not that it produced a full dozen words.
    """
    head, tail = split_description(text)
    # looks_truncated() must see the WHOLE reply, not the sentence alone. It
    # treats a pipe as proof the model reached the everyday-words section and
    # therefore was not cut off mid-clause; hand it the head with the pipe
    # already split away and that evidence is gone, so every sound sentence
    # shorter than twelve words and not ending in a full stop is read as
    # truncated. That warned on 34 of 39 objects and bought each of them a
    # pointless second call.
    #
    # There is deliberately no minimum word count. A very short description
    # ("Table.") is a poor one, but it is not a *cut* one, and a provider that
    # returns it will return it again -- so treating brevity as a retry
    # condition spends a second call to get the same string, and reports it
    # under a warning telling the user to check their output token limit,
    # which is not the problem. Shortness is judged by the reader; truncation
    # is judged here.
    if looks_truncated(text):
        return False
    if doc is not None and head.rstrip(".").strip().lower() in {
            str(doc.qname or "").lower(), str(doc.name or "").lower()}:
        return False
    if require_aliases:
        return bool([a for a in tail.split(",") if a.strip()])
    return True


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


def looks_truncated(text: str) -> bool:
    """Did the provider cut the reply off mid-sentence?

    A description that stops mid-clause is still a valid-looking string, so a
    provider that truncates degrades retrieval silently -- which is exactly
    what happened on a live OCI Generative AI run, where every description
    arrived at about ten tokens and recall dropped twenty points with no error
    anywhere. Cheap structural check: the model was asked for a sentence, so a
    reply with no terminal punctuation that is also suspiciously short is
    almost certainly cut.
    """
    text = (text or "").strip()
    if not text:
        return True
    if text.endswith((".", "!", "?")) or ALIAS_SEP.strip() in text:
        return False
    return len(text.split()) < 12


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
                 max_tokens: int = 220, strict: bool = False,
                 strict_format: Optional[bool] = None):
        self.provider = provider
        #: Hold out for the everyday-words half, and spend a retry to get it.
        #: Default: on for a local model, off for anything billed. A local
        #: call costs a second of laptop time, so it is worth re-asking a 1.5B
        #: model that ignored the format; the same retry against a paid API is
        #: real money for a description that was already serviceable.
        if strict_format is None:
            strict_format = str(getattr(provider, "name", "")).startswith("local:")
        self.strict_format = bool(strict_format)
        self.cache_path = pathlib.Path(cache_path) if cache_path else None
        self.max_columns = max_columns
        self.workers = max(1, int(workers))
        self.max_tokens = max_tokens
        self.strict = strict
        self.failures: List[str] = []
        #: objects whose reply came back cut off even after a retry
        self.truncated: List[str] = []
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
        rendered = _render(doc, self.max_columns)
        try:
            raw = self.provider.complete(SYSTEM_PROMPT, rendered,
                                         max_tokens=self.max_tokens)
        except ProviderError:
            self.failures.append(doc.qname)
            if self.strict:
                raise
            return None
        text = clean_description(raw, doc)
        if not text:
            self.failures.append(doc.qname)
            return None

        # One retry, and only one. Two failure modes reach here and they want
        # the same call: a reply cut off by the provider's token cap, and a
        # reply that ignored the format. Both are recoverable and both are
        # invisible without this -- a malformed description is still a
        # non-empty string, so the catalog looks complete while retrieval
        # quietly gets worse. That is exactly how the OCI truncation bug hid.
        if not is_usable(text, doc, require_aliases=self.strict_format):
            try:
                retry = self.provider.complete(
                    SYSTEM_PROMPT,
                    rendered + "\n\nYour previous reply was:\n" +
                    " ".join((raw or "").split())[:400] + "\n\n" + _REPAIR,
                    max_tokens=max(self.max_tokens, 512))
            except ProviderError:
                retry = ""
            better = clean_description(retry, doc)
            if better and is_usable(better, doc):
                text = better
            elif (better and not is_usable(text, doc, require_aliases=self.strict_format)
                  and len(better) > len(text)):
                text = better
                self.truncated.append(doc.qname)
            else:
                self.truncated.append(doc.qname)
        self._cache[key] = text
        return text

    def describe(self, docs: Iterable[ObjectDoc]) -> Dict[str, str]:
        """Return ``{qname: description}``. Objects that failed are absent."""
        docs = list(docs)
        self.failures = []
        self.truncated = []
        if not docs:
            return {}
        if self.workers == 1:
            results = [self._describe_one(d) for d in docs]
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(self._describe_one, docs))
        self._save_cache()
        if self.truncated:
            import warnings
            warnings.warn(
                f"{getattr(self.provider, 'name', 'provider')} returned a truncated "
                f"description for {len(self.truncated)} of {len(docs)} objects "
                f"(e.g. {', '.join(self.truncated[:3])}). Retrieval will be worse "
                f"than it should be; check the provider's output token limit.",
                RuntimeWarning, stacklevel=2)
        return {d.qname: t for d, t in zip(docs, results) if t}

    # what the provider would receive, for review before spending money
    def preview(self, doc: ObjectDoc) -> str:
        return _render(doc, self.max_columns)

    def estimate_calls(self, docs: Sequence[ObjectDoc]) -> int:
        """How many billed calls ``describe()`` would make right now."""
        name = getattr(self.provider, "name", "?")
        return sum(1 for d in docs if _fingerprint(d, name) not in self._cache)
