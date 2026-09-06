"""LangChain retriever over an ashiq catalog.

Drop-in for any LangChain chain that takes a retriever. Each selected object
comes back as one ``Document`` whose ``page_content`` is its DDL and whose
metadata carries the name, kind, score and selection reason.

    from ashiq import Catalog, Principal
    from ashiq.integrations.langchain import AshiqRetriever

    cat = Catalog().bootstrap("postgresql://localhost/app")
    retriever = AshiqRetriever(catalog=cat, top_k=6,
                               principal=Principal("okta:jdoe", roles={"finance"}))
    docs = retriever.invoke("revenue by month")

The principal is fixed at construction on purpose. A retriever is usually
built once per request, and binding identity to it means a chain cannot
forget to pass it -- the same fail-closed stance as everywhere else in ashiq.
Build a new retriever per caller; they are cheap.

``pip install 'ashiq[langchain]'``.
"""
from __future__ import annotations

from typing import Any, List, Optional

try:
    from langchain_core.callbacks import CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
except ImportError as e:  # pragma: no cover - import guard
    raise ImportError(
        "pip install 'ashiq[langchain]' to use AshiqRetriever") from e

from ..catalog import Catalog
from ..identity import Principal


class AshiqRetriever(BaseRetriever):
    """Identity-scoped schema retriever for LangChain."""

    catalog: Catalog
    principal: Optional[Principal] = None
    top_k: int = 6
    expand_fks: bool = True
    max_columns: int = 40

    model_config = {"arbitrary_types_allowed": True}

    def _get_relevant_documents(
        self, query: str, *, run_manager: Optional[CallbackManagerForRetrieverRun] = None
    ) -> List[Document]:
        sel = self.catalog.select(query, top_k=self.top_k,
                                  principal=self.principal,
                                  expand_fks=self.expand_fks)
        return [
            Document(
                page_content=hit.doc.render_ddl(self.max_columns),
                metadata={
                    "name": hit.doc.qname,
                    "kind": hit.doc.kind,
                    "score": hit.score,
                    "reason": hit.reason,
                    "columns": len(hit.doc.columns),
                },
            )
            for hit in sel.hits
        ]

    def with_principal(self, principal: Optional[Principal]) -> "AshiqRetriever":
        """A copy bound to a different caller. Catalog and settings shared."""
        return self.model_copy(update={"principal": principal})


def prompt_fragment(documents: List[Document]) -> str:
    """Join retrieved documents back into one DDL block for a prompt."""
    return "\n\n".join(d.page_content for d in documents)


__all__: List[Any] = ["AshiqRetriever", "prompt_fragment"]
