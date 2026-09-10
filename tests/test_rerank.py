"""Letting a model order the shortlist.

Matching on identifiers has a measured ceiling: recall@6 is 100% across five
test schemas and 50% on the one where questions are phrased in business words
rather than table words. No weighting fixes that -- "doctors" and `provider`
share no characters. A model does fix it, and only needs to see twenty
one-line summaries to do so.

These pin the three properties that make it safe to turn on: it cannot
introduce an object the caller may not see, it cannot make things worse when
the model misbehaves, and it survives the shapes models actually reply in.
"""
import pytest

from schemagate import Catalog, Principal
from schemagate.demo_schema import create_demo_db
from schemagate.rerank import build_prompt, parse_choice, rerank


class Doc:
    def __init__(self, qname, hint=""):
        self.qname = qname
        self.hint = hint
        self.description = ""
        self.columns = []


DOCS = [Doc(f"main.t{i}", f"table {i}") for i in range(1, 6)]


class Reply:
    def __init__(self, text): self.text = text
    def complete(self, system, prompt, max_tokens=120): return self.text


@pytest.mark.parametrize("text,want", [
    ("3, 1", [3, 1]),
    ("3,1", [3, 1]),
    ("**3**, 1", [3, 1]),
    ("3\n1\n", [3, 1]),
    ("The answer is 3 then 1.", [3, 1]),
    ("1. main.t3\n2. main.t1", [1, 2]),      # a numbered list is its own answer
    # The names on the schema this was measured against: 180 tables called
    # stg_feed_007 and audit_event_042. A bare \\d+ reads those as choices.
    ("1. stg_feed_007\n2. audit_event_042", [1, 2]),
    ("main.tbl_183 and main.tbl_042", []),
])
def test_the_shapes_models_actually_reply_in(text, want):
    assert parse_choice(text, 5, 6) == want


def test_a_number_outside_the_list_is_dropped_not_fatal():
    """A model that invents `47` for a list of five has still given four
    usable answers. Throwing the reply away would waste them."""
    assert parse_choice("2, 47, 4", 5, 6) == [2, 4]


def test_a_repeat_is_counted_once():
    assert parse_choice("2, 2, 4", 5, 6) == [2, 4]


def test_top_k_is_a_limit_the_model_cannot_talk_past():
    assert parse_choice("1,2,3,4,5", 5, top_k=2) == [1, 2]


def test_the_chosen_come_first_and_nothing_is_lost():
    """Reranking reorders. An object the model did not mention is demoted,
    never dropped -- it may still be pulled in by a foreign key."""
    out = rerank(Reply("4, 2"), "q", DOCS, top_k=6, candidates=5)
    assert [d.qname for d in out][:2] == ["main.t4", "main.t2"]
    assert set(d.qname for d in out) == set(d.qname for d in DOCS)


def test_objects_below_the_candidate_window_keep_their_place():
    out = rerank(Reply("2"), "q", DOCS, top_k=6, candidates=3)
    assert [d.qname for d in out] == ["main.t2", "main.t1", "main.t3",
                                      "main.t4", "main.t5"]


@pytest.mark.parametrize("bad", [
    RuntimeError("503 from the provider"),
    TimeoutError("read timed out"),
])
def test_a_model_that_fails_leaves_the_maths_order_untouched(bad):
    """The whole feature is opt-in on the promise that it cannot make things
    worse. A provider having a bad day must be a no-op, not an error."""
    class Boom:
        def complete(self, *a, **k): raise bad
    assert rerank(Boom(), "q", DOCS) == DOCS


def test_an_unparseable_reply_is_also_a_no_op():
    assert rerank(Reply("I'm sorry, I can't help with that."), "q", DOCS) == DOCS


def test_one_candidate_is_not_worth_a_call():
    class Boom:
        def complete(self, *a, **k): raise AssertionError("called the model")
    assert rerank(Boom(), "q", DOCS[:1]) == DOCS[:1]


def test_the_prompt_carries_summaries_not_the_schema():
    """Reranking is cheap because it ranks one-line summaries. If it ever
    started including DDL it would cost what this library exists to avoid."""
    p = build_prompt("which customers owe us money", DOCS, 6)
    assert "main.t1" in p and "table 1" in p
    assert "CREATE TABLE" not in p and "VARCHAR" not in p


def test_a_restricted_object_cannot_be_promoted_into_an_answer():
    """The reranker is handed the already-filtered list, so a table this
    caller may not see is not in it to be chosen. This is the property that
    makes the feature safe to enable by default in a UI."""
    cat = Catalog().bootstrap(create_demo_db())
    cat.restrict("hr_compensation", ["payroll"])

    class GrabsEverything:
        def complete(self, system, prompt, max_tokens=120):
            assert "hr_compensation" not in prompt
            return "1, 2, 3"

    sel = cat.select("employee compensation", top_k=5,
                     principal=Principal("demo:analyst"),
                     reranker=GrabsEverything())
    assert "hr_compensation" not in [d.name for d in sel.objects]
