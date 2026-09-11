"""Reading a catalog over HTTPS, because 1522 is not always available.

This path exists because of a real machine: a laptop that could load Database
Actions in a browser and could not open a SQL*Net connection to the same
database. The driver called that "cannot connect to database" and then, after
a proxy got involved, "the database or network closed the connection" -- two
sentences that blame the database for a firewall.
"""
import urllib.error

import pytest

from schemagate.ords import OrdsError, ords_base, reflect_ords

# ---- the URL people actually have ---------------------------------------

@pytest.mark.parametrize("given,want", [
    ("https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords/sql-developer",
     "https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords"),
    ("https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords/",
     "https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords"),
    ("https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords/sql-developer?p=1",
     "https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords"),
    ("https://x-db.adb.us-phoenix-1.oraclecloudapps.com",
     "https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords"),
    ("x-db.adb.us-phoenix-1.oraclecloudapps.com/ords/sql-developer",
     "https://x-db.adb.us-phoenix-1.oraclecloudapps.com/ords"),
])
def test_whatever_they_paste_becomes_the_ords_root(given, want):
    """The link they have is from the wallet README or the address bar of
    Database Actions. Asking them to construct a base URL from it is asking
    them to get it wrong."""
    assert ords_base(given) == want


def test_an_empty_url_is_refused():
    with pytest.raises(OrdsError):
        ords_base("")


# ---- parsing a real ORDS answer -----------------------------------------

def _rs(cols, rows):
    return {"resultSet": {"metadata": [{"jsonColumnName": c} for c in cols],
                          "items": [dict(zip(cols, r)) for r in rows]}}


#: shaped exactly like the payload a live Autonomous Database returned
_PAYLOAD = {"items": [
    _rs(["name", "kind"], [["SALES_ORDER", "TABLE"], ["CRM_CUSTOMER", "TABLE"]]),
    _rs(["name", "kind"], [["V_MONTHLY_REVENUE", "VIEW"]]),
    _rs(["table_name", "column_name", "data_type", "data_length",
         "data_precision", "data_scale", "nullable", "column_id"], [
        ["SALES_ORDER", "ID", "NUMBER", 22, None, None, "N", 1],
        ["SALES_ORDER", "ORDER_NUMBER", "VARCHAR2", 40, None, None, "N", 2],
        ["SALES_ORDER", "TOTAL_NET", "NUMBER", 22, 14, 2, "Y", 3],
        ["SALES_ORDER", "ID_CUSTOMER", "NUMBER", 22, None, None, "N", 4],
        ["CRM_CUSTOMER", "ID", "NUMBER", 22, None, None, "N", 1],
        ["V_MONTHLY_REVENUE", "MONTH", "VARCHAR2", 7, None, None, "Y", 1],
        ["NOT_MINE", "X", "NUMBER", 22, None, None, "Y", 1],
    ]),
    _rs(["table_name", "column_name"], [["SALES_ORDER", "ID"], ["CRM_CUSTOMER", "ID"]]),
    _rs(["table_name", "column_name", "ref_table", "ref_col"],
        [["SALES_ORDER", "ID_CUSTOMER", "CRM_CUSTOMER", "ID"]]),
    _rs(["table_name", "comments"], [["SALES_ORDER", "orders placed by customers"]]),
    _rs(["table_name", "column_name", "comments"],
        [["SALES_ORDER", "TOTAL_NET", "net of tax"]]),
]}


@pytest.fixture
def docs(monkeypatch):
    import schemagate.ords as m
    monkeypatch.setattr(m, "_post", lambda *a, **k: _PAYLOAD["items"])
    return reflect_ords("https://x/ords", schema="APPUSER",
                        user="appuser", password="pw")


def test_tables_and_views_both_arrive(docs):
    by = {d.name: d for d in docs}
    assert set(by) == {"SALES_ORDER", "CRM_CUSTOMER", "V_MONTHLY_REVENUE"}
    assert by["V_MONTHLY_REVENUE"].kind == "VIEW"
    assert by["SALES_ORDER"].kind == "TABLE"


def test_types_render_as_they_would_over_sqlnet(docs):
    """A catalog read over HTTPS must not describe a column differently from
    the same catalog read through the driver."""
    cols = {c.name: c.type for c in next(d for d in docs if d.name == "SALES_ORDER").columns}
    assert cols["ORDER_NUMBER"] == "VARCHAR2(40)"
    assert cols["TOTAL_NET"] == "NUMBER(14,2)"
    assert cols["ID"] == "NUMBER"


def test_nullability_primary_keys_and_comments(docs):
    so = next(d for d in docs if d.name == "SALES_ORDER")
    by = {c.name: c for c in so.columns}
    assert by["ID"].pk is True and by["ID"].nullable is False
    assert by["TOTAL_NET"].nullable is True
    assert by["TOTAL_NET"].comment == "net of tax"
    assert so.description == "orders placed by customers"


def test_foreign_keys_survive(docs):
    so = next(d for d in docs if d.name == "SALES_ORDER")
    assert len(so.foreign_keys) == 1
    fk = so.foreign_keys[0]
    assert fk.columns == ["ID_CUSTOMER"]
    assert fk.ref_table == "CRM_CUSTOMER" and fk.ref_columns == ["ID"]


def test_columns_of_objects_we_did_not_ask_for_are_dropped(docs):
    """USER_TAB_COLUMNS carries rows for things that are neither a table nor a
    view we listed. Keeping them would invent objects."""
    assert not any(d.name == "NOT_MINE" for d in docs)


def test_the_catalog_is_usable_end_to_end(docs):
    from schemagate import Catalog

    cat = Catalog()
    cat.add_all(docs)
    cat.index()
    sel = cat.select("orders by customer", top_k=3)
    assert sel.hits
    assert "SALES_ORDER" in sel.prompt_fragment()


# ---- failures say what to do about them ---------------------------------

def _http_error(code):
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", code, "no", {}, None)
    return boom


def test_a_bad_password_says_so(monkeypatch):
    import schemagate.ords as m
    monkeypatch.setattr(m.urllib.request, "urlopen", _http_error(401))
    with pytest.raises(OrdsError) as e:
        reflect_ords("https://x/ords", "APPUSER", "appuser", "wrong")
    assert "401" in str(e.value) and "password" in str(e.value)


def test_a_schema_that_is_not_rest_enabled_says_how_to_enable_it(monkeypatch):
    """404 here means the endpoint does not exist, and the reason is almost
    always this one. The remedy is one statement; printing it saves a search."""
    import schemagate.ords as m
    monkeypatch.setattr(m.urllib.request, "urlopen", _http_error(404))
    with pytest.raises(OrdsError) as e:
        reflect_ords("https://x/ords", "APPUSER", "appuser", "pw")
    assert "ORDS_ADMIN.ENABLE_SCHEMA" in str(e.value)
    assert "APPUSER" in str(e.value)


def test_a_statement_error_does_not_leak_the_statement(monkeypatch):
    import schemagate.ords as m
    monkeypatch.setattr(m, "_post", lambda *a, **k: (_ for _ in ()).throw(
        OrdsError("ORA-00942: table or view does not exist")))
    with pytest.raises(OrdsError) as e:
        reflect_ords("https://x/ords", "APPUSER", "appuser", "pw")
    assert "ORA-00942" in str(e.value)


def test_html_instead_of_json_is_a_useful_message(monkeypatch):
    import io

    import schemagate.ords as m
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda *a, **k: _ctx(io.BytesIO(b"<html>sign in</html>")))
    with pytest.raises(OrdsError) as e:
        reflect_ords("https://x/ords", "APPUSER", "appuser", "pw")
    assert "/ords" in str(e.value)


class _ctx:
    def __init__(self, fh): self.fh = fh
    def __enter__(self): return self.fh
    def __exit__(self, *a): return False
    def read(self, *a): return self.fh.read(*a)


def test_a_schema_is_required(monkeypatch):
    import schemagate.ords as m
    monkeypatch.setattr(m, "_post", lambda *a, **k: [])
    with pytest.raises(OrdsError):
        reflect_ords("https://x/ords", schema="", user="", password="pw")
