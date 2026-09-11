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
    # swap the bundled schemas for the live one and point the page at the API
    start = page.index('<script id="schemas" type="application/json">') + len('<script id="schemas" type="application/json">')
    end = page.index("</script>", start)
    page = (page[:start] + json.dumps(state.schemas_json(), ensure_ascii=False).replace("</", "<\\/")
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
                self._json(200, state.describe_settings())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/api/select", "/api/answer", "/api/settings",
                                 "/api/describe", "/api/apply-descriptions"):
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
         config: Optional[str] = None) -> int:
    engine = None
    if url:
        from sqlalchemy import create_engine
        engine = create_engine(url)
        cat = Catalog(name="studio").bootstrap(engine, include=include, exclude=exclude)
        if config:
            from . import config as _config
            _config.apply(cat, _config.load(config))
        title = "Your database"
        blurb = f"{len(cat._docs)} objects reflected. Hints, restrictions and descriptions come from --config."
        questions: List[str] = []
    else:
        from sqlalchemy import create_engine
        from .demo_schema import GOLDEN, HINTS, create_demo_db
        demo_url = create_demo_db()
        engine = create_engine(demo_url)
        cat = Catalog(name="studio").bootstrap(engine)
        for table, text in HINTS.items():
            cat.hint(table, text)
        cat.restrict("hr_compensation", ["payroll"])
        title, blurb = "Demo schema", "42 objects. Pass --url to run this against your own database."
        questions = [q for q, _ in GOLDEN]
    state = StudioState(cat, title, blurb, questions, engine=engine)
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
