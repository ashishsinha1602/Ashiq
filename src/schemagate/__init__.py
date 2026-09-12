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

#: Read from the installed distribution rather than written here. Hand-kept
#: it fell four releases behind without anything noticing: `pip show` said
#: 0.1.37 while `schemagate.__version__` said 0.1.33, and a bug report quoting
#: the second sends you looking at the wrong code. The fallback is for a
#: source tree that was never installed.
try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version
    try:
        __version__ = _pkg_version("schemagate")
    except PackageNotFoundError:                     # running from a checkout
        __version__ = "0.0.0.dev0"
except ImportError:                                  # pragma: no cover
    __version__ = "0.0.0.dev0"
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
