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


def test_serve_exposes_its_thread_so_main_can_wait_on_it_interruptibly():
    """`main()` used to wait with `threading.Event().wait(3600)`.

    Python runs a signal handler only between bytecodes in the main thread,
    and Windows has no EINTR to cut a wait short -- so Ctrl+C sat unhandled
    for up to an hour and the only way out was killing python.exe. On Linux
    and macOS the wait is interrupted immediately, which is why this survived:
    it was never broken on the machines it was written on.

    The fix waits in short slices on the serving thread, so `serve()` has to
    hand that thread back.
    """
    import threading

    from schemagate import Catalog
    from schemagate.demo_schema import create_demo_db
    from schemagate.studio import StudioState, serve

    srv = serve(StudioState(Catalog(name="t").bootstrap(create_demo_db()),
                            "t", "b", []), "127.0.0.1", 0, False)
    try:
        thread = getattr(srv, "serve_thread", None)
        assert isinstance(thread, threading.Thread) and thread.is_alive()
    finally:
        srv.shutdown()
        srv.server_close()


def test_main_waits_in_short_slices_and_closes_the_socket():
    """Two things, both of which a user notices and neither of which a unit
    test of `serve()` would catch: the wait must be short enough for a signal
    to land between slices, and the listening socket must be closed rather
    than left to the process exit -- otherwise an immediate restart fails with
    "address already in use"."""
    import inspect

    from schemagate.studio import main

    # Comments stripped first. The comment that explains this fix quotes the
    # very call it forbids, so a plain substring check matches the prose and
    # fails on correct code -- which is what it did the first time it ran.
    src = inspect.getsource(main)
    code = "\n".join(ln.split("#")[0] for ln in src.splitlines())

    assert "Event().wait(" not in code, "the un-interruptible wait is back"
    assert "thread.join(0.5)" in code
    assert "server_close()" in code


# --- connecting to a database from the page --------------------------------

def test_connecting_is_refused_unless_the_server_allowed_it(api):
    """The one thing on this page that reaches outside the process.

    Without this, anyone who can reach the Studio can hand it a URL and have
    the server connect on their behalf -- to a host only the server can see,
    with whatever credentials they typed. A page bound to 0.0.0.0 would be a
    URL box on the internet. So the person who started the server decides,
    not the person looking at it.
    """
    call, state = api
    assert state.allow_connect is False, "must be off unless asked for"
    out = call("/api/connect", {"url": "sqlite:///anything.db"})
    assert "error" in out and "allow-remote-connect" in out["error"]


def test_a_failed_connection_does_not_echo_the_url_back(api):
    """A SQLAlchemy URL carries a password, and driver errors quote the URL
    they were given. Returning that puts the password in the page, in the
    browser's network log, and in any screenshot of either."""
    call, state = api
    state.allow_connect = True
    out = call("/api/connect",
               {"url": "postgresql+psycopg://user:hunter2@127.0.0.1:1/nope"})
    assert "error" in out
    assert "hunter2" not in out["error"] and "@" not in out["error"]


def test_connecting_replaces_the_catalog(api, tmp_path):
    import sqlite3
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE widget (id INTEGER PRIMARY KEY, sku TEXT);")
    con.commit(); con.close()

    call, state = api
    state.allow_connect = True
    out = call("/api/connect", {"url": f"sqlite:///{db}"})
    assert out["objects"] == 1 and out["dialect"] == "sqlite"
    names = [o["name"] for o in call("/api/select", {"question": "widgets"})["objects"]]
    assert names == ["main.widget"], names          # qualified, as the API reports


def test_the_page_is_told_whether_connecting_is_even_possible(api):
    """Showing a Connect box that the endpoint will always refuse is a button
    that fails every time."""
    call, state = api
    assert call("/api/settings")["allow_connect"] is False
    state.allow_connect = True
    assert call("/api/settings")["allow_connect"] is True


# --- running SQL from the page ---------------------------------------------

def test_run_sql_returns_rows(api):
    out = api[0]("/api/run-sql",
                 {"sql": "SELECT display_name FROM core_party ORDER BY id LIMIT 2"})
    assert out["columns"] == ["display_name"] and len(out["rows"]) == 2


@pytest.mark.parametrize("sql", [
    "DROP TABLE core_party",
    "UPDATE core_party SET display_name = 'x'",
    "SELECT 1; DROP TABLE core_party",
    "SELECT 1 -- \n; DELETE FROM core_party",
])
def test_run_sql_refuses_anything_that_is_not_a_single_read(api, sql):
    """Same guard as `--sql`, not a second copy of it: this endpoint calls
    `answer.run_sql`, which checks again at the point the text reaches the
    database."""
    assert "refused" in api[0]("/api/run-sql", {"sql": sql}).get("error", "")


def test_run_sql_without_a_database_says_so_rather_than_failing(api):
    call, state = api
    state.engine = None
    assert call("/api/run-sql", {"sql": "SELECT 1"})["error"] == "no database connected"


def test_both_spellings_of_the_connect_flag_work():
    """`--allow-remote-connect` read as "let someone control this machine
    remotely", which is not what it does -- it lets the page hand the server a
    database URL. The clearer name is documented now, but the original shipped
    in 0.1.14 and anyone using it should not have it break."""
    from schemagate.cli import build_parser

    for flag in ("--allow-connect", "--allow-remote-connect"):
        assert build_parser().parse_args(["studio", flag]).allow_connect is True
    assert build_parser().parse_args(["studio"]).allow_connect is False


def test_one_install_can_bring_every_driver():
    """"pick the extra that matches your database" is a step people should not
    have to take to try something. `[all]` exists so one command does it --
    but not by default, because `import schemagate` must not be able to fail
    over a driver nobody is using."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        import pytest
        pytest.skip("not an installed-from-source tree")

    data = tomllib.loads(pyproject.read_text())["project"]
    extras = data["optional-dependencies"]
    assert len(data["dependencies"]) == 1, "the base install is one dependency"

    for driver in ("oracledb", "psycopg", "pyodbc", "PyMySQL"):
        assert any(driver in p for p in extras["databases"]), driver
        assert any(driver in p for p in extras["all"]), driver
    # the two that are large enough to ask for by name
    assert not any("torch" in p for p in extras["all"])
    assert not any(p.startswith("oci") for p in extras["all"])


def test_connecting_hands_back_the_new_catalog_not_the_old_one(api, tmp_path):
    """The page is built around the bundled demo, whose hints, restricted
    object and example questions are baked into it. After connecting to a real
    database none of that belongs, and leaving it is worse than untidy: the
    rail would claim `hr_compensation needs payroll` about a database that has
    no such table, which is the page describing an access rule that does not
    exist.
    """
    import sqlite3

    db = tmp_path / "fresh.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE widget (id INTEGER PRIMARY KEY, sku TEXT);")
    con.commit(); con.close()

    call, state = api
    state.allow_connect = True
    out = call("/api/connect", {"url": f"sqlite:///{db}"})

    assert out["demo"] is False
    schema = out["schema"]
    assert schema["hints"] == {} and schema["restrict"] == {}
    assert schema["questions"] == []
    assert "Nothing is catalogued yet" in schema["blurb"]


def test_cataloguing_after_connecting_describes_the_new_database(api, tmp_path):
    """The catalogue button has to follow the connection. Describing the demo
    schema while the page says it is connected to something else is the same
    class of lie as the stale rail."""
    import sqlite3

    db = tmp_path / "fresh2.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE invoice_line (id INTEGER PRIMARY KEY, qty INT);")
    con.commit(); con.close()

    call, state = api
    state.allow_connect = True
    call("/api/connect", {"url": f"sqlite:///{db}"})
    prompt = call("/api/describe", {})["paste_prompt"]
    assert "invoice_line" in prompt
    assert "crm_customer" not in prompt, "still describing the demo schema"
