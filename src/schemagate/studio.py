"""``schemagate studio``: the Studio page, served locally against your database.

    schemagate studio --url postgresql://localhost/app
    schemagate studio                       # bundled demo schema

Opens http://127.0.0.1:8770. The page is the same one published as the
public demo; the difference is that selection runs in this Python process
against your real catalog instead of in the browser against bundled
schemas. Nothing leaves your machine: the server binds to localhost and
makes no outbound requests.

Standard library only -- no web framework to install for a local tool.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from .catalog import Catalog
from .identity import IdentityError, Principal

_PAGE = pathlib.Path(__file__).with_name("studio.html")


def _estimate_tokens(text: str) -> int:
    """Same estimator as tests/bench.py, so the numbers match the README."""
    import re
    n = 0
    for piece in re.findall(r"[A-Za-z]+|[0-9]|[^\sA-Za-z0-9]", text):
        n += max(1, round(len(piece) / 4)) if piece.isalpha() else 1
    return n


#: Value sampling inside a connect. Deliberately shorter than the library
#: default: at this point someone is watching a button, and a catalog that
#: arrives now beats one with more values in it later. Everything sampled
#: before this is kept.
_CONNECT_SAMPLE_BUDGET = 10.0


class StudioState:
    """One catalog, one optional described twin, served by the handlers."""

    def __init__(self, catalog: Catalog, title: str, blurb: str,
                 questions: Optional[List[str]] = None, engine=None):
        self.catalog = catalog
        self.title = title
        self.blurb = blurb
        self.questions = questions or []
        self.described: Optional[Catalog] = None
        #: Needed to run the SQL a model writes. Without it the page can still
        #: select and still hand you a prompt to paste -- it just cannot show
        #: you rows.
        self.engine = engine
        self.settings: Dict[str, Any] = {}
        self.provider_error: Optional[str] = None
        #: Connecting to a database is the one thing on this page that reaches
        #: outside the process, so it is refused unless the person who started
        #: the server said otherwise. A Studio bound to 0.0.0.0 with this open
        #: is a URL box on the internet that will connect anywhere and read a
        #: schema back -- including to hosts only this machine can see.
        self.allow_connect = False
        self.restrict_from_grants = False
        self.sample_values = False
        #: True only for the bundled sample schema. An engine alone cannot say
        #: what it points at -- the demo has one too -- and the header calling
        #: invented tables "your database" is the confusion this exists to
        #: stop.
        self.is_demo = False

    def schemas_json(self) -> Dict[str, Any]:
        """The shape the page expects: docs are not needed server-side, but
        hints, restrictions and example questions drive the rail."""
        cat = self.catalog
        return {"live": {
            "title": self.title, "blurb": self.blurb,
            "docs": [],
            "hints": {d.name: d.hint for d in cat._docs.values() if d.hint},
            "restrict": {d.name: d.roles for d in cat._docs.values() if d.roles},
            "questions": self.questions, "golden": {},
        }}

    #: Set from the page rather than the command line. A key typed into a
    #: form does not end up in shell history, in a screen share of a terminal,
    #: or in a screenshot of a command -- which is where the last three keys
    #: in this project's history leaked from. It is held in memory for the
    #: life of the process and never written to disk.
    def set_settings(self, body: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"provider", "model", "api_key", "rerank", "answer"}
        self.settings.update({k: v for k, v in body.items() if k in allowed})
        return self.describe_settings()

    def describe_settings(self) -> Dict[str, Any]:
        """Never returns the key itself -- only whether one is set."""
        st = self.settings
        return {"provider": st.get("provider") or "", "model": st.get("model") or "",
                "has_key": bool(st.get("api_key")), "rerank": bool(st.get("rerank")),
                "answer": bool(st.get("answer"))}

    def _provider(self):
        """The configured provider, or None -- never an exception.

        A key typed into a form is wrong more often than one exported in a
        shell, and the page must survive that. A provider that cannot be
        built degrades to "no provider": selection still works offline,
        reranking is skipped, and answering falls back to a prompt you can
        paste. The reason is kept so the page can say what went wrong instead
        of failing silently.
        """
        st = self.settings
        name, model, key = st.get("provider"), st.get("model"), st.get("api_key")
        self.provider_error = None
        if not name or name == "none" or not model:
            return None
        try:
            from .ai import providers as _p
            classes = {"anthropic": _p.AnthropicProvider, "openai": _p.OpenAIProvider,
                       "gemini": _p.GeminiProvider, "oci": _p.OCIGenAIProvider,
                       "local": _p.LocalProvider}
            cls = classes.get(name)
            if cls is None:
                self.provider_error = f"unknown provider {name!r}"
                return None
            if cls is _p.LocalProvider:
                return cls(model=model)
            if cls is _p.OCIGenAIProvider:
                return cls(model=model, compartment_id=key) if key else cls(model=model)
            return cls(model=model, api_key=key) if key else cls(model=model)
        except Exception as e:                            # noqa: BLE001
            self.provider_error = f"{type(e).__name__}: {e}"
            return None

    def answer(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Question in, rows out -- the step after selection.

        The SQL only ever sees the objects select() returned, so a table this
        principal cannot see is not in the prompt and cannot be queried.
        """
        from .answer import (UnsafeSQL, generate_sql, run_sql, sql_prompt)

        picked = self.select(body)
        if "error" in picked:
            return picked
        question = str(body.get("question") or "").strip()[:2000]
        fragment = picked["ddl"]
        engine = getattr(self, "engine", None)
        dialect = engine.dialect.name if engine is not None else ""

        provider = self._provider()
        if provider is None:
            picked["paste_prompt"] = sql_prompt(question, fragment, dialect)
            if self.provider_error:
                picked["answer_error"] = self.provider_error
            return picked
        try:
            picked["sql"] = generate_sql(provider, question, fragment, dialect)
        except UnsafeSQL as e:
            picked["answer_error"] = f"refused the generated SQL: {e}"
            return picked
        except Exception as e:                            # noqa: BLE001
            picked["answer_error"] = f"{type(e).__name__}: {e}"
            return picked
        if engine is None:
            picked["answer_error"] = "no engine to run against"
            return picked
        try:
            cols, rows = run_sql(engine, picked["sql"], limit=50)
        except Exception as e:                            # noqa: BLE001
            picked["answer_error"] = f"{type(e).__name__}: {e}"
            return picked
        picked["columns"] = list(cols)
        picked["rows"] = [[None if v is None else str(v) for v in r] for r in rows]
        return picked

    def describe(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Catalogue the live database with the configured model.

        This is the step that actually raises accuracy, and it is the one
        people skip because it used to mean a command line and a JSON file.
        Descriptions are what turn "how much do we pay people" into
        `hr_compensation`: matching identifiers cannot do it, because the
        question and the table share no words.

        Written once and kept in memory for this session. Only metadata is
        sent -- names, types, comments, foreign keys -- never rows, which is
        the same promise `describe_prompt` has always made.

        With no provider configured this returns the prompt to paste into any
        chat instead, so the benefit does not require a key.
        """
        cat = self.catalog
        only_missing = bool(body.get("only_missing", True))
        provider = self._provider()
        if provider is None:
            prompt = cat.describe_prompt(only_missing=only_missing)
            return {"paste_prompt": prompt,
                    "pending": len([d for d in cat._docs.values()
                                    if not (d.description or d.hint)]),
                    "error": self.provider_error} if prompt else {
                    "paste_prompt": "", "pending": 0, "error": self.provider_error}

        from .ai import SchemaDescriber
        try:
            written = cat.describe(SchemaDescriber(provider),
                                   only_missing=only_missing)
        except Exception as e:                            # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        cat.index()
        sample = [{"name": d.qname, "description": d.description}
                  for d in list(cat._docs.values()) if d.description][:8]
        return {"written": written, "objects": len(cat._docs), "sample": sample}

    def apply_descriptions(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Take back a JSON reply pasted from a chat window."""
        raw = body.get("descriptions")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("```"):        # a reply wrapped in fences
                raw = raw.strip("`")
                raw = raw[raw.find("{"):raw.rfind("}") + 1]
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                return {"error": f"that is not JSON: {e}"}
        if not isinstance(raw, dict):
            return {"error": "expected a JSON object of name -> description"}
        written = self.catalog.describe(raw, only_missing=False)
        self.catalog.index()
        return {"written": written, "objects": len(self.catalog._docs)}

    def connect(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Reflect a database into this running Studio.

        Refused unless the server was started with --allow-remote-connect.
        Without that, anyone who can reach the page can hand it a URL and have
        the server connect on their behalf -- to a host only the server can
        see, with whatever credentials they put in the string. The page is a
        local tool by default and this keeps it one.
        """
        if not self.allow_connect:
            return {"error": "connecting from the page is off. Restart with "
                             "`schemagate studio --allow-remote-connect`, or "
                             "pass --url when you start it."}
        from sqlalchemy import create_engine

        from .catalog import Catalog
        from .connect import (
            ConnectError,
            driver_hint,
            recipe,
            resolve,
            safe_error,
            with_timeout,
        )

        # A SQLAlchemy URL, a JDBC string, or the wallet fields -- whichever
        # the person actually has. `connect_args` is not optional: an
        # Autonomous Database has no URL to speak of and the whole connection
        # lives there.
        try:
            url, connect_args = resolve(body if body.get("kind") else
                                        str(body.get("url") or ""))
        except ConnectError as e:
            return {"error": str(e)}

        schemas = [s for s in (body.get("schemas") or []) if s] or None
        want_grants = bool(body.get("restrict_from_grants", self.restrict_from_grants))
        want_values = bool(body.get("sample_values", self.sample_values))
        try:
            # Bound the connect. Without this a host that drops packets --
            # a firewall in front of 1522, most often -- never returns, and
            # the page sits on "Connecting..." with nothing to show. A
            # timeout turns that silence into DPY-6005, which is an answer.
            engine = create_engine(
                url, connect_args=with_timeout(url, connect_args))
            # Connecting is meant to be quick: reflect the schema and get out
            # of the way. Reading values is the only part that touches rows,
            # and it is the part whose cost is set by the network rather than
            # by the schema -- so inside a connect it gets a short leash.
            # Cataloguing with a model happens afterwards, on demand, and is
            # where the minutes are supposed to be spent.
            cat = Catalog(name="studio").bootstrap(
                engine, schemas=schemas, sample_values=want_values,
                sample_budget=_CONNECT_SAMPLE_BUDGET)
        except ModuleNotFoundError as e:
            hint = driver_hint(url)
            return {"error": f"driver not installed ({e.name})" +
                             (f" -- pip install '{hint}'" if hint else "")}
        except Exception as e:                            # noqa: BLE001
            # Driver messages quote the connect string they were handed, and
            # that carries a password -- which is why this used to return the
            # exception class alone. But "OperationalError" cannot tell a
            # blocked port from a wrong password from an alias that does not
            # resolve, and those need completely different things done about
            # them. So: the driver's code and message, with the secrets this
            # request supplied masked out of it.
            secrets = [str(connect_args.get("password") or ""),
                       str(connect_args.get("wallet_password") or ""),
                       str(body.get("password") or ""),
                       str(body.get("wallet_password") or "")]
            return {"error": "could not connect -- " + safe_error(e, *secrets)}

        report = None
        if want_grants:
            from .grants import restrict_from_grants
            try:
                rep = restrict_from_grants(cat, engine, report=True)
                report = {"dialect": rep.dialect, "seen": rep.objects_seen,
                          "restricted": rep.objects_restricted,
                          "public": rep.objects_public,
                          "unmatched": len(rep.objects_unmatched),
                          "roles_expanded": rep.roles_expanded,
                          "warnings": rep.warnings}
            except NotImplementedError as e:
                report = {"error": str(e)}
            except Exception as e:                        # noqa: BLE001
                report = {"error": f"{type(e).__name__}: {e}"}

        cat.index()
        self.catalog = cat
        self.engine = engine
        self.is_demo = False
        self.title = "Your database"
        self.blurb = (f"{len(cat._docs)} objects reflected from "
                      f"{engine.dialect.name}. Nothing is catalogued yet.")
        # The page was built around the bundled demo, whose hints, restricted
        # object and example questions are baked into it. None of that belongs
        # to the database just connected, and leaving it on screen is worse
        # than cosmetic: the rail would claim a restriction this database does
        # not have. Hand back the live catalog's own -- empty, for a database
        # nobody has catalogued yet -- so the page can replace them.
        self.questions = []
        return {"objects": len(cat._docs), "dialect": engine.dialect.name,
                "schemas": sorted({d.schema for d in cat._docs.values() if d.schema}),
                "grants": report, "values": want_values,
                "demo": False, "schema": self.schemas_json()["live"],
                # The command that reproduces this, handed over at the moment
                # it works. Connecting in a page is how someone tries this; a
                # command is how they use it, and reconstructing the URL from
                # memory afterwards is where a wallet connection goes wrong.
                # Passwords are placeholders -- a command with a live one in
                # it ends up in a screenshot and in shell history.
                "recipe": recipe(url, connect_args, schemas,
                                 want_grants, want_values)}

    def run_sql(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Run one read-only statement, through the same guard as `--sql`."""
        from .answer import UnsafeSQL, run_sql as _run

        if self.engine is None:
            return {"error": "no database connected"}
        try:
            cols, rows = _run(self.engine, str(body.get("sql") or ""),
                              limit=int(body.get("limit") or 50))
        except UnsafeSQL as e:
            return {"error": f"refused: {e}"}
        except Exception as e:                            # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        return {"columns": list(cols),
                "rows": [[None if v is None else str(v) for v in r] for r in rows]}

    def select(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cat = self.catalog
        question = str(body.get("question") or "").strip()[:2000]
        if not question:
            return {"error": "question is empty"}
        top_k = max(1, min(int(body.get("top_k") or 6), 50))
        who = None
        if body.get("principal"):
            who = Principal(str(body["principal"]),
                            roles=frozenset(str(r) for r in body.get("roles") or []))
        reranker = self._provider() if self.settings.get("rerank") else None
        sel = cat.select(question, top_k=top_k, principal=who, reranker=reranker)
        visible = [d for d in cat._docs.values() if cat._visible(d, who)]
        hidden = [d.qname for d in cat._docs.values() if not cat._visible(d, who)]
        full = "\n\n".join(d.render_ddl() for d in visible)
        shadows = cat.shadows()
        return {
            "objects": [{"name": h.doc.qname, "kind": h.doc.kind,
                         "columns": len(h.doc.columns), "score": h.score,
                         "reason": h.reason, "shadow": h.doc.qname in shadows}
                        for h in sel.hits],
            "total": sel.total_objects, "ddl": sel.prompt_fragment(),
            "tokens": _estimate_tokens(sel.prompt_fragment()),
            "full_tokens": _estimate_tokens(full),
            "hidden": hidden, "shadows": sorted(shadows.items()),
        }


def _handler(state: StudioState):
    page = _PAGE.read_text("utf-8")
    # The live catalog goes in front of the bundled sample rather than over
    # the top of it. Replacing it outright left one tab, so a Studio with
    # nothing connected had only invented tables to show and looked like it
    # was already pointed at something. Two tabs keep them apart: "Your
    # database" is this process talking to a real engine, "Demo schema" is the
    # same sample the public page runs, entirely in the browser.
    start = page.index('<script id="schemas" type="application/json">') + len('<script id="schemas" type="application/json">')
    end = page.index("</script>", start)
    bundled = json.loads(page[start:end])
    payload = dict(state.schemas_json())
    demo = bundled.get("commerce")
    if demo is not None:
        demo = dict(demo)
        demo["title"] = "Demo schema"
        payload["commerce"] = demo
    page = (page[:start] + json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
            + page[end:])
    page = page.replace("<script>\n(function(){", '<script>window.SCHEMAGATE_API="/api";\n(function(){', 1)
    body = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "schemagate-studio"

        def log_message(self, fmt, *args):      # quiet by default
            if os.environ.get("SCHEMAGATE_STUDIO_LOG"):
                super().log_message(fmt, *args)

        def _json(self, code: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/health":
                self._json(200, {"status": "ok", "objects": len(state.catalog._docs)})
            elif self.path == "/api/settings":
                out = state.describe_settings()
                out["allow_connect"] = state.allow_connect
                out["connected"] = state.engine is not None and not state.is_demo
                out["demo"] = state.is_demo
                self._json(200, out)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/api/select", "/api/answer", "/api/settings",
                                 "/api/describe", "/api/apply-descriptions",
                                 "/api/connect", "/api/run-sql"):
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    return self._json(400, {"error": "body must be a JSON object"})
                if self.path == "/api/settings":
                    return self._json(200, state.set_settings(payload))
                if self.path == "/api/answer":
                    return self._json(200, state.answer(payload))
                if self.path == "/api/describe":
                    return self._json(200, state.describe(payload))
                if self.path == "/api/apply-descriptions":
                    return self._json(200, state.apply_descriptions(payload))
                if self.path == "/api/connect":
                    return self._json(200, state.connect(payload))
                if self.path == "/api/run-sql":
                    return self._json(200, state.run_sql(payload))
                return self._json(200, state.select(payload))
            except IdentityError as e:
                return self._json(400, {"error": str(e)})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": f"bad request: {e}"})
            except Exception as e:                       # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


class _Server(ThreadingHTTPServer):
    #: the stdlib default backlog is 5, which drops connections the moment a
    #: page fires a handful of requests at once
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


def serve(state: StudioState, host: str = "127.0.0.1", port: int = 8770,
          open_browser: bool = True) -> ThreadingHTTPServer:
    server = _Server((host, port), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Kept on the server so main() can wait on it in short, interruptible
    # slices. Returning it instead would change a signature the tests and the
    # OCI stack both call.
    server.serve_thread = thread                          # type: ignore[attr-defined]
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"schemagate studio: {url}   ({len(state.catalog._docs)} objects)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                                # noqa: BLE001
            pass
    return server


def main(url: Optional[str] = None, host: str = "127.0.0.1", port: int = 8770,
         open_browser: bool = True, include=None, exclude=None,
         config: Optional[str] = None, restrict_from_grants: bool = False,
         sample_values: bool = False,
         allow_remote_connect: Optional[bool] = None,
         demo: bool = False) -> int:
    engine = None
    if url:
        from sqlalchemy import create_engine
        engine = create_engine(url)
        cat = Catalog(name="studio").bootstrap(engine, include=include,
                                               exclude=exclude,
                                               sample_values=sample_values)
        if restrict_from_grants:
            from .grants import restrict_from_grants as _rfg
            print(_rfg(cat, engine, report=True), file=sys.stderr)
        if config:
            from . import config as _config
            _config.apply(cat, _config.load(config))
        title = "Your database"
        blurb = f"{len(cat._docs)} objects reflected. Hints, restrictions and descriptions come from --config."
        questions: List[str] = []
    elif demo:
        from sqlalchemy import create_engine
        from .demo_schema import GOLDEN, HINTS, create_demo_db
        demo_url = create_demo_db()
        engine = create_engine(demo_url)
        cat = Catalog(name="studio").bootstrap(engine)
        for table, text in HINTS.items():
            cat.hint(table, text)
        cat.restrict("hr_compensation", ["payroll"])
        title, blurb = "Demo schema", "42 objects, invented. Connect a database to replace them."
        questions = [q for q, _ in GOLDEN]
    else:
        # Nothing was asked for, so nothing is loaded. The page opens on its
        # Connect panel rather than on a schema nobody asked to see: a demo
        # standing in for the user's database is how someone ends up reading
        # invented table names as their own, and the header saying "connected"
        # over borrowed tables is worse than an empty page. `--demo` still
        # brings the sample schema up for anyone who wants a look first.
        cat = Catalog(name="studio")
        cat.index()
        # Named for what the tab is for, not for what it currently holds:
        # "No database" sits next to "Demo schema" as though it were a second
        # sample. The blurb carries the state instead.
        title = "Your database"
        blurb = "Nothing connected yet. Use the Database panel on the left."
        questions = []
    state = StudioState(cat, title, blurb, questions, engine=engine)
    state.is_demo = bool(demo and not url)
    # On loopback, connecting needs no permission: the only person who can
    # reach the page is someone already sitting at a shell on this machine,
    # and they can open a database without asking the Studio to do it. The
    # guard exists for the other case -- a Studio on 0.0.0.0 is a URL box
    # anyone on the network can use to make this server connect to hosts only
    # it can see. So the default follows the bind address rather than making
    # every local user pass a flag to get the feature the page is for.
    if allow_remote_connect is None:
        allow_remote_connect = host in ("127.0.0.1", "::1", "localhost")
    state.allow_connect = bool(allow_remote_connect)
    state.restrict_from_grants = restrict_from_grants
    state.sample_values = sample_values
    server = serve(state, host, port, open_browser)

    # Wait in short slices rather than one long one. Python runs a signal
    # handler only between bytecodes in the main thread, and on Windows there
    # is no EINTR to break a wait early -- so `Event().wait(3600)` meant Ctrl+C
    # sat unhandled for up to an hour and the only way out was killing
    # python.exe. On Linux and macOS the wait is interrupted immediately, which
    # is why this survived: it was never broken on the machines it was written
    # on.
    thread = getattr(server, "serve_thread", None)
    try:
        while thread is not None and thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        # Without this the listening socket stays open until the process
        # exits, so an immediate restart fails with "address already in use".
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
