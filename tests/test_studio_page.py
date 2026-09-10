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
