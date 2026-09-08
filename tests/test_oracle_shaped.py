"""Oracle-shaped metadata through the real reflection code, no server needed.

The live Oracle run still needs an instance (``SCHEMAGATE_ORACLE_URL``). What
can be tested without one is everything schemagate does *with* what Oracle's
SQLAlchemy dialect hands back: uppercase owners and names, ``VARCHAR2(100
CHAR)`` and ``NUMBER(10, 2)`` type strings, ``DATE``/``TIMESTAMP(6)``/``CLOB``,
system schemas that must be skipped, view definitions full of ``NVL``,
``DECODE`` and ``SYSDATE``, and the object_list shape Select AI expects.

A fake Inspector returns exactly that, and ``schemagate.introspect.reflect`` is
run against it unchanged.
"""
import pytest

from schemagate import Catalog, Principal
from schemagate.introspect import _SYSTEM_SCHEMAS, reflect
from schemagate.models import _identifiers

# --- what python-oracledb + SQLAlchemy actually return -----------------------

OWNER = "APP_OWNER"

_TABLES = {
    "CUSTOMERS": [
        ("CUSTOMER_ID", "NUMBER(10, 0)", False, "Surrogate key", True),
        ("CUSTOMER_NAME", "VARCHAR2(200 CHAR)", False, None, False),
        ("SEGMENT_CD", "VARCHAR2(20 CHAR)", True, "Segment code, see REF_SEGMENT", False),
        ("CREATED_DT", "DATE", False, None, False),
        ("NOTES", "CLOB", True, "Free text, multi-line\nsecond line of comment", False),
    ],
    "ORDERS": [
        ("ORDER_ID", "NUMBER(10, 0)", False, None, True),
        ("CUSTOMER_ID", "NUMBER(10, 0)", False, None, False),
        ("ORDER_DT", "DATE", False, None, False),
        ("ORDER_TS", "TIMESTAMP(6) WITH TIME ZONE", True, None, False),
        ("TOTAL_AMT", "NUMBER(12, 2)", True, "Order total, net", False),
        ("STATUS_CD", "VARCHAR2(10 CHAR)", True, None, False),
    ],
    "ORDER_LINES": [
        ("ORDER_LINE_ID", "NUMBER(10, 0)", False, None, True),
        ("ORDER_ID", "NUMBER(10, 0)", False, None, False),
        ("PRODUCT_ID", "NUMBER(10, 0)", False, None, False),
        ("QTY", "NUMBER(10, 3)", False, None, False),
        ("UNIT_PRICE", "NUMBER(12, 4)", False, None, False),
    ],
    "PRODUCTS": [
        ("PRODUCT_ID", "NUMBER(10, 0)", False, None, True),
        ("SKU", "VARCHAR2(40 CHAR)", False, None, False),
        ("PRODUCT_DESC", "NVARCHAR2(400)", True, None, False),
        ("LIST_PRICE", "NUMBER(12, 4)", True, None, False),
        ("IS_ACTIVE", "CHAR(1 CHAR)", False, "Y/N", False),
    ],
    "REF_SEGMENT": [
        ("SEGMENT_CD", "VARCHAR2(20 CHAR)", False, None, True),
        ("SEGMENT_DESC", "VARCHAR2(100 CHAR)", False, None, False),
    ],
    "EMP_SALARY": [
        ("EMP_ID", "NUMBER(10, 0)", False, None, True),
        ("ANNUAL_SAL", "NUMBER(12, 2)", False, None, False),
        ("EFFECTIVE_DT", "DATE", False, None, False),
    ],
    "ORDERS_BKP": [
        ("ORDER_ID", "NUMBER(10, 0)", False, None, True),
        ("TOTAL_AMT", "NUMBER(12, 2)", True, None, False),
    ],
    "MLOG$_ORDERS": [                 # materialised-view log: Oracle noise
        ("ORDER_ID", "NUMBER", True, None, False),
        ("SNAPTIME$$", "DATE", True, None, False),
        ("DMLTYPE$$", "VARCHAR2(1)", True, None, False),
    ],
}

_FKS = {
    "ORDERS": [{"constrained_columns": ["CUSTOMER_ID"], "referred_table": "CUSTOMERS",
                "referred_columns": ["CUSTOMER_ID"], "name": "FK_ORD_CUST"}],
    "ORDER_LINES": [{"constrained_columns": ["ORDER_ID"], "referred_table": "ORDERS",
                     "referred_columns": ["ORDER_ID"], "name": "FK_OL_ORD"},
                    {"constrained_columns": ["PRODUCT_ID"], "referred_table": "PRODUCTS",
                     "referred_columns": ["PRODUCT_ID"], "name": "FK_OL_PROD"}],
    "CUSTOMERS": [{"constrained_columns": ["SEGMENT_CD"], "referred_table": "REF_SEGMENT",
                   "referred_columns": ["SEGMENT_CD"], "name": "FK_CUST_SEG"}],
}

_VIEWS = {
    "V_OPEN_ORDERS": (
        "SELECT o.ORDER_ID, c.CUSTOMER_NAME, NVL(o.TOTAL_AMT, 0) AS TOTAL_AMT,\n"
        "       DECODE(o.STATUS_CD, 'N', 'New', 'P', 'Picked', 'Other') AS STATUS,\n"
        "       TRUNC(SYSDATE) - TRUNC(o.ORDER_DT) AS AGE_DAYS\n"
        "  FROM ORDERS o JOIN CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID\n"
        " WHERE o.STATUS_CD NOT IN ('S', 'X') AND ROWNUM <= 1000"),
    "V_REVENUE_BY_MONTH": (
        "SELECT TO_CHAR(o.ORDER_DT, 'YYYY-MM') AS YM, SUM(ol.QTY * ol.UNIT_PRICE) AS REVENUE\n"
        "  FROM ORDERS o JOIN ORDER_LINES ol ON ol.ORDER_ID = o.ORDER_ID\n"
        " GROUP BY TO_CHAR(o.ORDER_DT, 'YYYY-MM')"),
    "V_SLOW_MOVING_STOCK": (
        "SELECT p.SKU, p.PRODUCT_DESC, MAX(o.ORDER_DT) AS LAST_SOLD_DT\n"
        "  FROM PRODUCTS p LEFT JOIN ORDER_LINES ol ON ol.PRODUCT_ID = p.PRODUCT_ID\n"
        "  LEFT JOIN ORDERS o ON o.ORDER_ID = ol.ORDER_ID\n"
        " GROUP BY p.SKU, p.PRODUCT_DESC HAVING MAX(o.ORDER_DT) < ADD_MONTHS(SYSDATE, -6)"),
}

_TABLE_COMMENTS = {"ORDERS": "Sales orders.\nOne row per order header.",
                   "EMP_SALARY": "Payroll -- restricted"}


class FakeOracleInspector:
    """Returns what SQLAlchemy's oracle dialect returns, shape for shape."""

    default_schema_name = OWNER

    def get_schema_names(self):
        # the dialect lists every user, including the ones that must be skipped
        return ["SYS", "SYSTEM", "AUDSYS", "CTXSYS", "DBSNMP", "GSMADMIN_INTERNAL",
                "MDSYS", "OUTLN", "XDB", OWNER, "OTHER_APP"]

    def get_table_names(self, schema=None):
        if schema == OWNER:
            return list(_TABLES)
        if schema == "OTHER_APP":
            return ["ORDERS"]                 # same name, different owner
        return ["DUAL", "USER$", "OBJ$"] if schema in ("SYS", "SYSTEM") else []

    def get_view_names(self, schema=None):
        return list(_VIEWS) if schema == OWNER else []

    def get_columns(self, name, schema=None):
        if schema == "OTHER_APP" and name == "ORDERS":
            return [{"name": "ORDER_ID", "type": "NUMBER(10, 0)", "nullable": False, "comment": None},
                    {"name": "LEGACY_REF", "type": "VARCHAR2(50 CHAR)", "nullable": True, "comment": None}]
        if name in _VIEWS:
            return [{"name": c, "type": "VARCHAR2(4000 CHAR)", "nullable": True, "comment": None}
                    for c in ("COL1", "COL2", "COL3")]
        if name not in _TABLES:
            raise Exception(f"ORA-00942: table or view does not exist: {name}")
        return [{"name": n, "type": t, "nullable": nl, "comment": cm}
                for n, t, nl, cm, _ in _TABLES[name]]

    def get_pk_constraint(self, name, schema=None):
        cols = [n for n, _, _, _, pk in _TABLES.get(name, []) if pk]
        return {"constrained_columns": cols, "name": f"PK_{name}" if cols else None}

    def get_foreign_keys(self, name, schema=None):
        return _FKS.get(name, []) if schema == OWNER else []

    def get_table_comment(self, name, schema=None):
        return {"text": _TABLE_COMMENTS.get(name)}

    def get_view_definition(self, name, schema=None):
        return _VIEWS.get(name)


class FakeEngine:
    """Enough of an Engine for reflect() to accept it."""
    dialect = type("D", (), {"name": "oracle", "driver": "oracledb"})()


@pytest.fixture
def docs(monkeypatch):
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", lambda engine: FakeOracleInspector())
    return reflect(FakeEngine())


@pytest.fixture
def cat(docs):
    c = Catalog(name="oracle-shaped")
    c.add_all(docs)
    c.hint("ORDERS", "sales order headers; the authoritative order record")
    c.restrict("EMP_SALARY", ["payroll"])
    return c


# --- reflection ------------------------------------------------------------

def test_system_schemas_are_skipped_and_app_schemas_kept(docs):
    owners = {d.schema for d in docs}
    assert OWNER in owners and "OTHER_APP" in owners
    assert not owners & {s.upper() for s in _SYSTEM_SCHEMAS}
    assert not any(d.name in ("DUAL", "USER$", "OBJ$") for d in docs)


def test_default_schema_is_reflected_first(docs):
    assert docs[0].schema == OWNER


def test_uppercase_names_and_oracle_types_survive(docs):
    by = {d.qname: d for d in docs}
    orders = by[f"{OWNER}.ORDERS"]
    types = {c.name: c.type for c in orders.columns}
    assert types["TOTAL_AMT"] == "NUMBER(12, 2)"
    assert types["ORDER_TS"] == "TIMESTAMP(6) WITH TIME ZONE"
    assert types["STATUS_CD"] == "VARCHAR2(10 CHAR)"
    assert by[f"{OWNER}.CUSTOMERS"].columns[4].type == "CLOB"


def test_primary_and_foreign_keys(docs):
    by = {d.qname: d for d in docs}
    assert [c.name for c in by[f"{OWNER}.ORDERS"].columns if c.pk] == ["ORDER_ID"]
    refs = {fk.ref_table for fk in by[f"{OWNER}.ORDER_LINES"].foreign_keys}
    assert refs == {"ORDERS", "PRODUCTS"}


def test_same_table_name_under_two_owners_stays_distinct(docs):
    both = [d for d in docs if d.name == "ORDERS"]
    assert {d.schema for d in both} == {OWNER, "OTHER_APP"}
    assert len(both[0].columns) != len(both[1].columns)


def test_table_and_column_comments_become_descriptions(docs):
    by = {d.qname: d for d in docs}
    assert by[f"{OWNER}.ORDERS"].description.startswith("Sales orders.")
    notes = next(c for c in by[f"{OWNER}.CUSTOMERS"].columns if c.name == "NOTES")
    assert notes.comment.startswith("Free text")


def test_view_definitions_are_reflected(docs):
    by = {d.qname: d for d in docs}
    assert "DECODE" in by[f"{OWNER}.V_OPEN_ORDERS"].definition
    assert by[f"{OWNER}.V_OPEN_ORDERS"].kind == "VIEW"


def test_oracle_functions_are_not_indexed_as_identifiers():
    """NVL, DECODE, SYSDATE and friends are noise, not concepts."""
    text = _identifiers(_VIEWS["V_OPEN_ORDERS"]).lower().split()
    for fn in ("nvl", "decode", "trunc", "sysdate", "rownum"):
        assert fn not in text, f"{fn} was indexed"
    assert "customer" in text and "status" in text


# --- selection on Oracle-shaped names --------------------------------------

def test_lowercase_questions_find_uppercase_objects(cat):
    names = {d.name for d in cat.select("revenue by month", top_k=4).objects}
    assert "V_REVENUE_BY_MONTH" in names


def test_meaning_in_view_sql_is_findable(cat):
    """'six months' and 'last sold' live only in the view's SQL."""
    names = {d.name for d in cat.select("products not sold in six months", top_k=4).objects}
    assert "V_SLOW_MOVING_STOCK" in names


def test_multiline_oracle_comment_is_rendered_on_one_line(cat):
    frag = cat.select("sales orders", top_k=2).prompt_fragment()
    assert "-- sales order headers; the authoritative order record" in frag
    assert "\nOne row per order header" not in frag


def test_backup_copy_and_mview_log_do_not_outrank(cat):
    names = [d.name for d in cat.select("order total amount", top_k=6,
                                        expand_fks=False).objects]
    assert "ORDERS" in names
    for junk in ("ORDERS_BKP", "MLOG$_ORDERS"):
        assert junk not in names or names.index("ORDERS") < names.index(junk)
    assert f"{OWNER}.ORDERS_BKP" in cat.shadows()


def test_fk_expansion_across_uppercase_names(cat):
    sel = cat.select("quantity and unit price per order line", top_k=2)
    assert "ORDERS" in {d.name for d in sel.objects}


def test_restricted_payroll_table_is_scoped(cat):
    analyst = Principal("db:ANALYST")
    sel = cat.select("annual salary by employee", top_k=10, principal=analyst)
    assert "EMP_SALARY" not in {d.name for d in sel.objects}
    assert "emp_salary" not in sel.prompt_fragment().lower()
    payroll = Principal("db:HR", roles={"payroll"})
    assert "EMP_SALARY" in {d.name for d in cat.select(
        "annual salary by employee", top_k=10, principal=payroll).objects}


def test_object_list_has_the_select_ai_shape(cat):
    ol = cat.select("revenue by month", top_k=2, expand_fks=False).object_list
    assert ol and all(set(e) == {"owner", "name"} for e in ol)
    assert ol[0]["owner"] == OWNER


def test_ddl_is_valid_looking_for_oracle_types(cat):
    frag = cat.select("customer segment", top_k=2).prompt_fragment()
    assert "VARCHAR2(200 CHAR)" in frag
    assert frag.count("(") == frag.count(")")


# --- SCHEMAGATE_CONNECT_ARGS -------------------------------------------------
# Autonomous Database cannot be reached by URL alone: the wallet directory and
# wallet password have nowhere to live in a URL. These cover the env-var route
# without needing an instance.

def test_connect_args_env_is_empty_when_unset(monkeypatch):
    from schemagate.introspect import connect_args_from_env
    monkeypatch.delenv("SCHEMAGATE_CONNECT_ARGS", raising=False)
    assert connect_args_from_env() == {}


def test_connect_args_env_is_empty_when_blank(monkeypatch):
    from schemagate.introspect import connect_args_from_env
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS", "   ")
    assert connect_args_from_env() == {}


def test_connect_args_env_parses_a_wallet_config(monkeypatch):
    from schemagate.introspect import connect_args_from_env
    monkeypatch.setenv(
        "SCHEMAGATE_CONNECT_ARGS",
        '{"config_dir": "./wallet", "wallet_location": "./wallet",'
        ' "wallet_password": "pw", "user": "ADMIN", "dsn": "db_high"}',
    )
    args = connect_args_from_env()
    assert args["config_dir"] == "./wallet"
    assert args["dsn"] == "db_high"


def test_connect_args_env_rejects_malformed_json(monkeypatch):
    import pytest
    from schemagate.introspect import connect_args_from_env
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS", "{not json}")
    with pytest.raises(ValueError, match="valid JSON"):
        connect_args_from_env()


def test_connect_args_env_rejects_a_json_array(monkeypatch):
    import pytest
    from schemagate.introspect import connect_args_from_env
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS", '["a", "b"]')
    with pytest.raises(ValueError, match="JSON object"):
        connect_args_from_env()


def test_engine_from_url_passes_env_connect_args_to_the_driver(monkeypatch):
    import sqlalchemy as sa
    from schemagate.introspect import engine_from_url
    seen = {}

    def fake_create_engine(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return "engine"

    monkeypatch.setattr(sa, "create_engine", fake_create_engine)
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS", '{"wallet_password": "pw"}')
    assert engine_from_url("oracle+oracledb://@", pool_pre_ping=True) == "engine"
    assert seen["url"] == "oracle+oracledb://@"
    assert seen["kwargs"]["connect_args"] == {"wallet_password": "pw"}
    assert seen["kwargs"]["pool_pre_ping"] is True


def test_engine_from_url_lets_explicit_connect_args_win(monkeypatch):
    import sqlalchemy as sa
    from schemagate.introspect import engine_from_url
    seen = {}

    monkeypatch.setattr(sa, "create_engine",
                        lambda url, **kw: seen.update(kw) or "engine")
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS",
                       '{"dsn": "from_env", "wallet_password": "pw"}')
    engine_from_url("oracle+oracledb://@", connect_args={"dsn": "explicit"})
    assert seen["connect_args"] == {"dsn": "explicit", "wallet_password": "pw"}


def test_engine_from_url_without_env_passes_no_connect_args(monkeypatch):
    import sqlalchemy as sa
    from schemagate.introspect import engine_from_url
    seen = {}

    monkeypatch.setattr(sa, "create_engine",
                        lambda url, **kw: seen.update(kw) or "engine")
    monkeypatch.delenv("SCHEMAGATE_CONNECT_ARGS", raising=False)
    engine_from_url("sqlite://")
    assert "connect_args" not in seen


# --- found live on Autonomous Database 26ai, 8 Sep 2026 --------------------

def _fake_engine(rows_by_sql):
    """An engine whose connect() answers exec_driver_sql from a dict."""
    class Result:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec_driver_sql(self, sql, params=None):
            for key, rows in rows_by_sql.items():
                if key in sql:
                    return Result(rows)
            raise AssertionError(f"unexpected SQL: {sql}")
    class Dialect:
        name = "oracle"
        default_schema_name = "ADMIN"
    class Engine:
        dialect = Dialect()
        def connect(self): return Conn()
    return Engine()


def test_oracle_maintained_schemas_are_excluded():
    """ADMIN on an ADB sees ~1,500 objects; only the user's own should be
    reflected. ALL_USERS.ORACLE_MAINTAINED is the signal."""
    from schemagate.dialects import vendor_maintained
    eng = _fake_engine({"oracle_maintained": [("APEX_230200",), ("ORDS_METADATA",), ("SYS",)]})
    assert vendor_maintained(eng) == {"APEX_230200", "ORDS_METADATA", "SYS"}


def test_vendor_maintained_failure_excludes_nothing():
    from schemagate.dialects import vendor_maintained
    class Boom:
        class dialect: name = "oracle"
        def connect(self): raise RuntimeError("ORA-00942")
    assert vendor_maintained(Boom()) == set()


def test_non_oracle_engines_get_no_dictionary_query():
    from schemagate.dialects import vendor_maintained, fill_unknown_types
    class PG:
        class dialect: name = "postgresql"
        def connect(self): raise AssertionError("must not connect")
    assert vendor_maintained(PG()) == set()
    cols = [{"name": "x", "type": "NULL"}]
    fill_unknown_types(PG(), None, "t", cols)
    assert cols[0]["type"] == "NULL"


def test_internal_schema_shapes_are_recognised():
    from schemagate.dialects.oracle import looks_internal as _looks_internal
    for s in ("APEX_230200", "FLOWS_FILES", "ORDS_PUBLIC_USER", "C##CLOUD$SERVICE",
              "GRAPH$METADATA", "SH$X", "DBSFWUSER", "PUBLIC",
              "ODI_REPO_USER", "OADC_CATALOG_USER", "OML$PROXY"):
        assert _looks_internal(s), s
    for s in ("SALES", "HR", "ADMIN", "MYAPP", "SH", "SSB", "OMLAPP"):
        assert not _looks_internal(s), s


def test_unknown_oracle_types_get_their_real_name():
    """XMLTYPE, JSON, SDO_GEOMETRY and VECTOR come back from SQLAlchemy as
    NULL. The prompt must show VECTOR(512, FLOAT32), not NULL."""
    from schemagate.dialects import fill_unknown_types
    eng = _fake_engine({"all_tab_columns": [
        ("EMBEDDING", "VECTOR(512, FLOAT32)", None, None, None),
        ("DOC", "XMLTYPE", None, None, None),
        ("PAYLOAD", "JSON", None, None, None),
        ("NAME", "VARCHAR2", 200, None, None),
        ("AMT", "NUMBER", 22, 12, 2),
    ]})
    cols = [{"name": "embedding", "type": "NULL"}, {"name": "doc", "type": "NULL"},
            {"name": "payload", "type": "NULL"}, {"name": "name", "type": "VARCHAR2(200 CHAR)"},
            {"name": "amt", "type": "NULL"}]
    fill_unknown_types(eng, "ADMIN", "t", cols)
    got = {c["name"]: c["type"] for c in cols}
    assert got["embedding"] == "VECTOR(512, FLOAT32)"
    assert got["doc"] == "XMLTYPE" and got["payload"] == "JSON"
    assert got["name"] == "VARCHAR2(200 CHAR)"      # known types untouched
    assert got["amt"] == "NUMBER(12,2)"


def test_unknown_type_lookup_is_skipped_when_nothing_is_unknown():
    from schemagate.dialects import fill_unknown_types
    class NoConnect:
        class dialect: name = "oracle"; default_schema_name = "ADMIN"
        def connect(self): raise AssertionError("must not query")
    cols = [{"name": "a", "type": "NUMBER"}]
    fill_unknown_types(NoConnect(), None, "t", cols)


def test_python_dash_m_schemagate_works():
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "schemagate", "--version"],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "schemagate" in (out.stdout + out.stderr).lower()


def test_oracle_store_merges_wallet_connect_args(monkeypatch):
    """OracleStore(dsn='x_high') on an ADB needs the wallet from
    SCHEMAGATE_CONNECT_ARGS; explicit user/password/dsn win over the env."""
    import sys, types
    seen = {}
    fake = types.ModuleType("oracledb")
    fake.connect = lambda **kw: seen.update(kw) or object()
    monkeypatch.setitem(sys.modules, "oracledb", fake)
    monkeypatch.setenv("SCHEMAGATE_CONNECT_ARGS",
                       '{"config_dir": "/w", "wallet_location": "/w", "wallet_password": "wp", "user": "IGNORED"}')
    from schemagate.stores.oracle import OracleStore
    OracleStore(dsn="db_high", user="ADMIN", password="pw", dim=8)
    assert seen == {"config_dir": "/w", "wallet_location": "/w", "wallet_password": "wp",
                    "user": "ADMIN", "password": "pw", "dsn": "db_high"}


def test_public_schema_is_internal_on_oracle_but_not_postgres():
    """The regression this pins: "public" was on a shared internal-schema list.
    On Oracle PUBLIC is a pseudo-schema; on PostgreSQL it is the user's entire
    database. Filtering it globally made every PostgreSQL catalog empty."""
    from schemagate.dialects import is_internal_schema

    class Eng:
        def __init__(self, name):
            self.dialect = type("d", (), {"name": name})()

    assert is_internal_schema(Eng("oracle"), "PUBLIC")
    assert is_internal_schema(Eng("oracle"), "APEX_260100")
    assert not is_internal_schema(Eng("postgresql"), "public")
    assert not is_internal_schema(Eng("postgresql"), "APEX_260100")
    assert not is_internal_schema(Eng("mysql"), "public")
    assert not is_internal_schema(Eng("sqlite"), "public")
    assert not is_internal_schema(Eng("oracle"), None)


def test_no_shared_internal_schema_list_outside_the_oracle_dialect():
    """A cross-dialect list of 'internal-looking' names is how the PostgreSQL
    catalog got emptied. Keep the notion inside the dialect that owns it."""
    import inspect as pyinspect
    from schemagate import introspect
    src = pyinspect.getsource(introspect)
    assert "_INTERNAL_PREFIXES" not in src
    assert "_looks_internal" not in src
