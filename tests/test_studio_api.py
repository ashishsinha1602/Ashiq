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


def test_a_provider_that_cannot_be_built_does_not_break_the_page(api):
    """Wrong key, missing SDK, typo in the model -- selection must still work
    and the answer must fall back to a prompt you can paste."""
    call, _ = api
    call("/api/settings", {"provider": "anthropic", "model": "nope",
                           "api_key": "wrong", "rerank": True})
    out = call("/api/answer", {"question": "which customers owe us money",
                               "top_k": 4})
    assert out["objects"], "selection stopped working because a provider failed"
    assert "paste_prompt" in out
    assert out.get("answer_error")          # says what went wrong


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
