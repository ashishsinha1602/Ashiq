"""What the Studio opens on when you just run `schemagate studio`.

Until 0.1.18 a bare launch loaded the bundled 42-object sample schema and the
header announced "connected to your database" over it. Both halves were wrong
in the same direction: someone who ran the command to point the tool at their
own data got invented tables labelled as theirs, and the only way to tell was
to recognise that `sales_order` was not a table they had.

So: nothing loads unless it was asked for, and the server never claims a
connection it does not have.
"""
import inspect
import json
import threading
import urllib.request

import pytest

from schemagate.studio import main


def _settings(state):
    """Drive the real endpoint rather than reading the attribute, because the
    attribute is not what the page believes -- the JSON is."""
    from schemagate.studio import serve

    srv = serve(state, "127.0.0.1", 0, False)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/settings"))
    finally:
        srv.shutdown()
        srv.server_close()


def _state(**kw):
    """Build the state `main()` would build, without blocking on the server."""
    captured = {}

    from schemagate import studio

    real_serve = studio.serve

    def fake_serve(state, host, port, open_browser):
        captured["state"] = state
        srv = real_serve(state, "127.0.0.1", 0, False)
        srv.shutdown()
        srv.server_close()

        class _Done:
            serve_thread = None

            def shutdown(self):
                pass

            def server_close(self):
                pass

        return _Done()

    studio.serve = fake_serve
    try:
        main(open_browser=False, **kw)
    finally:
        studio.serve = real_serve
    return captured["state"]


def test_a_bare_studio_loads_nothing():
    """The command that means "I want to use this on my database" must not
    quietly answer with someone else's tables."""
    state = _state()
    assert len(state.catalog._docs) == 0
    assert state.engine is None
    assert state.is_demo is False


def test_the_sample_schema_still_exists_but_has_to_be_asked_for():
    state = _state(demo=True)
    assert len(state.catalog._docs) == 42
    assert state.is_demo is True


def test_a_url_is_a_real_connection_not_a_demo(tmp_path):
    import sqlite3

    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE t(id INTEGER PRIMARY KEY);")
    state = _state(url=f"sqlite:///{db}")
    assert state.is_demo is False
    assert len(state.catalog._docs) == 1


@pytest.mark.parametrize("kw,connected,demo", [
    ({}, False, False),
    ({"demo": True}, False, True),
])
def test_the_server_does_not_claim_a_connection_it_lacks(kw, connected, demo):
    """`connected` drives the header. An engine alone cannot answer it: the
    demo has an engine too, and that is exactly how the sample schema came to
    be labelled as the user's own database."""
    out = _settings(_state(**kw))
    assert out["connected"] is connected
    assert out["demo"] is demo


def test_connecting_clears_the_demo_flag(tmp_path):
    """Someone who opens --demo to look around and then connects for real must
    not be left with the page still calling it a demo."""
    import sqlite3

    db = tmp_path / "real.db"
    sqlite3.connect(db).executescript("CREATE TABLE invoice(id INTEGER PRIMARY KEY);")
    state = _state(demo=True)
    state.allow_connect = True
    assert state.is_demo is True

    out = state.connect({"url": f"sqlite:///{db}"})
    assert out.get("error") is None, out
    assert state.is_demo is False
    assert out["objects"] == 1
    assert _settings(state)["connected"] is True


def test_demo_is_a_documented_flag_not_a_hidden_one():
    """It is the answer to "where did my sample schema go", so it has to be
    discoverable from `studio --help` rather than from the source.

    Asserting on the top-level help would pass without the flag existing at
    all -- `demo` has been a subcommand since before this -- so this reads the
    studio subparser itself."""
    from schemagate.cli import build_parser

    sub = [a for a in build_parser()._actions
           if hasattr(a, "choices") and a.choices and "studio" in a.choices]
    studio_help = sub[0].choices["studio"].format_help()
    assert "--demo" in studio_help
    assert "demo" in inspect.signature(main).parameters


def test_the_page_does_not_hardcode_the_connected_header():
    """The header said "connected to your database" for anything served by the
    backend, demo included. The string must be reachable only through the
    server's answer now, not written once at startup."""
    import pathlib as _p

    page = _p.Path(__file__).resolve().parents[1] / "src" / "schemagate" / "studio.html"
    html = page.read_text("utf-8")
    assert "no database connected" in html
    # the startup hardcode itself, which is what made the demo claim to be the
    # user's database. Counting occurrences would break on any copy edit; the
    # thing that must not come back is this one assignment.
    assert "$(\"mode\").innerHTML='<span>connected to your database</span>'" not in html
    assert 'mt.textContent = st.connected ? "connected to your database"' in html


# ---- the two tabs -------------------------------------------------------

def _served_page(state):
    """The HTML the backend actually sends, which is not the file on disk --
    it rewrites the schema payload on the way out."""
    import threading
    import urllib.request

    from schemagate.studio import serve

    srv = serve(state, "127.0.0.1", 0, False)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
    finally:
        srv.shutdown()
        srv.server_close()


def _schema_payload(html):
    import json
    import re

    m = re.search(r'<script id="schemas" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_the_backend_serves_both_a_live_tab_and_the_demo():
    """The live catalog used to replace the bundled one, which left a Studio
    with nothing connected showing only invented tables and no way back."""
    payload = _schema_payload(_served_page(_state()))
    assert list(payload) == ["live", "commerce"]
    assert payload["live"]["title"] == "Your database"
    assert payload["commerce"]["title"] == "Demo schema"
    assert len(payload["commerce"]["docs"]) == 42


def test_the_live_tab_is_the_one_that_opens():
    html = _served_page(_state())
    assert 'keys.includes("live") ? "live"' in html


def test_the_demo_tab_does_not_ask_the_server_about_its_tables():
    """Selection is routed by tab. The demo's tables do not exist in whatever
    the backend is connected to; asking would either error or match something
    real, and the second is worse."""
    html = _served_page(_state())
    assert 'if (API && state.schema === "live")' in html


# ---- connecting is the page, until there is a connection ----------------

def test_the_connect_form_moves_to_the_middle_when_nothing_is_connected():
    """It was the third panel in a nine-panel rail: the one thing the page
    needs from you, sized like a footnote. Moved, not duplicated -- the same
    section, so its fields and handlers survive the trip."""
    html = _served_page(_state())
    assert 'id="connectSlot"' in html
    assert "if (needsConnect && panel.parentNode !== slot) slot.appendChild(panel)" in html
    assert "railHome.insertBefore(panel, railAfter)" in html


def test_the_working_view_is_hidden_until_there_is_something_to_work_on():
    html = _served_page(_state())
    assert 'id="workView"' in html
    assert '$("workView").hidden = needsConnect' in html


def test_the_model_question_is_asked_once_on_connect():
    """Buried in a rail panel it reads as configuration, and nobody configures
    a thing they have not seen work yet. It belongs at the moment the catalog
    starts existing."""
    html = _served_page(_state())
    assert 'id="modelChoice"' in html
    for el in ("mcSkip", "mcLocal", "mcKey"):
        assert f'id="{el}"' in html


def test_leaving_the_model_question_re_reads_the_settings():
    """lastSettings was captured before the connection existed. Re-applying it
    decided the page still needed connecting and pulled the form back to the
    middle of the screen, over a catalog that had just loaded."""
    html = _served_page(_state())
    assert 'lastSettings = await (await fetch(API+"/settings")).json()' in html


def test_the_code_block_is_this_connection_not_an_illustration():
    """The Studio is a test bench; the library is the product. A code block
    with someone else's database, someone else's question and someone else's
    identity in it teaches the shape and then has to be rewritten line by
    line. This one is paste-ready."""
    html = _served_page(_state())
    assert 'id="codePy"' in html
    assert "function paintCode(q, principal)" in html
    assert "state.recipe && state.recipe.python" in html
    # the API it writes has to be the API that exists
    from schemagate import Catalog
    sel = Catalog().select("x")
    assert hasattr(sel, "prompt_fragment")


def test_the_favicon_and_mark_travel_with_the_page():
    """A self-contained page cannot fetch an icon, and the mark has to follow
    the theme without a second colour definition."""
    html = _served_page(_state())
    assert 'rel="icon" href="data:image/svg+xml,' in html
    assert 'class="mark"' in html
    assert 'stroke="currentColor"' in html


# ---- settings belong in settings ----------------------------------------

def test_identity_and_model_are_configuration_not_screen_furniture():
    """Who is asking and which model to use are set once and then in the way.
    The working screen is a question and its answer."""
    html = _served_page(_state())
    assert 'id="settingsDrawer"' in html and 'id="settingsBtn"' in html
    assert 'for (const id of ["whoPanel", "modelPanel", "recipePanel"])' in html
    assert "body.appendChild(el)" in html


def test_the_drawer_is_set_up_on_load_not_after_connecting():
    """It was first wired inside the connect handler -- one of five call sites
    that look identical -- so the panels only moved once a database existed,
    and the page opened with the rail it was supposed to have replaced."""
    html = _served_page(_state())
    i_settings = html.index("function settings()")
    i_boot = html.index('$("q").value = SCHEMAS[state.schema].questions[0]')
    i_connect = html.index("async function doConnect") if "async function doConnect" in html else None
    assert i_settings < i_boot, "the drawer must be built in the bootstrap path"
    if i_connect is not None:
        assert not (i_connect < i_settings < i_boot)


def test_the_panels_are_moved_not_copied():
    """Two copies of a form means two sets of ids, and the second one wins the
    getElementById that every handler here uses."""
    html = _served_page(_state())
    assert html.count('id="whoPanel"') == 1
    assert html.count('id="principal"') == 1
    assert html.count('id="modelPanel"') == 1
