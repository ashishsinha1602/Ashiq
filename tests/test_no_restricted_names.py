"""The generic-schema constraint, enforced by the test suite.

This project ships on the condition that it contains no employer-specific
schema, table or domain names. ``scripts/check_names.py`` enforced that in
CI only, which meant a local ``pytest`` run could pass on a tree that must
not be published. Running the guard as a test closes that gap: it now fails
the same command everyone already runs.

The term list deliberately lives OUTSIDE this repository -- in ``.namecheck``
(gitignored) or the ``NAMECHECK_TERMS`` environment variable -- because
publishing the list would broadcast the very names it exists to keep out,
and even generic industry vocabulary would fingerprint the domain this was
written near. With no terms configured the term check is skipped, so forks
and outside contributors still get a green suite; the structural checks
below always run, since they need no secret list.
"""
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".pytest_cache", ".venv", ".hypothesis",
             ".mypy_cache", ".ruff_cache", "node_modules"}
SKIP_SUFFIX = {".pyc", ".gz", ".whl", ".png", ".jpg", ".gif", ".so"}

FIXTURES = ["schema_fixture", "schema_fixture_health"]


def source_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name == ".namecheck":
            continue
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_check_names_guard_passes():
    """Whatever terms are configured locally or in CI, the tree is clean."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_names.py")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_is_wired_up_and_actually_fails_on_a_hit():
    """A guard that cannot fail is not a guard.

    Feed the script a term that is certainly present and assert it rejects
    the tree, so a broken guard is never mistaken for a clean one.
    """
    import os
    env = dict(os.environ, NAMECHECK_TERMS="schemagate")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_names.py")],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 1, "check_names did not fail on a planted term"
    assert "FAIL" in result.stdout


def test_no_personal_contact_details_in_metadata():
    """An email in packaging metadata becomes permanently public on PyPI."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", pyproject), \
        "an email address would be published in the PyPI metadata"


#: where each fixture's real source lives -- the commerce schema ships in the
#: package so `schemagate demo` can use it, and tests/schema_fixture.py re-exports it
FIXTURE_SOURCES = {
    "schema_fixture": ROOT / "src" / "schemagate" / "demo_schema.py",
    "schema_fixture_health": ROOT / "tests" / "schema_fixture_health.py",
    "schema_fixture_complex": ROOT / "tests" / "schema_fixture_complex.py",
}


@pytest.mark.parametrize("fixture", sorted(FIXTURE_SOURCES))
def test_fixture_schemas_declare_themselves_synthetic(fixture):
    """Every benchmark schema must be invented, not copied from anywhere."""
    text = FIXTURE_SOURCES[fixture].read_text(encoding="utf-8").lower()
    assert "synthetic" in text or "invented" in text, \
        f"{fixture} must state that it is synthetic"


def test_fixtures_come_from_unrelated_domains():
    """Two schemas only prove generality if they share no vocabulary."""
    import importlib

    words = []
    for fixture in FIXTURES:
        mod = importlib.import_module(fixture)
        names = re.findall(r"CREATE (?:TABLE|VIEW) (\w+)", mod.DDL)
        words.append({p for n in names for p in n.lower().split("_")
                      if len(p) > 2 and p not in {"tbl", "ref", "org"}})
    shared = words[0] & words[1]
    smaller = min(len(words[0]), len(words[1]))
    overlap = len(shared) / smaller
    # A couple of structural words ("line", "rate") will always coincide.
    # What must not happen is two schemas that are the same domain twice.
    assert overlap < 0.10, (
        f"the fixtures share {overlap:.0%} of their vocabulary "
        f"({sorted(shared)}); they are not independent domains"
    )


def test_no_absolute_paths_from_the_authoring_machine():
    """Local paths leak usernames and directory layout."""
    self_path = pathlib.Path(__file__).resolve()
    pattern = re.compile("/home/|/Users/|" + re.escape("C:\\Users"))
    leaks = []
    for path, text in source_files():
        if path.resolve() == self_path:   # this file defines the pattern
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                leaks.append(f"{path.relative_to(ROOT)}:{i}")
    assert not leaks, "machine-local paths found in: " + ", ".join(leaks)
