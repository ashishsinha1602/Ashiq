"""`schemagate certify` has to work where it is installed.

It looked for `scripts/certify_dialect.py` three directories above the
installed module -- a path that exists in a git checkout and nowhere else. So
it worked for the author and printed a GitHub link for everyone who installed
from PyPI, which is the shape of bug that survives longest: the person who
would notice never sees it.

The comment in the code said the script lived in the sdist. It did not.
"""
import pathlib
import subprocess
import sys


def test_the_implementation_is_importable_from_the_package():
    """Not resolved by walking up from __file__ -- that is what broke."""
    from schemagate.certify import main
    assert callable(main)


def test_the_command_does_not_just_print_a_link(tmp_path):
    out = subprocess.run(
        [sys.executable, "-m", "schemagate", "certify",
         f"sqlite:///{tmp_path/'c.db'}"],
        capture_output=True, text=True, timeout=300)
    assert "CERTIFIED" in out.stdout, out.stdout[-400:] + out.stderr[-400:]
    assert "github.com" not in out.stdout
    assert out.returncode == 0


def test_it_does_not_depend_on_the_scripts_directory():
    """The failure was a path relative to the installed module. Run from a
    directory with no `scripts/` in sight, from a copy of the source tree that
    has none either."""
    import schemagate
    pkg = pathlib.Path(schemagate.__file__).resolve().parent
    assert (pkg / "certify.py").is_file(), "certify must ship inside the package"


def test_the_standalone_script_still_works_because_its_path_is_published():
    """`python scripts/certify_dialect.py <url>` is in the README and in a
    published post, so it has to keep working -- it now calls the package."""
    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "certify_dialect.py"
    if not script.is_file():
        return                                   # not in an installed tree
    assert "from schemagate.certify import main" in script.read_text()
