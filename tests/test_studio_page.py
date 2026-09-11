"""The Studio page itself.

The API tests prove the endpoints work. These prove the page reaches them,
which is a different thing entirely -- for a while every endpoint here worked
and none of them was reachable by clicking anything.

No browser needed: the page is one file, so its markup and its script can be
read directly. That keeps these fast and keeps them running everywhere,
rather than only where Playwright is installed.
"""
import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "src" / "schemagate" / "studio.html"
pytestmark = pytest.mark.skipif(not PAGE.is_file(), reason="page not in this tree")


def page() -> str:
    return PAGE.read_text("utf-8")


@pytest.mark.parametrize("endpoint", [
    "/settings", "/describe", "/apply-descriptions", "/answer", "/select",
])
def test_the_page_actually_calls_each_endpoint(endpoint):
    """An endpoint with no control that reaches it may as well not exist."""
    assert f'"{endpoint}"' in page() or f"API+\"{endpoint}\"" in page()


@pytest.mark.parametrize("control", [
    "mProvider", "mModel", "mKey", "mRerank", "mAnswer", "mSave",
    "catRun", "catApply", "catPrompt", "catReply",
    "answerPane", "answerSql", "answerRows",
])
def test_every_control_the_script_drives_exists_in_the_markup(control):
    """A typo'd id fails silently in the browser -- the handler simply never
    fires and nothing says why."""
    s = page()
    assert f'id="{control}"' in s, f"{control} is used by the script but not in the markup"


def test_hidden_actually_hides():
    """`hidden` loses to any element carrying an explicit display, and both
    .pane and section set one. Without this rule the answer panel sat on the
    page, empty, whether or not answering was switched on -- which is exactly
    how it shipped for a few minutes."""
    assert re.search(r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important", page())


def test_the_key_field_is_a_password_field():
    s = page()
    m = re.search(r'<input id="mKey"[^>]*>', s)
    assert m and 'type="password"' in m.group(0)


def test_the_page_never_renders_the_key_back_into_itself():
    """The endpoint does not return the key; the page must not try to show
    one either, or a screenshot of Studio leaks it."""
    s = page()
    assert "st.api_key" not in s and "settings.api_key" not in s


def test_an_empty_key_field_means_keep_the_existing_key():
    """Retyping a key to change a checkbox is the kind of friction that makes
    people paste keys into shell history instead."""
    s = page()
    assert re.search(r"if\s*\(\s*key\s*\)\s*body\.api_key\s*=\s*key", s)


def test_the_model_panels_are_hidden_without_a_server():
    """The offline build runs the catalog in the browser: no key to hold, and
    nothing to run SQL against. Showing the controls there would promise
    something the page cannot do."""
    s = page()
    assert 'id="modelPanel" hidden' in s and 'id="catPanel" hidden' in s
    assert re.search(r'for \(const id of \["modelPanel","catPanel"\]\) \$\(id\)\.hidden = false',
                     s)


def test_the_template_and_the_shipped_page_agree():
    """`studio/build.py` regenerates `src/schemagate/studio.html` from
    `studio/studio.template.html`, and the template had fallen behind: it had
    no mKey, no answerPane, no catPrompt. Running the documented build command
    silently deleted the entire model panel and took 25 tests with it.

    Nothing warned, because the build succeeds either way -- it is a string
    substitution, and a template missing a whole panel substitutes just fine.
    So the check has to be that every control the shipped page drives also
    exists in the thing that regenerates it.
    """
    tpl = PAGE.parent.parent.parent / "studio" / "studio.template.html"
    if not tpl.is_file():
        pytest.skip("no template in this tree; the page is hand-maintained")

    t = tpl.read_text("utf-8")
    for control in ("modelPanel", "catPanel", "answerPane", "mProvider",
                    "mModel", "mKey", "mRerank", "mAnswer", "mSave",
                    "catRun", "catApply", "catPrompt", "catReply",
                    "answerSql", "answerRows"):
        assert f'id="{control}"' in t, (
            f"{control} is in the shipped page but not in the template that "
            "regenerates it -- running studio/build.py would delete it")


def test_the_template_keeps_the_placeholders_build_py_substitutes():
    """Regenerating the template from the built page is only correct if the
    four injected blobs went back to being placeholders. A template with the
    442 KB of schema JSON baked in would build, and would then be impossible
    to update."""
    tpl = PAGE.parent.parent.parent / "studio" / "studio.template.html"
    if not tpl.is_file():
        pytest.skip("no template in this tree")
    t = tpl.read_text("utf-8")
    for token in ("__BLAKE__", "__SCHEMAGATE_JS__", "__SCHEMAS_JSON__",
                  "__DESCRIPTIONS_JSON__"):
        assert t.count(token) == 1, f"{token} appears {t.count(token)} times"
    assert len(t) < 100_000, "the template has a built blob baked into it"


@pytest.mark.parametrize("control", [
    "cKind", "cUrl", "cWallet", "cAlias", "cWalletPw", "cHost", "cPort",
    "cDatabase", "cUser", "cPassword", "cSchemas", "cGrants", "cValues",
    "cGo", "sqlText", "sqlGo", "sqlRows",
])
def test_the_connect_controls_exist(control):
    assert f'id="{control}"' in page()


@pytest.mark.parametrize("kind", ["url", "jdbc", "wallet", "postgresql",
                                  "oracle", "mssql", "mysql"])
def test_every_connection_kind_is_offered_and_has_fields(kind):
    """A kind in the dropdown with no field map shows an empty form; a field
    map with no option is dead code. They have to match."""
    s = page()
    assert f'<option value="{kind}">' in s
    assert f"{kind}:" in s or f'"{kind}"' in s


def test_credentials_are_cleared_after_a_successful_connect():
    """Not just the URL. A wallet password and a plain password are just as
    much of a problem sitting in the page during a shared screen."""
    s = page()
    assert 'for (const id of ["cUrl","cPassword","cWalletPw"]) $(id).value = "";' in s


def test_password_fields_are_password_inputs():
    s = page()
    for control in ("cPassword", "cWalletPw"):
        import re as _re
        m = _re.search(rf'<input id="{control}"[^>]*>', s)
        assert m and 'type="password"' in m.group(0), control


def test_connecting_replaces_the_demo_rail_in_the_page():
    """The server returns the live catalog's own hints, restrictions and
    questions; the page has to actually use them, hide the "this is a demo"
    intro, and drop the pre-written AI descriptions toggle, which only ever
    applied to the bundled schemas."""
    s = page()
    assert "SCHEMAS[state.schema] = Object.assign(" in s
    assert '$("intro").hidden = true' in s
    assert '$("aiswitch").hidden = true' in s
    assert "renderRail(); renderExamples();" in s


def test_the_command_that_reproduces_a_connection_is_offered():
    """Connecting in a page is how someone tries this; a command is how they
    use it. Reconstructing the URL from memory afterwards is where a wallet
    connection in particular goes wrong."""
    s = page()
    for control in ("recipePanel", "recipeCli", "recipePy", "recipeCopy",
                    "recipeCopyPy"):
        assert f'id="{control}"' in s, control
    assert 'out.recipe.cli' in s and 'out.recipe.python' in s
