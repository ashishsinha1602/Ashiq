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


def _open(args) -> Catalog:
    cat = Catalog().bootstrap(args.url, include=args.include or None,
                              exclude=args.exclude or None,
                              schemas=getattr(args, "schema", None) or None)
    if getattr(args, "config", None):
        from . import config as _config
        _config.apply(cat, _config.load(args.config))
    return cat


def cmd_select(args) -> int:
    cat = _open(args)
    sel = cat.select(args.question, top_k=args.top_k, principal=_principal(args),
                     expand_fks=not args.no_fk)
    _print_selection(sel, args.prompt, args.explain)
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
    if args.provider:
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
                    "any chat (Claude, ChatGPT, Gemini, a local model), save the JSON "
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
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
