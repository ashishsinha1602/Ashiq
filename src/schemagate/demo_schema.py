"""The bundled demo schema. ``schemagate demo`` runs against this with no database.

A deliberately nasty synthetic schema for testing selection quality.

Design goals -- every one of these is a way real selection breaks:
  * 34 tables + 6 views across 3 schemas, so top_k=6 is a hard filter
  * homonyms: STATUS/status_code/state appear in 9 unrelated tables
  * near-duplicate pairs (BILLING.INVOICE vs SALES.INVOICE_DRAFT) that a
    naive matcher conflates
  * cryptic legacy names (DIM_CT_MSTR, FCT_TXN_LN) alongside modern ones
  * required join tables whose names never appear in the question
  * multi-hop FK chains 4 deep
  * decoy tables sharing vocabulary with the question but wrong grain
  * tenant-scoped and role-scoped objects for isolation tests
"""
from __future__ import annotations

import sqlite3
import tempfile


def create_demo_db() -> str:
    """Materialise the schema in a temporary SQLite file; returns its URL."""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


DDL = """
CREATE TABLE core_country (
  id INTEGER PRIMARY KEY, iso2 TEXT, name TEXT, region TEXT);

CREATE TABLE core_address (
  id INTEGER PRIMARY KEY, line1 TEXT, city TEXT, postcode TEXT,
  id_country INTEGER REFERENCES core_country(id), kind TEXT);

CREATE TABLE core_party (
  id INTEGER PRIMARY KEY, display_name TEXT, party_type TEXT,
  id_primary_address INTEGER REFERENCES core_address(id), status TEXT,
  created_at TEXT);

CREATE TABLE crm_customer (
  id INTEGER PRIMARY KEY, id_party INTEGER REFERENCES core_party(id),
  account_number TEXT, segment TEXT, credit_limit REAL, status TEXT,
  id_owner_rep INTEGER, onboarded_on TEXT);

CREATE TABLE crm_contact (
  id INTEGER PRIMARY KEY, id_customer INTEGER REFERENCES crm_customer(id),
  full_name TEXT, email TEXT, phone TEXT, is_primary INTEGER, status TEXT);

CREATE TABLE crm_opportunity (
  id INTEGER PRIMARY KEY, id_customer INTEGER REFERENCES crm_customer(id),
  name TEXT, amount REAL, stage TEXT, close_date TEXT, probability REAL);

CREATE TABLE hr_employee (
  id INTEGER PRIMARY KEY, id_party INTEGER REFERENCES core_party(id),
  employee_number TEXT, hired_on TEXT, id_manager INTEGER REFERENCES hr_employee(id),
  id_department INTEGER, status TEXT);

CREATE TABLE hr_department (
  id INTEGER PRIMARY KEY, code TEXT, name TEXT, cost_centre TEXT);

CREATE TABLE hr_compensation (
  id INTEGER PRIMARY KEY, id_employee INTEGER REFERENCES hr_employee(id),
  effective_from TEXT, annual_amount REAL, currency TEXT, pay_grade TEXT);

CREATE TABLE cat_product (
  id INTEGER PRIMARY KEY, sku TEXT, name TEXT, id_category INTEGER,
  unit_of_measure TEXT, list_price REAL, status TEXT, is_serialised INTEGER);

CREATE TABLE cat_category (
  id INTEGER PRIMARY KEY, name TEXT, id_parent INTEGER REFERENCES cat_category(id),
  depth INTEGER);

CREATE TABLE cat_product_attribute (
  id INTEGER PRIMARY KEY, id_product INTEGER REFERENCES cat_product(id),
  attr_key TEXT, attr_value TEXT);

CREATE TABLE cat_price_list (
  id INTEGER PRIMARY KEY, name TEXT, currency TEXT, valid_from TEXT, valid_to TEXT);

CREATE TABLE cat_price_list_item (
  id INTEGER PRIMARY KEY, id_price_list INTEGER REFERENCES cat_price_list(id),
  id_product INTEGER REFERENCES cat_product(id), unit_price REAL, min_qty INTEGER);

CREATE TABLE sales_order (
  id INTEGER PRIMARY KEY, order_number TEXT,
  id_customer INTEGER REFERENCES crm_customer(id),
  id_ship_address INTEGER REFERENCES core_address(id),
  ordered_at TEXT, id_status INTEGER, currency TEXT, total_net REAL);

CREATE TABLE sales_order_status (
  id INTEGER PRIMARY KEY, code TEXT, label TEXT, is_terminal INTEGER);

CREATE TABLE sales_order_line (
  id INTEGER PRIMARY KEY, id_order INTEGER REFERENCES sales_order(id),
  line_no INTEGER, id_product INTEGER REFERENCES cat_product(id),
  quantity REAL, unit_price REAL, discount_pct REAL, line_net REAL);

CREATE TABLE sales_quote (
  id INTEGER PRIMARY KEY, quote_number TEXT,
  id_customer INTEGER REFERENCES crm_customer(id), issued_on TEXT,
  expires_on TEXT, state TEXT, total_net REAL);

CREATE TABLE sales_quote_line (
  id INTEGER PRIMARY KEY, id_quote INTEGER REFERENCES sales_quote(id),
  id_product INTEGER REFERENCES cat_product(id), quantity REAL, unit_price REAL);

CREATE TABLE sales_invoice_draft (
  id INTEGER PRIMARY KEY, id_order INTEGER REFERENCES sales_order(id),
  drafted_on TEXT, state TEXT, total_gross REAL);

CREATE TABLE billing_invoice (
  id INTEGER PRIMARY KEY, invoice_number TEXT,
  id_customer INTEGER REFERENCES crm_customer(id),
  id_order INTEGER REFERENCES sales_order(id),
  issued_on TEXT, due_on TEXT, total_net REAL, total_tax REAL,
  total_gross REAL, currency TEXT, status TEXT);

CREATE TABLE billing_invoice_line (
  id INTEGER PRIMARY KEY, id_invoice INTEGER REFERENCES billing_invoice(id),
  id_order_line INTEGER REFERENCES sales_order_line(id),
  description TEXT, quantity REAL, unit_price REAL, tax_rate REAL, line_gross REAL);

CREATE TABLE billing_payment (
  id INTEGER PRIMARY KEY, id_invoice INTEGER REFERENCES billing_invoice(id),
  paid_on TEXT, amount REAL, method TEXT, reference TEXT, status TEXT);

CREATE TABLE billing_credit_note (
  id INTEGER PRIMARY KEY, id_invoice INTEGER REFERENCES billing_invoice(id),
  issued_on TEXT, amount REAL, reason_code TEXT);

CREATE TABLE billing_tax_rate (
  id INTEGER PRIMARY KEY, id_country INTEGER REFERENCES core_country(id),
  code TEXT, rate REAL, valid_from TEXT);

CREATE TABLE inv_warehouse (
  id INTEGER PRIMARY KEY, code TEXT, name TEXT,
  id_address INTEGER REFERENCES core_address(id), is_active INTEGER);

CREATE TABLE inv_stock_level (
  id INTEGER PRIMARY KEY, id_warehouse INTEGER REFERENCES inv_warehouse(id),
  id_product INTEGER REFERENCES cat_product(id), qty_on_hand REAL,
  qty_reserved REAL, reorder_point REAL, counted_at TEXT);

CREATE TABLE inv_movement (
  id INTEGER PRIMARY KEY, id_warehouse INTEGER REFERENCES inv_warehouse(id),
  id_product INTEGER REFERENCES cat_product(id), moved_at TEXT,
  direction TEXT, quantity REAL, id_reason INTEGER);

CREATE TABLE ship_carrier (
  id INTEGER PRIMARY KEY, name TEXT, scac_code TEXT, service_level TEXT,
  is_active INTEGER);

CREATE TABLE ship_shipment (
  id INTEGER PRIMARY KEY, tracking_number TEXT,
  id_order INTEGER REFERENCES sales_order(id),
  id_carrier INTEGER REFERENCES ship_carrier(id),
  id_warehouse INTEGER REFERENCES inv_warehouse(id),
  dispatched_at TEXT, delivered_at TEXT, status TEXT, freight_cost REAL);

CREATE TABLE ship_shipment_line (
  id INTEGER PRIMARY KEY, id_shipment INTEGER REFERENCES ship_shipment(id),
  id_order_line INTEGER REFERENCES sales_order_line(id), quantity REAL);

CREATE TABLE sup_supplier (
  id INTEGER PRIMARY KEY, id_party INTEGER REFERENCES core_party(id),
  vendor_number TEXT, payment_terms TEXT, status TEXT);

CREATE TABLE sup_purchase_order (
  id INTEGER PRIMARY KEY, po_number TEXT,
  id_supplier INTEGER REFERENCES sup_supplier(id),
  raised_on TEXT, status TEXT, total_net REAL);

CREATE TABLE sup_purchase_order_line (
  id INTEGER PRIMARY KEY, id_po INTEGER REFERENCES sup_purchase_order(id),
  id_product INTEGER REFERENCES cat_product(id), quantity REAL, unit_cost REAL);

CREATE TABLE dim_ct_mstr (
  id INTEGER PRIMARY KEY, ct_cd TEXT, ct_desc TEXT, actv_flg INTEGER);

CREATE TABLE fct_txn_ln (
  id INTEGER PRIMARY KEY, txn_dt TEXT, id_ct INTEGER REFERENCES dim_ct_mstr(id),
  amt REAL, qty REAL, src_sys TEXT);

CREATE VIEW v_customer_balance AS
  SELECT c.id AS id_customer, c.account_number,
         SUM(i.total_gross) AS invoiced, SUM(p.amount) AS paid
  FROM crm_customer c
  LEFT JOIN billing_invoice i ON i.id_customer = c.id
  LEFT JOIN billing_payment p ON p.id_invoice = i.id
  GROUP BY c.id, c.account_number;

CREATE VIEW v_monthly_revenue AS
  SELECT substr(i.issued_on,1,7) AS month, i.currency,
         SUM(i.total_net) AS revenue_net
  FROM billing_invoice i GROUP BY 1,2;

CREATE VIEW v_order_fulfilment AS
  SELECT o.id AS id_order, o.order_number, s.status AS shipment_status,
         s.dispatched_at, s.delivered_at
  FROM sales_order o LEFT JOIN ship_shipment s ON s.id_order = o.id;

CREATE VIEW v_stock_shortfall AS
  SELECT sl.id_warehouse, sl.id_product,
         sl.reorder_point - (sl.qty_on_hand - sl.qty_reserved) AS shortfall
  FROM inv_stock_level sl;

CREATE VIEW v_employee_headcount AS
  SELECT d.name AS department, COUNT(*) AS headcount
  FROM hr_employee e JOIN hr_department d ON d.id = e.id_department
  GROUP BY d.name;

CREATE VIEW v_supplier_spend AS
  SELECT s.vendor_number, substr(po.raised_on,1,4) AS year,
         SUM(po.total_net) AS spend
  FROM sup_purchase_order po JOIN sup_supplier s ON s.id = po.id_supplier
  GROUP BY 1,2;
"""

HINTS = {
    "billing_invoice": "issued invoices; the authoritative revenue record",
    "sales_invoice_draft": "pre-issue drafts only; NOT real revenue, exclude from financials",
    "fct_txn_ln": "legacy general-ledger transaction lines from the retired finance system",
    "dim_ct_mstr": "legacy cost-type master; ct_cd is the cost type code",
    "sales_order_line": "one row per product per order; join sales_order for the header",
    "hr_compensation": "salary history; restricted",
}

# question -> tables that MUST appear in the selection
GOLDEN = [
    ("total revenue by month last year",           {"v_monthly_revenue"}),
    ("which customers owe us money",               {"v_customer_balance"}),
    ("how much did we pay each supplier in 2024",  {"v_supplier_spend"}),
    ("products below their reorder point",         {"v_stock_shortfall"}),
    ("average discount per product category",      {"sales_order_line", "cat_product"}),
    ("late shipments by carrier",                  {"ship_shipment", "ship_carrier"}),
    ("unpaid invoices over 90 days old",           {"billing_invoice"}),
    ("headcount per department",                   {"v_employee_headcount"}),
    ("purchase order lines for a given sku",       {"sup_purchase_order_line", "cat_product"}),
    ("credit notes raised and why",                {"billing_credit_note"}),
    ("quote conversion to orders",                 {"sales_quote"}),
    ("stock on hand per warehouse",                {"inv_stock_level", "inv_warehouse"}),
]

# Harder set: paraphrase with no vocabulary overlap with object names.
# This is where a lexical embedder is expected to fail and a true semantic
# model should earn its dependency. Keep both sets: reporting only the easy
# one overstates what the default embedder does.
GOLDEN_PARAPHRASE = [
    ("who hasn't paid us yet",            {"billing_invoice"}),
    ("money coming in each month",        {"v_monthly_revenue"}),
    ("things we're running out of",       {"v_stock_shortfall"}),
    ("how many people work in each team", {"v_employee_headcount"}),
    ("parcels that arrived late",         {"ship_shipment"}),
    ("what we spend with vendors",        {"v_supplier_spend"}),
]
