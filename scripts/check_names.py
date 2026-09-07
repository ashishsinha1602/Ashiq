#!/usr/bin/env python3
"""Fail the build if employer-specific identifiers appear anywhere in the tree.

Terms are read from `.namecheck` (one per line, case-insensitive), which is
gitignored on purpose: publishing the list would broadcast exactly the names
it exists to keep out. Keep your copy locally and in the CI secret
NAMECHECK_TERMS (newline-separated).

    python scripts/check_names.py
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "dist", "build", ".pytest_cache", ".venv"}
SKIP_SUFFIX = {".pyc", ".gz", ".whl", ".png", ".jpg"}


def terms() -> list[str]:
    out = []
    env = os.environ.get("NAMECHECK_TERMS", "")
    if env.strip():
        out += re.split(r"[\n,]", env)   # one per line, or comma-separated
    f = ROOT / ".namecheck"
    if f.exists():
        out += f.read_text().splitlines()
    return [t.strip() for t in out if t.strip() and not t.startswith("#")]


def main() -> int:
    pats = terms()
    if not pats:
        print("check_names: no terms configured, skipping")
        return 0
    rx = re.compile("|".join(re.escape(p) for p in pats), re.IGNORECASE)
    bad = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name == ".namecheck":
            continue
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                # print the location, never the matched term
                bad.append(f"  {path.relative_to(ROOT)}:{i}")
    if bad:
        print(f"check_names: FAIL -- restricted identifier in {len(bad)} place(s):")
        print("\n".join(bad))
        return 1
    print(f"check_names: clean ({len(pats)} terms checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
