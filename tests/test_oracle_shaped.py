"""Oracle-shaped metadata through the real reflection code, no server needed.

The live Oracle run still needs an instance (``ASHIQ_ORACLE_URL``). What
can be tested without one is everything ashiq does *with* what Oracle's
SQLAlchemy dialect hands back: uppercase owners and names, ``VARCHAR2(100
CHAR)`` and ``NUMBER(10, 2)`` type strings, ``DATE``/``TIMESTAMP(6)``/``CLOB``,
system schemas that must be skipped, view definitions full of ``NVL``,
``DECODE`` and ``SYSDATE``, and the object_list shape Select AI expects.

A fake Inspector returns exactly that, and ``ashiq.introspect.reflect`` is
run against it unchanged.
"""
import pytest

from ashiq import Catalog, Principal
from ashiq.introspect import _SYSTEM_SCHEMAS, reflect
from ashiq.models import _identifiers

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
