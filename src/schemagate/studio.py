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
        #: Whether a real database has been reflected. Not "is there an
        #: engine": the ORDS path has a catalog and no engine at all, and
        #: answering that question with the engine left the page insisting it
        #: still needed connecting while showing 29 of the user's own tables.
        self.connected = False
        #: Whether the current connection was written down for next time.
        #: Reported to the page so it can say so rather than leaving someone
        #: to wonder whether a restart will cost them the wallet fields again.
        self.remembered = False
        #: True while a remembered connection is being replayed on a
        #: background thread, so the page can say "connecting" instead of
        #: "no database connected" for the half-minute that takes.
        self.connecting = False
        self.connect_error: Optional[str] = None
        #: The request that produced the current catalog, so "Resync" can
        #: replay it without asking for the wallet and passwords again.
        self.last_connect: Optional[Dict[str, Any]] = None
        #: Saving a connection means putting a database password on disk, in a
        #: tool that otherwise stores nothing. That is the user's call, not a
        #: default -- set by --remember, or per-connection by the page's
        #: checkbox.
        self.remember_connection = False

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

    def _connect_ords(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Catalog over HTTPS, for a machine that cannot reach 1522.

        No engine is kept. Everything that needs one -- running the SQL a
        model writes, sampling values -- stays off, and the page is told so
        rather than being left to fail at the point of use.
        """
        from .catalog import Catalog
        from .ords import OrdsError, ords_base, reflect_ords

        user = str(body.get("user") or "")
        schema = str(body.get("schema") or body.get("schemas") or user or "")
        if isinstance(schema, list):
            schema = schema[0] if schema else user
        try:
            docs = reflect_ords(str(body.get("url") or ""), schema=schema,
                                user=user, password=str(body.get("password") or ""))
        except OrdsError as e:
            return {"error": f"could not connect -- {e}"}
        except Exception as e:                            # noqa: BLE001
            from .connect import safe_error
            return {"error": "could not connect -- " +
                             safe_error(e, str(body.get("password") or ""))}

        cat = Catalog(name="studio")
        cat.add_all(docs)
        cat.index()
        self.catalog = cat
        self.engine = None
        self.is_demo = False
        self.connected = True
        self.title = "Your database"
        self.blurb = (f"{len(docs)} objects reflected from Oracle over ORDS. "
                      "Read-only catalog: no rows are read on this path.")
        self.questions = []
        base = ords_base(str(body.get("url") or ""))
        return {"objects": len(docs), "dialect": "oracle (ORDS)",
                "schemas": [schema.upper()], "grants": None, "values": False,
                "demo": False, "schema": self.schemas_json()["live"],
                "recipe": {
                    "cli": "# ORDS is a library path today, not a CLI flag",
                    "python": (
                        "from schemagate import Catalog\n"
                        "from schemagate.ords import reflect_ords\n\n"
                        "docs = reflect_ords(\n"
                        f"    {base!r},\n"
                        f"    schema={schema.upper()!r}, user={user!r},\n"
                        "    password=$DB_PASSWORD)\n"
                        "cat = Catalog()\n"
                        "cat.add_all(docs)   # returns None; chaining it does not work\n"
                        "cat.index()"),
                    "studio": "# connect from the page: Database -> Oracle over HTTPS (ORDS)",
                }}

    def resync(self) -> Dict[str, Any]:
        """Re-reflect the same database, keeping the descriptions.

        A schema moves -- a column is added, a view is replaced -- and the
        catalog is a snapshot taken at connect time. Without this the only way
        to pick that up was to retype the whole connection, which for a wallet
        is a directory and two passwords. Descriptions are carried across by
        qualified name so a resync does not mean paying to catalogue again;
        an object whose structure actually changed is re-described by the
        fingerprint cache the next time cataloguing runs.
        """
        if not self.last_connect:
            return {"error": "nothing to resync -- connect first"}
        keep = {q: d.description for q, d in self.catalog._docs.items()
                if d.description}
        hints = {q: d.hint for q, d in self.catalog._docs.items() if d.hint}
        out = self.connect(dict(self.last_connect))
        if out.get("error"):
            return out
        # Three outcomes, not two. A description that came back on its own is
        # kept, not lost: most of these originate as database comments, which
        # the reflection re-reads every time. Counting "I did not have to
        # restore it" as a loss reported 7 lost on a resync that lost nothing.
        restored = reflected = gone = 0
        for q, text in keep.items():
            doc = self.catalog._docs.get(q)
            if doc is None:
                gone += 1                      # the object itself is no longer there
            elif doc.description:
                reflected += 1                 # came back from the database
            else:
                doc.description = text
                restored += 1
        for q, text in hints.items():
            doc = self.catalog._docs.get(q)
            if doc is not None:
                doc.hint = text
        self.catalog.index()
        out["descriptions_kept"] = restored + reflected
        out["descriptions_restored"] = restored
        out["descriptions_lost"] = gone
        return out

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

        # ORDS is not a SQLAlchemy URL and never will be -- it is a REST
        # endpoint that runs a statement and returns JSON. It gets its own
        # branch rather than a fake dialect, because the difference is real:
        # this path reads the catalog and nothing else.
        if str(body.get("kind") or "").lower() == "ords":
            return self._connect_ords(body)

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
                url, connect_args=with_timeout(url, connect_args),
                # A pooled connection that has been sitting idle is not
                # necessarily still open: an Autonomous Database closes idle
                # sessions, and any firewall between here and 1522 will drop
                # the socket without telling either end. Without pre_ping the
                # pool hands that dead connection to the next question and the
                # page reports a lost connection for a database that is up.
                # pre_ping costs one round trip on checkout and turns the
                # whole class of "it worked this morning" into a silent
                # reconnect.
                pool_pre_ping=True,
                # And retire them on a timer regardless, so a connection never
                # ages past whatever the far end is willing to keep.
                pool_recycle=1800)
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
            msg = safe_error(e, *secrets)
            # DPY-6005 and DPY-4011 are both "the TCP connection did not
            # happen", and on an Autonomous Database that is nearly always a
            # network that will not carry 1522 rather than anything about the
            # wallet -- a wallet fault reads ORA-28759 or a PEM error. The
            # same machine can almost always reach Database Actions on 443,
            # so say which door is open instead of leaving someone to
            # re-check a wallet that was never the problem.
            # The ancient "SQL Server" driver still shipped with Windows binds
            # parameters in a way the mssql dialect's reflection queries trip
            # over, and it surfaces as HY104 / "Invalid precision value (0)"
            # against INFORMATION_SCHEMA -- which reads like a broken database
            # rather than a driver that is twenty years past its use. It is
            # also what someone gets by default, because it is the only one
            # present until Microsoft's is installed deliberately.
            if "HY104" in msg or "Invalid precision value" in msg:
                msg += ("  |  This is the ODBC driver, not the database. The "
                        "legacy 'SQL Server' driver cannot reflect a schema. "
                        "Install 'ODBC Driver 18 for SQL Server' from Microsoft "
                        "and set Driver to it.")
            if ("DPY-6005" in msg or "DPY-4011" in msg) and "oracle" in url.lower():
                msg += ("  |  This is the network, not the wallet: nothing "
                        "reached port 1522. If Database Actions opens in your "
                        "browser, use the 'Oracle over HTTPS (ORDS)' "
                        "connection type instead -- same database, port 443.")
            return {"error": "could not connect -- " + msg}

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
        self.connected = True
        # Write the request down now that it is known to work. Only a connect
        # that actually reflected something is worth replaying on restart, so
        # this sits after the bootstrap rather than beside the form handler.
        self.last_connect = dict(body)
        from . import remember
        self.remembered = remember.save(
            body, allow=self.remember_connection or bool(body.get("remember"))
        ) is not None
        self.title = "Your database"
        # Say what is actually true. Oracle and PostgreSQL both carry table
        # comments, so a fresh reflection often arrives already part-described
        # -- announcing "nothing is catalogued yet" over seven descriptions
        # reads as a product that has not noticed its own state.
        _described = sum(1 for d in cat._docs.values() if d.description)
        self.blurb = (f"{len(cat._docs)} objects reflected from "
                      f"{engine.dialect.name}. " +
                      (f"{_described} already carry a description."
                       if _described else "Nothing is catalogued yet."))
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


def _render_page(state: StudioState, _raw: Dict[str, str] = {}) -> bytes:
    """Build the page against the state as it is *now*.

    This used to be done once, when the server started, and the resulting
    bytes were closed over and served to every GET for the life of the
    process. Connecting from the page updated the server but could not update
    those bytes, so refreshing the browser re-served the startup snapshot: a
    rail still reading "Nothing connected yet" and an empty object list, while
    /api/settings answered `connected: true` and the header said "connected to
    your database". The page looked like it had lost a connection it still
    had. Re-rendering per request is what makes a refresh tell the truth.

    Only the file read is cached -- the injection is a couple of small JSON
    dumps (`docs` is deliberately empty in `schemas_json`), so this is cheap
    enough to do per request and wrong to do any less often.
    """
    page = _raw.get("html")
    if page is None:
        page = _raw["html"] = _PAGE.read_text("utf-8")
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
    return page.encode("utf-8")


def _handler(state: StudioState):

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
                body = _render_page(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # The page carries connection state now, so a cached copy is a
                # stale one -- which is the bug this whole change is about.
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/health":
                self._json(200, {"status": "ok", "objects": len(state.catalog._docs)})
            elif self.path == "/api/settings":
                out = state.describe_settings()
                out["allow_connect"] = state.allow_connect
                out["connected"] = state.connected and not state.is_demo
                #: The engine is a separate question -- it is what runs the SQL
                #: a model writes, and the ORDS path has none.
                out["can_run_sql"] = state.engine is not None
                out["demo"] = state.is_demo
                # The object count travels with the flag so the page can tell
                # "connected" from a catalog it can see, rather than trusting a
                # boolean it may have read before the connection existed.
                out["objects"] = len(state.catalog._docs)
                out["connecting"] = state.connecting
                out["connect_error"] = state.connect_error
                # Whether "Resync" and "Re-catalogue" have anything to act on.
                out["can_resync"] = bool(state.last_connect)
                out["described"] = sum(
                    1 for d in state.catalog._docs.values() if d.description)
                self._json(200, out)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/api/select", "/api/answer", "/api/settings",
                                 "/api/describe", "/api/apply-descriptions",
                                 "/api/connect", "/api/resync"):
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
                if self.path == "/api/resync":
                    return self._json(200, state.resync())
                if self.path == "/api/connect":
                    return self._json(200, state.connect(payload))
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
         demo: bool = False, forget: bool = False,
         remember_connection: bool = False) -> int:
    if forget:
        from . import remember
        print("forgot the remembered connection" if remember.forget()
              else "there was no remembered connection")
        return 0
    engine = None
    if url:
        from sqlalchemy import create_engine
        # Same reasoning as the Connect path: an idle pooled connection is not
        # a live one, and without pre_ping the first question after a quiet
        # spell fails against a database that never went away.
        engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
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
    # `--url` is a connection made before the page opened; the page must not
    # then ask for one.
    state.connected = bool(url)
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
    state.remember_connection = bool(remember_connection)

    # Replay the last connection, if there is one and nothing more specific
    # was asked for. This is what stops a restart from meaning "type the
    # wallet directory and both passwords again" -- the connection outlives
    # the process that made it. --url and --demo are explicit instructions and
    # win; a saved connection only fills the otherwise-empty case.
    #
    # On a background thread, and the server starts first. Reflecting a real
    # schema across a network takes tens of seconds -- 30 against an
    # Autonomous Database -- and doing it before serve() meant the port was
    # not open for that whole time: the browser opened on a refused
    # connection, which looks exactly like a Studio that failed to start. The
    # page comes up immediately now and fills in when the reconnect lands.
    saved = None
    if not url and not demo and state.allow_connect:
        from . import remember
        saved = remember.load()

    server = serve(state, host, port, open_browser)

    if saved:
        def _reconnect():
            print("reconnecting to the remembered database...", file=sys.stderr)
            try:
                out = state.connect(dict(saved))
            except Exception as e:                        # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
            finally:
                state.connecting = False
            if out.get("error"):
                # A remembered connection that no longer works must not stop
                # the Studio -- the page is how someone fixes it.
                state.connect_error = out["error"]
                print(f"  could not reconnect: {out['error']}", file=sys.stderr)
                print("  (clear it with: schemagate studio --forget)", file=sys.stderr)
            else:
                print(f"  reconnected: {out['objects']} objects from "
                      f"{out['dialect']}", file=sys.stderr)

        state.connecting = True
        threading.Thread(target=_reconnect, daemon=True).start()

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
