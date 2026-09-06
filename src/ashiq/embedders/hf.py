"""Sentence-transformers embedder.

Not exercised in CI (no model download in the sandbox). Install with
``pip install ashiq[huggingface]``. Note that on identifier-heavy schema
text a sentence model does not automatically beat the built-in
HashingEmbedder -- benchmark on your own schema with tests/bench.py before
taking the dependency.
"""
from __future__ import annotations
from typing import List, Sequence


class SentenceTransformerEmbedder:
    def __init__(self, model: str = "all-MiniLM-L6-v2", device: str | None = None,
                 batch_size: int = 64, cache_folder: str | None = None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "pip install 'ashiq[huggingface]' to use SentenceTransformerEmbedder"
            ) from e
        self._m = SentenceTransformer(model, device=device, cache_folder=cache_folder)
        self.dim = int(self._m.get_sentence_embedding_dimension())
        self.batch_size = batch_size
        self.name = f"st:{model}"

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        v = self._m.encode(list(texts), batch_size=self.batch_size,
                           normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, row)) for row in v]
