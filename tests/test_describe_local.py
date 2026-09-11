"""The catalog has to survive a small local model, not just a frontier one.

Every `raw` string below is the shape a 1.5B instruct model actually returns
when handed the describe prompt: a preamble, a markdown heading, three
sentences instead of one, the object name echoed back, the everyday words
missing or filled with column names. None of these is an error -- each is a
non-empty string that would be stored and would quietly make retrieval worse.
"""
import pytest

from schemagate.ai.describe import (
    SchemaDescriber, clean_description, is_usable, split_description)
from schemagate.models import Column, ObjectDoc

GOOD_ALIASES = ("what people bought, basket, cart, items ordered, shopping, "
                "purchases, how many, line total, spend, per item")


def doc():
    return ObjectDoc(
        name="cust_ord_ln_t", schema="sales", kind="table",
        columns=[Column(name="id", type="NUMBER", pk=True),
                 Column(name="ord_id", type="NUMBER"),
                 Column(name="qty", type="NUMBER"),
                 Column(name="amt", type="NUMBER")])


class Scripted:
    """Returns each queued reply in turn, and records what it was asked."""

    name = "local:test"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, prompt, max_tokens=1024):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


@pytest.mark.parametrize("raw,why", [
    ("Sure! Here is the description: One row per order line. | " + GOOD_ALIASES,
     "preamble"),
    ("**Description:** One row per order line. | " + GOOD_ALIASES,
     "markdown emphasis"),
    ("- One row per order line. | " + GOOD_ALIASES, "bullet"),
    ('"One row per order line." | ' + GOOD_ALIASES, "wrapping quotes"),
    ("sales.cust_ord_ln_t holds one row per order line. | " + GOOD_ALIASES,
     "echoes the qualified name"),
    ("One row per order line.|" + GOOD_ALIASES, "bare pipe, no spaces"),
])
def test_repairs_small_model_junk(raw, why):
    out = clean_description(raw, doc())
    head, tail = split_description(out)
    assert head.startswith("One row per order line"), (why, out)
    assert "basket" in tail, (why, out)
    assert is_usable(out, doc()), (why, out)


def test_extra_sentences_are_kept_not_trimmed():
    """A two-sentence reply has always been stored whole.

    Trimming to the single sentence the prompt asks for would be a quiet
    rewrite of descriptions people already have cached, so rambling is bounded
    by a word cap instead of by cutting at the first full stop.
    """
    out = clean_description(
        "One row per order line. It also carries the discount. | " + GOOD_ALIASES,
        doc())
    head, _ = split_description(out)
    assert head == "One row per order line. It also carries the discount."


def test_a_short_sentence_with_aliases_is_not_called_truncated():
    """The pipe is the evidence that the model got to the end of the format.

    Judging the sentence in isolation throws that away, and every sound reply
    under twelve words with no full stop reads as cut off -- which warned on
    34 of 39 objects and bought each a second call it did not need.
    """
    short = "Alerts raised by sanctions screening | flagged, watchlist, hits"
    assert is_usable(short, doc())


def test_a_missing_full_stop_is_not_tidied_onto_a_cut_reply():
    """Terminal punctuation is how truncation is detected.

    Adding one here would make every cut-off reply look finished, which is the
    OCI bug (descriptions at ~10 tokens, recall down 20 points, no error).
    """
    cut = "This view shows which devices are"
    assert clean_description(cut, doc()) == cut
    assert not is_usable(cut, doc())


def test_column_names_are_not_accepted_as_everyday_words():
    """The alias half exists to add vocabulary the schema does not have."""
    out = clean_description(
        "One row per order line. | qty, amt, ord_id, basket, cart, shopping, "
        "purchases, how many, spend, per item, trolley, goods", doc())
    _, tail = split_description(out)
    got = [w.strip() for w in tail.split(",")]
    assert "qty" not in got and "amt" not in got and "ord_id" not in got
    assert "basket" in got and "trolley" in got


def test_retry_when_the_format_was_ignored():
    """A reply with no everyday words is not usable, and is worth one retry."""
    bad = "Here is a summary:\n- id: the primary key\n- qty: the quantity"
    good = "One row per order line. | " + GOOD_ALIASES
    p = Scripted(bad, good)
    got = SchemaDescriber(p, workers=1).describe([doc()])
    assert len(p.prompts) == 2, "should have retried exactly once"
    assert "not in the required format" in p.prompts[1]
    assert "basket" in got["sales.cust_ord_ln_t"]


def test_a_good_first_reply_is_not_retried():
    p = Scripted("One row per order line. | " + GOOD_ALIASES)
    SchemaDescriber(p, workers=1).describe([doc()])
    assert len(p.prompts) == 1, "a usable reply must not cost a second call"


def test_unusable_after_retry_is_reported_not_hidden():
    d = SchemaDescriber(Scripted("nope", "still nope"), workers=1)
    with pytest.warns(RuntimeWarning):
        d.describe([doc()])
    assert d.truncated == ["sales.cust_ord_ln_t"]


def test_frontier_output_passes_through_unchanged():
    """The repair layer must not damage a reply that was already correct."""
    good = "One row per item on a customer order, with quantity and line amount."
    out = clean_description(good + " | " + GOOD_ALIASES, doc())
    assert out == good + " | " + GOOD_ALIASES


def test_no_row_data_reaches_the_provider():
    p = Scripted("One row per order line. | " + GOOD_ALIASES)
    SchemaDescriber(p, workers=1).describe([doc()])
    sent = p.prompts[0]
    assert "cust_ord_ln_t" in sent and "qty" in sent
    assert "NUMBER" in sent
