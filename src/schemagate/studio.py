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
                 questions: Optional[List[str]] = None):
        self.catalog = catalog
        self.title = title
        self.blurb = blurb
        self.questions = questions or []
        self.described: Optional[Catalog] = None

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

    def select(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cat = self.catalog
        question = str(body.get("question") or "").strip()[:2000]
        if not question:
            return {"error": "question is empty"}
        top_k = max(1, min(int(body.get("top_k") or 6), 50))
        who = None
        if body.get("principal"):
            who = Principal(str(body["principal"]), roles={str(r) for r in body.get("roles") or []})
        sel = cat.select(question, top_k=top_k, principal=who)
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
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/select":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    return self._json(400, {"error": "body must be a JSON object"})
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
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"schemagate studio: {url}   ({len(state.catalog._docs)} objects)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                                # noqa: BLE001
            pass
    return server


def main(url: Optional[str] = None, host: str = "127.0.0.1", port: int = 8770,
         open_browser: bool = True, include=None, exclude=None) -> int:
    if url:
        cat = Catalog(name="studio").bootstrap(url, include=include, exclude=exclude)
        title = "Your database"
        blurb = f"{len(cat._docs)} objects reflected. Hints and restrictions come from your code or config."
        questions: List[str] = []
    else:
        from .demo_schema import GOLDEN, HINTS, create_demo_db
        cat = Catalog(name="studio").bootstrap(create_demo_db())
        for table, text in HINTS.items():
            cat.hint(table, text)
        cat.restrict("hr_compensation", ["payroll"])
        title, blurb = "Demo schema", "42 objects. Pass --url to run this against your own database."
        questions = [q for q, _ in GOLDEN]
    state = StudioState(cat, title, blurb, questions)
    server = serve(state, host, port, open_browser)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
