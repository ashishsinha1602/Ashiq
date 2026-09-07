"""Embeddings from a hosted model.

The offline ``HashingEmbedder`` matches subwords, which is strong on
identifier-shaped questions and weak on pure paraphrase. An API embedder
trades money, latency and a network dependency for semantic matching.

Measure before adopting: on identifier-heavy schema text the offline
embedder is often competitive, and ``tests/bench.py`` runs against your
own schema. This is a swap, not an upgrade.

    from schemagate import Catalog
    from schemagate.ai import APIEmbedder, OpenAIProvider

    provider = OpenAIProvider(model="gpt-4.1-mini",
                              embed_model="text-embedding-3-small")
    cat = Catalog(embedder=APIEmbedder(provider, dim=1536))

Vectors are cached in memory for the life of the embedder and, if you pass
``cache_path``, on disk -- so re-indexing an unchanged schema is free.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
from typing import Dict, List, Optional, Sequence

from .providers import Provider, ProviderError


def _l2(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


class APIEmbedder:
    """Embedder backed by a provider's embedding endpoint.

    Parameters
    ----------
    provider
        Any object with ``embed(texts) -> list[list[float]]``.
    dim
        Expected dimension. Checked on the first response, because a silent
        dimension change invalidates every persisted vector.
    batch_size
        Texts per request.
    cache_path
        Optional JSON cache keyed by SHA-256 of the text plus provider name.
    """

    def __init__(self, provider: Provider, dim: int, batch_size: int = 64,
                 cache_path: Optional[str] = None, normalize: bool = True):
        if not hasattr(provider, "embed"):
            raise TypeError(
                f"{getattr(provider, 'name', provider)!r} has no embed(); "
                "pass a provider built with an embed model")
        self.provider = provider
        self.dim = int(dim)
        self.batch_size = max(1, int(batch_size))
        self.normalize = normalize
        self.name = f"api:{getattr(provider, 'name', 'provider')}"
        self.cache_path = pathlib.Path(cache_path) if cache_path else None
        self._cache: Dict[str, List[float]] = self._load()

    # ---------------- cache ----------------

    def _key(self, text: str) -> str:
        seed = f"{self.name}:{self.dim}:{text}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    def _load(self) -> Dict[str, List[float]]:
        if not self.cache_path or not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    # ---------------- embedding ----------------

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        texts = list(texts)
        out: List[Optional[List[float]]] = [None] * len(texts)
        todo: List[int] = []
        for i, text in enumerate(texts):
            hit = self._cache.get(self._key(text))
            if hit is not None:
                out[i] = list(hit)
            else:
                todo.append(i)

        for start in range(0, len(todo), self.batch_size):
            chunk = todo[start:start + self.batch_size]
            vectors = self.provider.embed([texts[i] for i in chunk])
            if len(vectors) != len(chunk):
                raise ProviderError(
                    f"{self.name} returned {len(vectors)} vectors for "
                    f"{len(chunk)} inputs")
            for i, vector in zip(chunk, vectors):
                vector = [float(v) for v in vector]
                if len(vector) != self.dim:
                    raise ProviderError(
                        f"{self.name} returned {len(vector)}-dim vectors but "
                        f"dim={self.dim} was declared; a changed embedding "
                        "model invalidates every persisted vector, so this "
                        "is refused rather than mixed")
                if self.normalize:
                    vector = _l2(vector)
                self._cache[self._key(texts[i])] = vector
                out[i] = vector

        if todo:
            self._save()
        return [v for v in out if v is not None]
