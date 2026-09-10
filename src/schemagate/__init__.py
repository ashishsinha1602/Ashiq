"""schemagate -- identity-scoped schema selection for NL2SQL.

    from schemagate import Catalog, Principal
    cat = Catalog().bootstrap("postgresql://localhost/app")
    sel = cat.select("revenue by month", principal=Principal("okta:jdoe"))
    sel.prompt_fragment()
"""
from .identity import Principal, IdentityError
from .models import Column, ForeignKey, ObjectDoc, Selection, Scored
from .embedder import HashingEmbedder, cosine_distance, tokenize
from .stores.memory import MemoryStore
from .catalog import Catalog

__version__ = "0.1.13"
__all__ = ["Catalog", "Principal", "IdentityError", "ObjectDoc", "Column",
           "ForeignKey", "Selection", "Scored", "HashingEmbedder",
           "MemoryStore", "cosine_distance", "tokenize"]


def __getattr__(name):
    # Lazy, so importing schemagate never pulls in oracledb or
    # sentence-transformers. Both raise a clear ImportError naming the extra.
    if name == "OracleStore":
        from .stores.oracle import OracleStore
        return OracleStore
    if name == "SentenceTransformerEmbedder":
        from .embedders.hf import SentenceTransformerEmbedder
        return SentenceTransformerEmbedder
    raise AttributeError(f"module 'schemagate' has no attribute {name!r}")
