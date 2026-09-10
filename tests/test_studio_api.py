"""The Studio endpoints behind the browser UI.

A key typed into a form is wrong more often than one exported in a shell, and
a provider is misconfigured more often than it is configured. Every test here
is about the page surviving that: nothing a user can type into the settings
panel should be able to take the page down or make selection stop working.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest
from sqlalchemy import create_engine

from schemagate import Catalog
from schemagate.demo_schema import create_demo_db
from schemagate.studio import StudioState, serve


@pytest.fixture
def api():
    url = create_demo_db()
    engine = create_engine(url)
    cat = Catalog(name="t").bootstrap(engine)
    cat.restrict("hr_compensation", ["payroll"])
    state = StudioState(cat, "t", "b", [], engine=engine)
    srv = serve(state, "127.0.0.1", 0, False)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, body=None):
        base = f"http://127.0.0.1:{port}{path}"
        if body is None:
            return json.load(urllib.request.urlopen(base))
        req = urllib.request.Request(base, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    yield call, state
    srv.shutdown()


def test_the_api_key_never_comes_back_out(api):
    """The page needs to know whether a key is set. It never needs the key,
    and anything the endpoint returns can end up in a screenshot."""
    call, _ = api
    call("/api/settings", {"provider": "anthropic", "model": "m",
                           "api_key": "sk-secret-value"})
    assert "sk-secret-value" not in json.dumps(call("/api/settings"))
    assert call("/api/settings")["has_key"] is True


def test_settings_only_accept_known_keys(api):
    """The body is user input posted to a local server. It sets settings,
    not arbitrary attributes."""
    call, state = api
    call("/api/settings", {"provider": "none", "catalog": "replaced",
                           "engine": "replaced"})
    assert "catalog" not in state.settings and "engine" not in state.settings


def test_a_provider_that_cannot_be_built_falls_back_to_the_paste_prompt(api):
    """An unknown name, or an SDK that is not installed: nothing can be
    constructed, so there is no model and the paste path is the answer.

    This used to name a real provider with a wrong key, which made the test
    depend on whether that optional SDK happened to be installed -- it passed
    by hitting ImportError and started failing the moment the package was
    present. An unknown name fails to build on every machine."""
    call, _ = api
    call("/api/settings", {"provider": "no-such-provider", "model": "nope",
                           "api_key": "wrong", "rerank": True})
    out = call("/api/answer", {"question": "which customers owe us money",
                               "top_k": 4})
    assert out["objects"], "selection stopped working because a provider failed"
    assert "paste_prompt" in out
    assert out.get("answer_error")          # says what went wrong


def test_a_provider_that_builds_but_cannot_answer_reports_the_error(api):
    """The other half: the provider constructs, the call fails -- a wrong key,
    a rate limit, an outage. There is a model, so the paste path is not the
    answer; the reason is."""
    call, state = api
    call("/api/settings", {"provider": "x", "model": "m"})

    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("401 invalid x-api-key")

    state._provider = lambda: Broken()
    out = call("/api/answer", {"question": "which customers owe us money"})
    assert out["objects"], "selection stopped working because a provider failed"
    assert "paste_prompt" not in out
    assert "401" in out["answer_error"]


def test_with_no_provider_the_answer_is_a_prompt_to_paste(api):
    call, _ = api
    call("/api/settings", {"provider": "none"})
    out = call("/api/answer", {"question": "which customers owe us money"})
    assert "paste_prompt" in out and "Question:" in out["paste_prompt"]
    assert "answer_error" not in out        # not an error, a different path


def test_a_restricted_object_stays_hidden_through_the_api(api):
    """The whole point, exercised over HTTP rather than in-process."""
    call, _ = api
    out = call("/api/answer", {"question": "employee compensation",
                               "principal": "demo:analyst"})
    names = [o["name"] for o in out["objects"]]
    assert "main.hr_compensation" not in names
    assert "main.hr_compensation" in out["hidden"]
    assert "hr_compensation" not in out["ddl"]


def test_the_role_lets_it_through(api):
    call, _ = api
    out = call("/api/answer", {"question": "employee compensation",
                               "principal": "demo:hr", "roles": ["payroll"]})
    assert "main.hr_compensation" not in out["hidden"]


def test_an_empty_question_is_a_message_not_a_stack_trace(api):
    call, _ = api
    assert "error" in call("/api/answer", {"question": "   "})


def test_unknown_routes_are_404(api):
    call, _ = api
    with pytest.raises(urllib.error.HTTPError) as e:
        call("/api/anything", {})
    assert e.value.code == 404


def test_cataloguing_without_a_key_hands_back_a_prompt(api):
    """The step that raises accuracy most is the one people skip, because it
    used to mean a command line and a JSON file. Without a key it must still
    be reachable: a prompt to paste into any chat."""
    call, _ = api
    out = call("/api/describe", {})
    assert out["paste_prompt"] and out["pending"] > 0
    assert "JSON object" in out["paste_prompt"]


def test_only_metadata_is_ever_sent_for_cataloguing(api):
    """The prompt carries names, types, comments and foreign keys. Never
    rows. That promise is older than this endpoint and must survive it."""
    call, state = api
    prompt = call("/api/describe", {})["paste_prompt"]
    # a value that exists in the demo data, and must not be in the prompt
    with state.engine.connect() as conn:
        from sqlalchemy import text
        name = conn.execute(text("SELECT display_name FROM core_party LIMIT 1")).scalar()
    assert name and name not in prompt


def test_a_pasted_reply_is_applied_and_changes_selection(api):
    """The point of cataloguing, asserted rather than assumed: a question
    phrased in business words finds the table whose name shares none of them.
    This is the 50% row in the README."""
    call, _ = api
    # "get paid" shares no words with `hr_compensation` or its columns --
    # `annual_amount`, `pay_grade`. That is exactly the gap identifier
    # matching cannot close and a description can.
    q = "what does each person get paid"
    # hr_compensation is restricted in this fixture, so ask as someone who may
    # see it -- otherwise this would be testing the role check, not the
    # description.
    who = {"principal": "demo:hr", "roles": ["payroll"]}
    before = [o["name"] for o in
              call("/api/select", dict(question=q, top_k=4, **who))["objects"]]
    assert "main.hr_compensation" not in before

    call("/api/apply-descriptions", {"descriptions": json.dumps(
        {"main.hr_compensation":
         "What each employee is paid. | salary, wages, earnings, take home"})})
    after = [o["name"] for o in
             call("/api/select", dict(question=q, top_k=4, **who))["objects"]]
    assert after[0] == "main.hr_compensation", after


def test_a_reply_wrapped_in_code_fences_still_applies(api):
    """Every chat window wraps JSON in ```json. Making the user strip that by
    hand is a step that fails silently when they forget."""
    call, _ = api
    out = call("/api/apply-descriptions",
               {"descriptions": '```json\n{"main.crm_customer": "People who buy from us."}\n```'})
    assert out["written"] == 1


def test_a_reply_that_is_not_json_says_so(api):
    call, _ = api
    assert "error" in call("/api/apply-descriptions", {"descriptions": "sure, here you go"})


def test_descriptions_must_be_an_object_not_a_list(api):
    call, _ = api
    assert "error" in call("/api/apply-descriptions", {"descriptions": "[1, 2, 3]"})
