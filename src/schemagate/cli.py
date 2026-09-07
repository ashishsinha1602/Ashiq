"""Command line: try schemagate in thirty seconds without writing any code.

    schemagate demo                                  # bundled schema, no database
    schemagate demo "things we're running out of"    # your own question against it
    schemagate select "revenue by month" --url postgresql://localhost/app
    schemagate select "salary by employee" --url ... --principal okta:jdoe --role finance
    schemagate studio --url postgresql://localhost/app   # the Studio page, locally
    schemagate certify postgresql://user:pw@host/db  # end-to-end check on your engine

Everything printed is what your application would get from ``Catalog``.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .catalog import Catalog
from .identity import IdentityError, Principal


def _principal(args) -> Optional[Principal]:
    if not args.principal:
        return None
    try:
        return Principal(args.principal, roles=set(args.role or []))
    except IdentityError as e:
        sys.exit(f"schemagate: {e}")


def _print_selection(sel, show_prompt: bool, show_explain: bool) -> None:
    print(f"{len(sel)} of {sel.total_objects} objects selected")
    print()
    if show_explain:
        print(sel.explain())
        print()
    if show_prompt:
        print(sel.prompt_fragment())
    else:
        for name in sel.table_names:
            print(f"  {name}")


def cmd_demo(args) -> int:
    from .demo_schema import GOLDEN_PARAPHRASE, HINTS, create_demo_db

    cat = Catalog().bootstrap(create_demo_db())
    for table, text in HINTS.items():
        cat.hint(table, text)
    cat.restrict("hr_compensation", ["payroll"])

    questions = [args.question] if args.question else [
        "total revenue by month last year",
        "which customers owe us money",
        "salary and pay grade by employee",
    ]
    principal = _principal(args) or Principal("demo:analyst")
    print(f"schemagate {__version__} -- demo schema, {len(cat._docs)} objects, "
          f"caller {principal.subject} roles={sorted(principal.roles) or '-'}")
    print("hr_compensation is restricted to the 'payroll' role.\n")
    for question in questions:
        print(f"> {question}")
        sel = cat.select(question, top_k=args.top_k, principal=principal)
        _print_selection(sel, args.prompt, args.explain)
        print()
    if not args.question:
        print("Try:  schemagate demo \"things we're running out of\"")
        print("      schemagate demo \"salary by employee\" --principal okta:hr --role payroll")
        print("      schemagate demo --prompt      # the DDL your model would receive")
    return 0


def cmd_select(args) -> int:
    cat = Catalog().bootstrap(args.url, include=args.include or None,
                              exclude=args.exclude or None,
                              schemas=args.schema or None)
    sel = cat.select(args.question, top_k=args.top_k, principal=_principal(args),
                     expand_fks=not args.no_fk)
    _print_selection(sel, args.prompt, args.explain)
    return 0


def cmd_studio(args) -> int:
    from .studio import main as studio_main
    return studio_main(url=args.url, host=args.host, port=args.port,
                       open_browser=not args.no_browser,
                       include=args.include or None, exclude=args.exclude or None)


def cmd_certify(args) -> int:
    import importlib.util
    import pathlib
    import subprocess

    # the script lives in the sdist, not the wheel, so locate it or fall back
    here = pathlib.Path(__file__).resolve()
    candidates = [here.parents[2] / "scripts" / "certify_dialect.py"]
    for path in candidates:
        if path.exists():
            return subprocess.call([sys.executable, str(path), args.url])
    sys.exit("schemagate: certify_dialect.py is in the source repository:\n"
             "  https://github.com/ashishsinha1602/schemagate/blob/main/scripts/certify_dialect.py")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schemagate",
        description="Identity-scoped schema selection for NL2SQL.")
    parser.add_argument("--version", action="version", version=f"schemagate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--top-k", type=int, default=6, metavar="N")
        p.add_argument("--principal", metavar="SOURCE:ID",
                       help="caller identity, e.g. okta:jdoe")
        p.add_argument("--role", action="append", metavar="ROLE",
                       help="repeatable")
        p.add_argument("--prompt", action="store_true",
                       help="print the DDL fragment instead of names")
        p.add_argument("--explain", action="store_true",
                       help="show score and reason per object")

    demo = sub.add_parser("demo", help="run against the bundled schema")
    demo.add_argument("question", nargs="?")
    common(demo)
    demo.set_defaults(func=cmd_demo)

    select = sub.add_parser("select", help="select against your database")
    select.add_argument("question")
    select.add_argument("--url", required=True, help="SQLAlchemy URL")
    select.add_argument("--include", action="append", metavar="PATTERN")
    select.add_argument("--exclude", action="append", metavar="PATTERN")
    select.add_argument("--schema", action="append", metavar="NAME")
    select.add_argument("--no-fk", action="store_true",
                        help="disable foreign-key expansion")
    common(select)
    select.set_defaults(func=cmd_select)

    studio = sub.add_parser("studio", help="open the Studio page on a database")
    studio.add_argument("--url", help="SQLAlchemy URL; omit for the bundled demo")
    studio.add_argument("--host", default="127.0.0.1")
    studio.add_argument("--port", type=int, default=8770)
    studio.add_argument("--no-browser", action="store_true")
    studio.add_argument("--include", action="append", metavar="PATTERN")
    studio.add_argument("--exclude", action="append", metavar="PATTERN")
    studio.set_defaults(func=cmd_studio)

    certify = sub.add_parser("certify", help="end-to-end check on a real engine")
    certify.add_argument("url")
    certify.set_defaults(func=cmd_certify)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
