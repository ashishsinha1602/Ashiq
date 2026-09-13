"""The local Studio server, driven over real HTTP."""
import json
import pathlib
import urllib.request

import pytest

from schemagate import studio as st


@pytest.fixture(scope="module")
def server():
    from schemagate import Catalog
    from schemagate.demo_schema import HINTS, create_demo_db
    cat = Catalog(name="studio-test").bootstrap(create_demo_db())
    for t, h in HINTS.items():
        cat.hint(t, h)
    cat.restrict("hr_compensation", ["payroll"])
    state = st.StudioState(cat, "Test", "test schema", ["revenue by month"])
    srv = st.serve(state, port=0, open_browser=False)   # port 0: any free port
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(base, payload):
    req = urllib.request.Request(base + "/api/select", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_page_is_served_with_the_api_switch(server):
    html = urllib.request.urlopen(server + "/", timeout=10).read().decode()
    assert "<title>schemagate studio</title>" in html
    assert 'window.SCHEMAGATE_API="/api"' in html
    assert '"title": "Test"' in html or '"title":"Test"' in html


def test_health(server):
    out = json.loads(urllib.request.urlopen(server + "/api/health", timeout=10).read())
    assert out["status"] == "ok" and out["objects"] == 42


def test_select_scopes_by_identity(server):
    code, anon = _post(server, {"question": "salary by employee", "top_k": 10})
    assert code == 200 and "main.hr_compensation" not in [o["name"] for o in anon["objects"]]
    assert "main.hr_compensation" in anon["hidden"]
    code, hr = _post(server, {"question": "salary by employee", "top_k": 10,
                              "principal": "okta:hr", "roles": ["payroll"]})
    assert "main.hr_compensation" in [o["name"] for o in hr["objects"]]
    assert hr["hidden"] == []


def test_select_returns_what_the_page_needs(server):
    code, out = _post(server, {"question": "revenue by month"})
    assert code == 200
    assert {"objects", "total", "ddl", "tokens", "full_tokens", "hidden", "shadows"} <= set(out)
    assert out["tokens"] < out["full_tokens"]
    assert all({"name", "kind", "columns", "score", "reason", "shadow"} <= set(o) for o in out["objects"])


def test_bad_principal_is_400_not_500(server):
    code, out = _post(server, {"question": "x", "principal": "nocolon"})
    assert code == 400 and "namespaced" in out["error"]


def test_garbage_body_does_not_kill_the_server(server):
    req = urllib.request.Request(server + "/api/select", data=b"not json",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
    for bad in [[], "str", 42, {"question": None}, {"question": "x", "top_k": "lots"}]:
        code, _ = _post(server, bad) if isinstance(bad, dict) else (400, None)
        assert code in (200, 400)
    code, out = _post(server, {"question": "revenue by month"})
    assert code == 200 and out["objects"], "server did not survive"


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(server + "/nope", timeout=10)
    assert e.value.code == 404


def test_cli_wires_studio():
    from schemagate.cli import build_parser
    args = build_parser().parse_args(["studio", "--no-browser", "--port", "0"])
    assert args.func.__name__ == "cmd_studio"


# --- `schemagate studio --url` and the wallet -----------------------------
#
# This was broken in every release up to 0.1.44 and nothing caught it, because
# every Oracle test in this repo built its engine with a custom `creator=` and
# called the library directly. Nobody does that. The documented way to open
# the Studio on an Autonomous Database is the flag, and the flag could not
# work: the URL branch called create_engine() and dropped the one variable a
# wallet travels in.

def test_studio_url_path_merges_connect_args_from_the_environment():
    """`--url` must go through engine_from_url, not create_engine.

    SCHEMAGATE_CONNECT_ARGS is the only route for a connection a URL cannot
    express -- an Autonomous Database wallet is the documented case -- and
    only engine_from_url merges it. Asserted against the source of the branch
    because the alternative is a live Oracle in CI.
    """
    import re

    src = pathlib.Path(st.__file__).read_text("utf-8")
    i = src.index("def main(")
    branch = src[i:i + 4000]
    j = branch.index("if url:")
    # far enough past `if url:` to clear the comment and reach the call
    window = branch[j:j + 1600]

    assert "engine_from_url(" in window, (
        "the --url branch must use engine_from_url so a wallet passed in "
        "SCHEMAGATE_CONNECT_ARGS reaches the driver")
    assert not re.search(r"\bcreate_engine\(\s*url\b", window), (
        "create_engine(url, ...) here silently drops SCHEMAGATE_CONNECT_ARGS; "
        "DPY-4027 'no configuration directory specified' is what the user sees")


def test_engine_from_url_actually_merges_the_environment(monkeypatch, tmp_path):
    """And the merge itself works, on a database the suite can really open."""
    from schemagate.introspect import connect_args_from_env, engine_from_url

    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS", '{"timeout": 17}')
    assert connect_args_from_env() == {"timeout": 17}

    # sqlite3.connect takes `timeout`, so a wrong value would raise here.
    eng = engine_from_url("sqlite:///" + str(tmp_path / "t.db"))
    with eng.connect() as c:
        assert c.exec_driver_sql("select 1").scalar() == 1
