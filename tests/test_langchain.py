"""LangChain adapter, exercised against the real langchain-core package."""
import pytest

langchain_core = pytest.importorskip("langchain_core")

from schemagate import Principal  # noqa: E402
from schemagate.integrations.langchain import SchemagateRetriever, prompt_fragment  # noqa: E402


@pytest.fixture
def retriever(cat):
    cat.restrict("hr_compensation", ["payroll"])
    return SchemagateRetriever(catalog=cat, top_k=4)


def test_is_a_real_langchain_retriever(retriever):
    from langchain_core.retrievers import BaseRetriever
    assert isinstance(retriever, BaseRetriever)


def test_invoke_returns_documents_with_ddl_and_metadata(retriever):
    docs = retriever.invoke("revenue by month")
    assert docs
    names = {d.metadata["name"] for d in docs}
    assert "main.v_monthly_revenue" in names
    for d in docs:
        assert d.page_content.startswith(("TABLE", "VIEW", "--"))
        assert {"name", "kind", "score", "reason", "columns"} <= set(d.metadata)


def test_top_k_is_honoured_before_expansion(cat):
    r = SchemagateRetriever(catalog=cat, top_k=2, expand_fks=False)
    assert len(r.invoke("revenue")) <= 2


def test_anonymous_retriever_fails_closed(retriever):
    names = {d.metadata["name"] for d in retriever.invoke("salary by employee")}
    assert "main.hr_compensation" not in names


def test_bound_principal_sees_what_it_may(retriever):
    hr = retriever.with_principal(Principal("okta:hr", roles={"payroll"}))
    names = {d.metadata["name"] for d in hr.invoke("salary by employee")}
    assert "main.hr_compensation" in names
    # the original is untouched
    assert retriever.principal is None


def test_with_principal_shares_the_catalog(retriever):
    other = retriever.with_principal(Principal("okta:x"))
    assert other.catalog is retriever.catalog


def test_prompt_fragment_round_trips(retriever):
    docs = retriever.invoke("stock per warehouse")
    fragment = prompt_fragment(docs)
    assert "inv_stock_level" in fragment
    assert fragment.count("(") == fragment.count(")")


def test_works_inside_a_runnable_chain(retriever):
    """The reason to be a BaseRetriever at all: composability."""
    from langchain_core.runnables import RunnableLambda

    chain = retriever | RunnableLambda(prompt_fragment)
    out = chain.invoke("late shipments by carrier")
    assert isinstance(out, str) and "ship_shipment" in out


def test_batch_works(retriever):
    results = retriever.batch(["revenue by month", "headcount per department"])
    assert len(results) == 2
    assert all(isinstance(r, list) for r in results)
