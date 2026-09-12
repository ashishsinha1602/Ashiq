"""The MCP server, driven by a real MCP client.

The tool functions are tested directly first (no mcp package needed), then
the whole thing is exercised by spawning ``python -m schemagate.mcp_server`` and
talking to it over stdio with ``mcp``'s own client -- the same path a desktop
Desktop and Cursor use, and one that works on both SDK 1.x and 2.x.
"""
import json

import pytest

from schemagate import mcp_server


@pytest.fixture
def served(cat):
    """The commerce fixture, with one restriction, installed as the catalog."""
    cat.restrict("hr_compensation", ["payroll"])
    mcp_server.build_catalog(catalog=cat)
    return cat


# --- tool functions, no transport -----------------------------------------

def test_select_returns_ddl_and_explanations(served):
    out = mcp_server.select_schema("revenue by month", top_k=3)
    assert out["selected"] >= 3
    assert "v_monthly_revenue" in " ".join(out["objects"])
    assert "TABLE" in out["ddl"] or "VIEW" in out["ddl"]
    assert all({"object", "score", "reason"} <= set(e) for e in out["explain"])


def test_anonymous_caller_fails_closed(served):
    """No principal supplied means restricted objects are simply absent."""
    out = mcp_server.select_schema("salary by employee", top_k=10)
    assert "hr_compensation" not in " ".join(out["objects"])
    assert "hr_compensation" not in out["ddl"].lower()


def test_authorised_caller_sees_restricted_object(served):
    out = mcp_server.select_schema("salary by employee", top_k=10,
                                   principal="okta:hr", roles=["payroll"])
    assert "hr_compensation" in " ".join(out["objects"])


def test_wrong_role_is_not_enough(served):
    out = mcp_server.select_schema("salary by employee", top_k=10,
                                   principal="okta:x", roles=["finance"])
    assert "hr_compensation" not in " ".join(out["objects"])


def test_malformed_principal_is_an_error_not_a_crash(served):
    out = mcp_server.select_schema("anything", principal="no-namespace")
    assert "error" in out and "namespaced" in out["error"]


# --- run_query: the half that turns a selection into an answer ------------
#
# select_schema withholding a table means nothing if the next tool will run
# `SELECT * FROM` it anyway, so every one of these is about that.

@pytest.fixture
def runnable(cat, db_url, monkeypatch):
    """The commerce fixture with a restriction, and a database to query."""
    cat.restrict("hr_compensation", ["payroll"])
    mcp_server.build_catalog(catalog=cat)
    monkeypatch.setattr(mcp_server, "_URL", db_url, raising=False)
    monkeypatch.setattr(mcp_server, "_ENGINE", None, raising=False)
    return cat


def test_run_query_returns_rows(runnable):
    out = mcp_server.run_query("SELECT id FROM crm_customer", max_rows=3)
    assert out["columns"] == ["id"]
    assert out["row_count"] <= 3
    assert out["truncated"] in (True, False)


def test_run_query_refuses_a_table_the_caller_may_not_see(runnable):
    out = mcp_server.run_query("SELECT * FROM hr_compensation")
    assert "not available to this caller" in out["error"]
    assert "hr_compensation" in out["error"]


def test_run_query_allows_it_with_the_role(runnable):
    out = mcp_server.run_query("SELECT * FROM hr_compensation",
                               principal="okta:hr", roles=["payroll"])
    assert "error" not in out, out


def test_a_restricted_table_cannot_hide_in_a_join(runnable):
    out = mcp_server.run_query(
        "SELECT * FROM crm_customer c JOIN hr_compensation h ON 1=1")
    assert "not available to this caller" in out["error"]


def test_a_restricted_table_cannot_hide_in_a_cte(runnable):
    out = mcp_server.run_query(
        "WITH x AS (SELECT * FROM hr_compensation) SELECT * FROM x")
    assert "not available to this caller" in out["error"]


def test_scope_is_taken_from_this_call_not_the_previous_one(runnable):
    """An earlier authorised select_schema must not authorise a later query."""
    mcp_server.select_schema("salary by employee", top_k=10,
                             principal="okta:hr", roles=["payroll"])
    out = mcp_server.run_query("SELECT * FROM hr_compensation")
    assert "not available to this caller" in out["error"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM crm_customer",
    "UPDATE crm_customer SET id = 1",
    "DROP TABLE crm_customer",
    "SELECT 1 FROM crm_customer; DROP TABLE crm_customer",
    "SELECT 1 FROM crm_customer -- \nUPDATE crm_customer SET id = 1",
])
def test_run_query_refuses_anything_that_is_not_a_read(runnable, sql):
    out = mcp_server.run_query(sql)
    assert "error" in out, sql


def test_an_unknown_table_is_refused_rather_than_attempted(runnable):
    """Fail closed: restricted, misspelled and never-reflected look alike."""
    out = mcp_server.run_query("SELECT * FROM no_such_table")
    assert "not available to this caller" in out["error"]


def test_the_database_error_comes_back_so_the_model_can_fix_it(runnable):
    out = mcp_server.run_query("SELECT nope FROM crm_customer")
    assert "the database rejected this query" in out["error"]
    assert "nope" in out["error"]


def test_row_cap_is_enforced_and_truncation_is_reported(runnable):
    out = mcp_server.run_query("SELECT id FROM crm_customer", max_rows=1)
    assert out["row_count"] <= 1
    assert isinstance(out["truncated"], bool)


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_run_query_survives_empty_sql(runnable, bad):
    assert "error" in mcp_server.run_query(bad)


def test_answer_without_a_model_hands_back_the_selection(runnable, monkeypatch):
    monkeypatch.delenv("SCHEMAGATE_MCP_PROVIDER", raising=False)
    monkeypatch.delenv("SCHEMAGATE_MCP_MODEL", raising=False)
    out = mcp_server.answer("revenue by month")
    assert out["sql"] is None
    assert "run_query" in out["next"]
    assert out["objects"]


def test_referenced_tables_ignores_ctes_and_keeps_qualified_names():
    found = mcp_server._referenced_tables(
        'WITH t AS (SELECT 1 FROM a) SELECT * FROM t JOIN main.b ON 1=1')
    assert "a" in found and "main.b" in found and "t" not in found


def test_list_objects_is_scoped(served):
    anon = mcp_server.list_objects()
    hr = mcp_server.list_objects(principal="okta:hr", roles=["payroll"])
    names_anon = {o["name"] for o in anon["objects"]}
    names_hr = {o["name"] for o in hr["objects"]}
    assert "main.hr_compensation" not in names_anon
    assert "main.hr_compensation" in names_hr
    assert hr["count"] == anon["count"] + 1


def test_describe_object_does_not_leak_existence(served):
    """A restricted object and a missing one must look identical."""
    restricted = mcp_server.describe_object("hr_compensation")
    missing = mcp_server.describe_object("no_such_table")
    assert "error" in restricted and "error" in missing
    assert restricted["error"].replace("hr_compensation", "X") == \
        missing["error"].replace("no_such_table", "X")


def test_describe_object_for_authorised_caller(served):
    out = mcp_server.describe_object("hr_compensation", principal="okta:hr",
                                     roles=["payroll"])
    assert out["name"] == "main.hr_compensation"
    assert "annual_amount" in out["ddl"]
    assert any(fk["references"] == "hr_employee" for fk in out["foreign_keys"])


def test_demo_url_builds_the_bundled_schema(monkeypatch):
    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL", "demo")
    monkeypatch.delenv("SCHEMAGATE_CATALOG_CONFIG", raising=False)
    cat = mcp_server.build_catalog()
    assert len(cat._docs) == 42
    assert cat._docs["main.hr_compensation"].roles == ["payroll"]


def test_missing_url_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("SCHEMAGATE_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="SCHEMAGATE_DATABASE_URL"):
        mcp_server.build_catalog()


def test_config_file_applies_restrictions_and_hints(tmp_path, monkeypatch):
    config = tmp_path / "cfg.json"
    config.write_text(json.dumps({
        "restrict": {"billing_invoice": ["finance"]},
        "hint": {"billing_payment": "cash receipts"},
    }))
    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL", "demo")
    monkeypatch.setenv("SCHEMAGATE_CATALOG_CONFIG", str(config))
    cat = mcp_server.build_catalog()
    assert cat._docs["main.billing_invoice"].roles == ["finance"]
    assert cat._docs["main.billing_payment"].hint == "cash receipts"


# --- through a real MCP client, over stdio, in a subprocess --------------
# This is the path desktop MCP clients and Cursor use. It is also the only test
# surface that is identical across MCP SDK 1.x and 2.x -- the in-memory test
# helpers were removed in 2.0, and coupling to them is how a clean install
# broke the first time.

import os
import sys


def _stdio_params():
    from mcp import StdioServerParameters
    env = {k: v for k, v in os.environ.items() if k != "SCHEMAGATE_CATALOG_CONFIG"}
    env["SCHEMAGATE_DATABASE_URL"] = "demo"
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "schemagate.mcp_server"], env=env)


async def _call(session, tool, args):
    result = await session.call_tool(tool, args)
    return "".join(getattr(c, "text", "") for c in result.content)


@pytest.mark.anyio
async def test_round_trip_over_stdio_subprocess():
    """What an actual MCP client receives from `python -m schemagate.mcp_server`."""
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_stdio_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert {"select_schema", "list_objects", "describe_object"} <= names

            anon = await _call(session, "select_schema",
                               {"question": "salary by employee", "top_k": 10})
            assert "hr_compensation" not in anon.lower()

            hr = await _call(session, "select_schema",
                             {"question": "salary by employee", "top_k": 10,
                              "principal": "okta:hr", "roles": ["payroll"]})
            assert "hr_compensation" in hr


@pytest.mark.anyio
async def test_tool_schemas_expose_identity_parameters():
    """Clients must be able to see that principal and roles exist."""
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_stdio_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            tool = tools["select_schema"]
            # the field was renamed between SDK 1.x (camelCase) and 2.x
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema")
            assert {"question", "principal", "roles", "top_k"} <= set(schema["properties"])


def test_server_builds_on_whichever_sdk_is_installed(served):
    """The v1/v2 shim: create_server() must work, not just import."""
    pytest.importorskip("mcp")
    app = mcp_server.create_server()
    assert hasattr(app, "run")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- it must not die -------------------------------------------------------
# Everything below throws bad input at the server. The invariant is not
# "returns the right answer" but "returns a dict, and the next good request
# is still answered correctly".

_GARBAGE = [
    None, "", "   ", 0, -1, 10 ** 9, 3.14, True, [], {}, [None], {"a": 1},
    "x" * 100_000, "\x00\x01\x02", "🔥" * 500, "'; DROP TABLE users; --",
    "\\u0000", "SELECT * FROM", "okta:", ":", "a:b:c:d", "no namespace",
    "\n\r\t", "𝔘𝔫𝔦𝔠𝔬𝔡𝔢", "<script>alert(1)</script>",
]


def _still_healthy():
    out = mcp_server.select_schema("salary by employee", top_k=10,
                                   principal="okta:hr", roles=["payroll"])
    assert "error" not in out and "hr_compensation" in " ".join(out["objects"])
    anon = mcp_server.select_schema("salary by employee", top_k=10)
    assert "hr_compensation" not in " ".join(anon["objects"])


@pytest.mark.parametrize("bad", _GARBAGE, ids=[repr(g)[:20] for g in _GARBAGE])
def test_select_survives_garbage_question(served, bad):
    out = mcp_server.select_schema(bad)
    assert isinstance(out, dict)
    _still_healthy()


@pytest.mark.parametrize("bad", _GARBAGE, ids=[repr(g)[:20] for g in _GARBAGE])
def test_select_survives_garbage_principal(served, bad):
    out = mcp_server.select_schema("revenue", principal=bad)
    assert isinstance(out, dict)
    _still_healthy()


@pytest.mark.parametrize("bad", _GARBAGE, ids=[repr(g)[:20] for g in _GARBAGE])
def test_select_survives_garbage_roles(served, bad):
    out = mcp_server.select_schema("revenue", principal="okta:x", roles=bad)
    assert isinstance(out, dict)
    _still_healthy()


@pytest.mark.parametrize("bad", _GARBAGE, ids=[repr(g)[:20] for g in _GARBAGE])
def test_select_survives_garbage_top_k(served, bad):
    out = mcp_server.select_schema("revenue", top_k=bad)
    assert isinstance(out, dict)
    if "error" not in out:
        assert 1 <= out["selected"] <= mcp_server.MAX_TOP_K + 20   # + fk expansion
    _still_healthy()


@pytest.mark.parametrize("bad", _GARBAGE, ids=[repr(g)[:20] for g in _GARBAGE])
def test_describe_and_list_survive_garbage(served, bad):
    assert isinstance(mcp_server.describe_object(bad), dict)
    assert isinstance(mcp_server.list_objects(principal=bad), dict)
    _still_healthy()


def test_question_is_capped_not_rejected(served):
    out = mcp_server.select_schema("revenue " * 10_000)
    assert "error" not in out
    assert len(out["question"]) <= mcp_server.MAX_QUESTION_CHARS


def test_too_many_roles_is_an_error_not_a_crash(served):
    out = mcp_server.select_schema("revenue", principal="okta:x",
                                   roles=[f"r{i}" for i in range(500)])
    assert "error" in out and "roles" in out["error"]
    _still_healthy()


def test_a_broken_catalog_method_does_not_kill_the_tool(served, monkeypatch):
    """Simulate an internal failure: the tool reports it, the process lives."""
    def boom(*a, **k):
        raise RuntimeError("simulated internal failure")
    monkeypatch.setattr(served, "select", boom)
    out = mcp_server.select_schema("revenue")
    assert out["error"].startswith("select_schema failed")
    assert "hint" in out
    monkeypatch.undo()
    _still_healthy()


def test_health_reports_state(served):
    h = mcp_server.health()
    assert h["status"] == "ok" and h["objects"] == 42
    assert h["calls"] >= 1 and "version" in h


def test_refresh_keeps_previous_catalog_when_database_is_gone(monkeypatch):
    """The database disappears; the server must keep its last good index."""
    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL", "demo")
    monkeypatch.delenv("SCHEMAGATE_CATALOG_CONFIG", raising=False)
    mcp_server.build_catalog()
    before = mcp_server.health()["objects"]

    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL",
                       "postgresql+psycopg://nobody@127.0.0.1:1/nowhere")
    out = mcp_server.refresh_catalog()
    assert out["ok"] is False and out["kept_previous"] is True
    assert mcp_server.health()["objects"] == before
    assert mcp_server.health()["last_refresh_ok"] is False
    # and it still answers
    assert "error" not in mcp_server.select_schema("revenue")


def test_refresh_succeeds_and_swaps_atomically(monkeypatch):
    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL", "demo")
    monkeypatch.delenv("SCHEMAGATE_CATALOG_CONFIG", raising=False)
    mcp_server.build_catalog()
    out = mcp_server.refresh_catalog()
    assert out["ok"] is True and out["objects"] == 42
    assert mcp_server.health()["last_refresh_ok"] is True


def test_password_never_appears_in_health(monkeypatch):
    monkeypatch.setenv("SCHEMAGATE_DATABASE_URL", "demo")
    mcp_server.build_catalog()
    assert mcp_server._redact("postgresql://alice:s3cret@db.internal/app") == \
        "postgresql://alice:***@db.internal/app"
    assert "s3cret" not in json.dumps(mcp_server.health())


@pytest.mark.anyio
async def test_stdio_server_survives_garbage_from_a_real_client():
    """Over the wire: 40 bad calls, then a good one must still work."""
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_stdio_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for bad in ["", "x" * 50_000, "'; DROP--", "🔥" * 200]:
                for tool, args in [
                    ("select_schema", {"question": bad}),
                    ("select_schema", {"question": "revenue", "principal": bad}),
                    ("describe_object", {"name": bad}),
                    ("list_objects", {"principal": bad}),
                ]:
                    try:
                        await session.call_tool(tool, args)
                    except Exception:            # noqa: BLE001
                        pass                     # a protocol-level reject is fine
            # wrong types at the protocol layer
            for args in [{"question": 123}, {"question": "x", "top_k": "lots"},
                         {"question": "x", "roles": "notalist"}, {}]:
                try:
                    await session.call_tool("select_schema", args)
                except Exception:                # noqa: BLE001
                    pass
            good = await _call(session, "select_schema",
                               {"question": "salary by employee", "top_k": 10,
                                "principal": "okta:hr", "roles": ["payroll"]})
            assert "hr_compensation" in good, "server did not survive the garbage"
            h = await _call(session, "health", {})
            assert '"status": "ok"' in h or "'status': 'ok'" in h or "ok" in h
