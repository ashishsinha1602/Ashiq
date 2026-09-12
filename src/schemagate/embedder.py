"""Embedder protocol and the zero-dependency default.

HashingEmbedder is not a placeholder. Schema selection matches questions
against identifier-heavy text (CUST_ORDER_LINE_ITEMS_V, ID_CUSTOMER),
where subword overlap carries more signal than sentence semantics. It needs
no model download, no API key, no fitting, and is byte-for-byte deterministic
across machines -- which also makes cached vectors portable.

Swap in SentenceTransformerEmbedder, or any object satisfying the Embedder
protocol, if your questions share little vocabulary with your schema.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import List, Protocol, Sequence, runtime_checkable

#: Unicode-aware. ``[a-z0-9]+`` silently shredded any non-English schema:
#: ``facturación`` became ['facturaci', 'n'] and ``売上明細`` became nothing
#: at all, making those objects unreachable by name. ``_`` is excluded so
#: snake_case still splits.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Scripts written without spaces, where one "word" is the whole run and
#: character n-grams are the only sensible unit.
_CJK_RANGES = (
    (0x3040, 0x30FF),    # hiragana, katakana
    (0x3400, 0x4DBF),    # CJK extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xAC00, 0xD7AF),    # hangul syllables
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
)


@runtime_checkable
class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> List[List[float]]: ...


def _l2(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """0.0 == identical, 1.0 == orthogonal, 2.0 == opposite. Assumes L2-normalised."""
    return 1.0 - sum(x * y for x, y in zip(a, b))


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return any(lo <= point <= hi for lo, hi in _CJK_RANGES)


def _fold(token: str) -> str:
    """Strip accents so a query typed without them still matches.

    People type ``facturacion`` for a column called ``facturación``, and
    ``ano`` for ``año``. Folding at index and query time makes both work.
    Pure-ASCII tokens are returned unchanged, so English behaviour is
    byte-identical to before folding existed.
    """
    if token.isascii():
        return token
    # NFKD does not decompose these; German and Nordic schemas are common
    # enough that folding them by hand is worth the four lines.
    for char, replacement in (("ß", "ss"), ("ø", "o"), ("æ", "ae"),
                              ("đ", "d"), ("ł", "l")):
        token = token.replace(char, replacement)
    return "".join(c for c in unicodedata.normalize("NFKD", token)
                   if not unicodedata.combining(c))


def expand_joins(tokens: List[str], vocab=None, max_len: int = 14) -> List[str]:
    """Add the joined form of each adjacent pair of short words.

    People write product names as they say them -- "my convo", "sign up",
    "e mail" -- and identifiers write them as one token: `myconvo`,
    `signup`, `email`. Neither side is wrong, and no description fixes it,
    because the description is written by a model that never heard the
    product name. So the *question* also carries `myconvo`, `signup`,
    `email`. Query side only: documents are left exactly as they were, so
    the index does not grow and nothing already matching changes rank.
    """
    out = list(tokens)
    for a, b in zip(tokens, tokens[1:]):
        if not (a.isalpha() and b.isalpha() and len(a) + len(b) <= max_len):
            continue
        joined = a + b
        # With a vocabulary, only joins the schema actually contains are
        # kept: `myconvo` stays because an identifier says it, `whichmy` goes.
        # That is what lets the joined form be fed to the vector side too
        # without stuffing the query with noise.
        if vocab is None or joined in vocab:
            out.append(joined)
    return out


def tokenize(text: str) -> List[str]:
    """Split identifiers into comparable tokens.

    Handles snake_case, camelCase, digits, accented Latin scripts, and
    scripts with no word boundaries (Chinese, Japanese, Korean), which are
    emitted as character unigrams and bigrams because there is nothing else
    to split on.
    """
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    out: List[str] = []
    for token in _WORD.findall(text.lower()):
        if any(_is_cjk(c) for c in token):
            out.extend(token)                                  # unigrams
            out.extend(token[i:i + 2] for i in range(len(token) - 1))
            continue
        folded = _fold(token)
        if folded:
            out.append(folded)
    return out


class HashingEmbedder:
    """Hashed bag of word tokens + character n-grams, L2 normalised."""

    def __init__(self, dim: int = 512, ngram_range: tuple = (3, 5),
                 word_weight: float = 2.0):
        if dim < 32:
            raise ValueError("dim must be >= 32")
        self.dim = dim
        self.ngram_range = ngram_range
        self.word_weight = word_weight
        self.name = f"hashing-{dim}-{ngram_range[0]}{ngram_range[1]}"

    def _bucket_and_sign(self, feature: str) -> tuple:
        """Bucket index and sign, both derived from one blake2b digest.

        The sign must NOT come from Python's builtin ``hash()``: string
        hashing is salted per interpreter process (PYTHONHASHSEED), so a
        vector written by one process would not match a query embedded by
        the next -- a persisted index would silently degrade to noise,
        which is exactly the class of failure this library exists to stop.
        blake2b is stable across processes, machines and Python versions.
        """
        h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        n = int.from_bytes(h, "big")
        # low bits choose the bucket, the top bit chooses the sign
        return n % self.dim, (1.0 if (n >> 63) & 1 else -1.0)

    def _features(self, text: str):
        toks = tokenize(text)
        for t in toks:
            yield f"w:{t}", self.word_weight
        lo, hi = self.ngram_range
        for t in toks:
            padded = f"^{t}$"
            for n in range(lo, hi + 1):
                for i in range(len(padded) - n + 1):
                    yield f"c:{padded[i:i + n]}", 1.0

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            counts: dict = {}
            for feat, w in self._features(text or ""):
                counts[feat] = counts.get(feat, 0.0) + w
            for feat, c in counts.items():
                # sublinear scaling stops long DDL from swamping short questions
                bucket, sign = self._bucket_and_sign(feat)
                vec[bucket] += (1.0 + math.log(c)) * sign
            out.append(_l2(vec))
        return out
