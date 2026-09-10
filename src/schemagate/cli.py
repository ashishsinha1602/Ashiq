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
import os
import sys
from typing import List, Optional

from . import __version__
from .catalog import Catalog
from .identity import IdentityError, Principal


def _principal(args) -> Optional[Principal]:
    if not args.principal:
        return None
    try:
        return Principal(args.principal, roles=frozenset(args.role or []))
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


def _provider(args):
    """Build the provider the user asked for. Never picks one on its own.

    There is no default model id here and there will not be: they change
    often enough that a stale default fails with a confusing error months
    after it was written. `--provider auto` still needs `--model`; it only
    chooses which service by whichever API key is in the environment.
    """
    if not args.model:
        sys.exit("schemagate: --model is required with --provider "
                 "(model ids change too often to have a default)")
    from .ai import providers as _p
    classes = {"anthropic": _p.AnthropicProvider, "openai": _p.OpenAIProvider,
               "gemini": _p.GeminiProvider, "oci": _p.OCIGenAIProvider,
               "local": _p.LocalProvider}
    if args.provider == "auto":
        return _p.auto_provider(args.model)
    if args.provider in classes:
        return classes[args.provider](model=args.model)
    sys.exit(f"schemagate: unknown provider {args.provider!r}; use "
             "anthropic, openai, gemini, oci, local or auto")


def _reranker(args):
    """A model to order the shortlist, when asked for and only then."""
    if not getattr(args, "rerank", False):
        return None
    if not getattr(args, "provider", None) or args.provider == "none":
        sys.exit("schemagate: --rerank needs --provider and --model")
    return _provider(args)


def _answer(cat, sel, question, args, url) -> None:
    """Selection is the library's job; this is the step after it.

    The SQL only ever sees the objects `select()` returned, which is what
    makes the identity boundary real: a table the caller may not see is not
    in the prompt, so the model cannot write SQL against it.

    With no API key this prints the prompt to paste into any chat window and
    stops -- the same escape hatch `describe --provider none` offers, because
    the benefit should not require a key.
    """
    from sqlalchemy import create_engine

    from .answer import (UnsafeSQL, format_rows, generate_sql, run_sql,
                         sql_prompt)

    engine = create_engine(url) if isinstance(url, str) else url
    fragment = sel.prompt_fragment()
    dialect = engine.dialect.name

    if args.provider in (None, "none"):
        print("\n-- no --provider given; paste this into any chat, then run the")
        print("-- SQL it gives you with:  schemagate ... --sql \"SELECT ...\"\n")
        print(sql_prompt(question, fragment, dialect))
        return

    try:
        provider = _provider(args)
        sql = generate_sql(provider, question, fragment, dialect)
    except UnsafeSQL as e:
        sys.exit(f"schemagate: refused the generated SQL -- {e}")
    except Exception as e:
        sys.exit(f"schemagate: {type(e).__name__}: {e}")

    # Say who wrote the SQL. `--provider auto` picks by whichever API key is
    # in the environment, so without this the user cannot tell which service
    # just received their schema -- which is exactly the thing to be clear
    # about.
    print(f"\n-- SQL written by {type(provider).__name__.replace('Provider', '')}"
          f" / {args.model}, from {len(sel.objects)} tables")
    print(f"{sql}\n")
    try:
        cols, rows = run_sql(engine, sql, limit=args.limit)
    except UnsafeSQL as e:
        sys.exit(f"schemagate: refused the generated SQL -- {e}")
    print(format_rows(cols, rows))


def _run_sql_only(args, url) -> int:
    """`--sql` runs a query you supply, through the same read-only guard."""
    from sqlalchemy import create_engine

    from .answer import UnsafeSQL, format_rows, run_sql
    try:
        cols, rows = run_sql(create_engine(url), args.sql, limit=args.limit)
    except UnsafeSQL as e:
        sys.exit(f"schemagate: refused that SQL -- {e}")
    print(format_rows(cols, rows))
    return 0


def cmd_demo(args) -> int:
    from .demo_schema import HINTS, create_demo_db

    url = create_demo_db()
    cat = Catalog().bootstrap(url)
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
        sel = cat.select(question, top_k=args.top_k, principal=principal,
                         reranker=_reranker(args))
        _print_selection(sel, args.prompt, args.explain)
        if args.answer:
            _answer(cat, sel, question, args, url)
        print()
    if not args.question:
        print("Try:  schemagate demo \"things we're running out of\"")
        print("      schemagate demo \"salary by employee\" --principal okta:hr --role payroll")
        print("      schemagate demo --prompt      # the DDL your model would receive")
    return 0


def _open(args) -> Catalog:
    cat = Catalog().bootstrap(args.url, include=args.include or None,
                              exclude=args.exclude or None,
                              schemas=getattr(args, "schema", None) or None,
                              sample_values=getattr(args, "values", False))
    if getattr(args, "config", None):
        from . import config as _config
        _config.apply(cat, _config.load(args.config))
    return cat


def cmd_select(args) -> int:
    if getattr(args, "sql", None):
        return _run_sql_only(args, args.url)
    cat = _open(args)
    sel = cat.select(args.question, top_k=args.top_k, principal=_principal(args),
                     expand_fks=not args.no_fk, reranker=_reranker(args))
    _print_selection(sel, args.prompt, args.explain)
    if args.answer:
        _answer(cat, sel, args.question, args, args.url)
    return 0


def cmd_studio(args) -> int:
    from .studio import main as studio_main
    return studio_main(url=args.url, host=args.host, port=args.port,
                       open_browser=not args.no_browser,
                       include=args.include or None, exclude=args.exclude or None,
                       config=args.config)


def cmd_describe(args) -> int:
    """Descriptions without an API key: print a prompt, paste it into any
    chat, save the JSON reply, apply it. Or with a key, call a provider."""
    import json
    from . import config as _config

    cat = _open(args)
    if args.apply:
        with open(args.apply, encoding="utf-8") as fh:
            raw = fh.read().strip()
        # tolerate a reply wrapped in ```json fences
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):raw.rfind("}") + 1]
        try:
            reply = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"schemagate: {args.apply} is not JSON: {e}")
        if not isinstance(reply, dict):
            sys.exit("schemagate: reply must be a JSON object of name -> description")
        known = {d.qname for d in cat.objects()} | {d.name for d in cat.objects()}
        unknown = [k for k in reply if k not in known]
        applied = cat.describe(reply, only_missing=False)
        keep = {k: v for k, v in reply.items() if k in known}
        n = _config.merge_descriptions(args.config, keep) if args.config else applied
        print(f"applied {applied} description(s)"
              + (f"; saved to {args.config}" if args.config else "")
              + (f"; {len(unknown)} name(s) not in catalog: {', '.join(unknown[:5])}" if unknown else ""))
        if not args.config:
            print("tip: add --config catalog.json to keep them for select/studio/MCP")
        return 0 if n or applied else 1
    # `--provider none` is spelled out in the docs as the no-key path, and it
    # used to fail asking for a --model it would never use. Absent and "none"
    # both mean the same thing: print the prompt, do not call anyone.
    if args.provider and args.provider != "none":
        from .ai import SchemaDescriber
        from .ai import providers as _p
        if not args.model:
            sys.exit("schemagate: --model is required with --provider (model ids change; pick one)")
        classes = {"anthropic": _p.AnthropicProvider, "openai": _p.OpenAIProvider,
                   "gemini": _p.GeminiProvider, "oci": _p.OCIGenAIProvider,
                   "local": _p.LocalProvider}
        if args.provider == "auto":
            provider = _p.auto_provider(args.model)
        elif args.provider in classes:
            kwargs = {"model": args.model}
            if args.provider == "oci":
                # On an OCI instance there is no ~/.oci/config: the machine
                # authenticates as itself. OCI_CLI_AUTH is the variable every
                # other OCI tool reads for this, so honour it here too --
                # without it a VM-side `describe --provider oci` fails with
                # ConfigFileNotFound and the catalog is silently left empty.
                auth = os.environ.get("OCI_CLI_AUTH")
                if auth:
                    kwargs["auth"] = auth
            provider = classes[args.provider](**kwargs)   # key from its env var
        else:
            sys.exit(f"schemagate: unknown provider {args.provider!r}; use anthropic, openai, gemini, oci, local or auto")
        describer = SchemaDescriber(provider, cache_path=args.cache)
        n = cat.describe(describer, only_missing=not args.all)
        got = {d.qname: d.description for d in cat.objects() if d.description}
        if args.config:
            _config.merge_descriptions(args.config, got)
        print(f"described {n} object(s)" + (f"; saved to {args.config}" if args.config else ""))
        return 0
    prompt = cat.describe_prompt(only_missing=not args.all)
    if not prompt:
        print("nothing to describe: every object already has a comment or hint", file=sys.stderr)
        return 0
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        print(f"wrote {args.out} -- paste it into any chat, save the JSON reply, then:\n"
              f"  schemagate describe --url ... --apply reply.json --config catalog.json", file=sys.stderr)
    else:
        sys.stdout.write(prompt)
    return 0


def cmd_certify(args) -> int:
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
        p.add_argument("--answer", action="store_true",
                       help="write the SQL and run it, not just pick tables")
        p.add_argument("--provider", metavar="NAME",
                       help="anthropic, openai, gemini, oci, local or auto. "
                            "Omit with --answer to print a prompt to paste")
        p.add_argument("--model", metavar="ID",
                       help="model id for --provider")
        p.add_argument("--limit", type=int, default=50, metavar="N",
                       help="rows to show from --answer (default 50)")
        p.add_argument("--rerank", action="store_true",
                       help="let the model order the shortlist. The maths "
                            "still narrows hundreds of objects to ~20 for "
                            "free; this fixes the ordering, which is where "
                            "matching on names is weakest")
        p.add_argument("--values", action="store_true",
                       help="also read the distinct values of short string "
                            "columns, so the model does not have to guess "
                            "whether a status reads 'denied' or 'DENIED'")

    demo = sub.add_parser("demo", help="run against the bundled schema")
    demo.add_argument("question", nargs="?")
    common(demo)
    demo.set_defaults(func=cmd_demo)

    select = sub.add_parser("select", help="select against your database")
    select.add_argument("question", nargs="?")
    select.add_argument("--url", required=True, help="SQLAlchemy URL")
    select.add_argument("--sql", metavar="SELECT",
                        help="run this read-only SQL instead of selecting; "
                             "for pasting back what a chat window wrote")
    select.add_argument("--include", action="append", metavar="PATTERN")
    select.add_argument("--exclude", action="append", metavar="PATTERN")
    select.add_argument("--schema", action="append", metavar="NAME")
    select.add_argument("--no-fk", action="store_true",
                        help="disable foreign-key expansion")
    select.add_argument("--config", metavar="JSON",
                        help="restrict / hint / describe blocks (see schemagate.config)")
    common(select)
    select.set_defaults(func=cmd_select)

    studio = sub.add_parser("studio", help="open the Studio page on a database")
    studio.add_argument("--url", help="SQLAlchemy URL; omit for the bundled demo")
    studio.add_argument("--host", default="127.0.0.1")
    studio.add_argument("--port", type=int, default=8770)
    studio.add_argument("--no-browser", action="store_true")
    studio.add_argument("--include", action="append", metavar="PATTERN")
    studio.add_argument("--exclude", action="append", metavar="PATTERN")
    studio.add_argument("--config", metavar="JSON",
                        help="restrict / hint / describe blocks (see schemagate.config)")
    studio.set_defaults(func=cmd_studio)

    describe = sub.add_parser(
        "describe",
        help="AI descriptions for your objects -- with a key, or with none",
        description="Without --provider or --apply, prints a prompt: paste it into "
                    "any chat (ChatGPT, Gemini, a local model), save the JSON "
                    "reply, then run again with --apply reply.json. No API key needed.")
    describe.add_argument("--url", required=True, help="SQLAlchemy URL")
    describe.add_argument("--include", action="append", metavar="PATTERN")
    describe.add_argument("--exclude", action="append", metavar="PATTERN")
    describe.add_argument("--schema", action="append", metavar="NAME")
    describe.add_argument("--config", metavar="JSON",
                          help="catalog config to read hints from and save descriptions into")
    describe.add_argument("--out", metavar="FILE", help="write the prompt here instead of stdout")
    describe.add_argument("--apply", metavar="REPLY.json", help="the chat's JSON reply to apply")
    describe.add_argument("--all", action="store_true",
                          help="include objects that already have a comment or hint")
    describe.add_argument("--provider", metavar="NAME",
                          help="anthropic | openai | gemini | oci | local | auto -- key from the environment; "
                               "oci uses ~/.oci/config, local needs no key at all")
    describe.add_argument("--model", metavar="ID", help="model id for --provider")
    describe.add_argument("--cache", metavar="FILE", help="description cache for --provider")
    describe.set_defaults(func=cmd_describe)

    certify = sub.add_parser("certify", help="end-to-end check on a real engine")
    certify.add_argument("url")
    certify.set_defaults(func=cmd_certify)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    # Bare `schemagate` opens the browser rather than printing usage. Someone
    # who has just installed this has a database and a question, not a
    # subcommand in mind, and an argparse usage dump is the least useful thing
    # to hand them at that moment.
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        argv = ["studio"]
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
