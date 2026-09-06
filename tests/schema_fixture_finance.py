"""A bank's general ledger and trading book, built so grain and role confuse.

What makes finance schemas hard is not size but *near-synonyms with different
meaning*: a journal entry and a ledger line, a trade and a position, a
counterparty and a customer, booked and settled. Every one of those pairs is
here, plus:

  * the ledger at three grains: journal header, journal line, daily balance
  * positions (what we hold) versus trades (what we did) versus settlements
    (what actually moved) -- three tables that all mention amount and date
  * the same entity twice: dim_counterparty (canonical) and legacy cpty_mstr
  * FX rates as both a daily table and a point-in-time view
  * regulatory tables (KYC, AML alerts, sanctions hits) that most analysts
    must never see in a prompt, restricted by role
  * the usual _bkp, _old, _v2 and stg_ copies

Synthetic and invented. Not derived from any real institution's schema.
"""
from __future__ import annotations

DDL = """
-- ============ reference ============

CREATE TABLE ref_chart_of_accounts (
  account_id INTEGER PRIMARY KEY, account_code TEXT, account_name TEXT,
  account_type TEXT, parent_account_id INTEGER REFERENCES ref_chart_of_accounts(account_id),
  is_posting INTEGER, currency TEXT);

CREATE TABLE ref_currency (
  currency TEXT PRIMARY KEY, name TEXT, minor_units INTEGER);

CREATE TABLE ref_legal_entity (
  entity_id INTEGER PRIMARY KEY, entity_code TEXT, name TEXT, jurisdiction TEXT,
  functional_currency TEXT REFERENCES ref_currency(currency));

CREATE TABLE ref_cost_centre (
  cost_centre_id INTEGER PRIMARY KEY, code TEXT, name TEXT,
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id));

CREATE TABLE ref_instrument (
  instrument_id INTEGER PRIMARY KEY, isin TEXT, ticker TEXT, name TEXT,
  asset_class TEXT, currency TEXT REFERENCES ref_currency(currency),
  maturity_date TEXT);

CREATE TABLE dim_counterparty (
  counterparty_id INTEGER PRIMARY KEY, lei TEXT, name TEXT, country TEXT,
  sector TEXT, rating TEXT, is_active INTEGER);

CREATE TABLE cpty_mstr (
  cpty_id INTEGER PRIMARY KEY, cpty_nm TEXT, ctry_cd TEXT, actv_flg INTEGER);

CREATE TABLE dim_customer (
  customer_id INTEGER PRIMARY KEY, customer_number TEXT, name TEXT,
  segment TEXT, relationship_manager TEXT, onboarded_on TEXT);

-- ============ general ledger, three grains ============

CREATE TABLE gl_journal_header (
  journal_id INTEGER PRIMARY KEY, journal_number TEXT,
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id),
  posted_on TEXT, period TEXT, source_system TEXT, description TEXT,
  status TEXT, created_by TEXT);

CREATE TABLE gl_journal_line (
  line_id INTEGER PRIMARY KEY,
  journal_id INTEGER REFERENCES gl_journal_header(journal_id),
  line_no INTEGER,
  account_id INTEGER REFERENCES ref_chart_of_accounts(account_id),
  cost_centre_id INTEGER REFERENCES ref_cost_centre(cost_centre_id),
  debit_amount REAL, credit_amount REAL,
  currency TEXT REFERENCES ref_currency(currency),
  functional_amount REAL, memo TEXT);

CREATE TABLE gl_daily_balance (
  balance_id INTEGER PRIMARY KEY, balance_date TEXT,
  account_id INTEGER REFERENCES ref_chart_of_accounts(account_id),
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id),
  opening_balance REAL, debits REAL, credits REAL, closing_balance REAL);

CREATE TABLE gl_period (
  period TEXT PRIMARY KEY, fiscal_year INTEGER, start_date TEXT, end_date TEXT,
  is_closed INTEGER, closed_on TEXT);

-- ============ trading book: did, hold, moved ============

CREATE TABLE trd_trade (
  trade_id INTEGER PRIMARY KEY, trade_ref TEXT,
  instrument_id INTEGER REFERENCES ref_instrument(instrument_id),
  counterparty_id INTEGER REFERENCES dim_counterparty(counterparty_id),
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id),
  trade_date TEXT, value_date TEXT, side TEXT, quantity REAL, price REAL,
  notional REAL, currency TEXT, trader TEXT, status TEXT);

CREATE TABLE trd_position (
  position_id INTEGER PRIMARY KEY, as_of_date TEXT,
  instrument_id INTEGER REFERENCES ref_instrument(instrument_id),
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id),
  book TEXT, quantity REAL, average_cost REAL, market_value REAL,
  unrealised_pnl REAL);

CREATE TABLE trd_settlement (
  settlement_id INTEGER PRIMARY KEY,
  trade_id INTEGER REFERENCES trd_trade(trade_id),
  settled_on TEXT, amount REAL, currency TEXT, status TEXT, fail_reason TEXT);

CREATE TABLE trd_pnl_daily (
  pnl_id INTEGER PRIMARY KEY, pnl_date TEXT, book TEXT,
  entity_id INTEGER REFERENCES ref_legal_entity(entity_id),
  realised REAL, unrealised REAL, fees REAL, total REAL);

-- ============ fx ============

CREATE TABLE fx_rate_daily (
  rate_id INTEGER PRIMARY KEY, rate_date TEXT,
  from_currency TEXT REFERENCES ref_currency(currency),
  to_currency TEXT REFERENCES ref_currency(currency),
  mid_rate REAL, source TEXT);

-- ============ payments and lending ============

CREATE TABLE pay_payment (
  payment_id INTEGER PRIMARY KEY, payment_ref TEXT,
  customer_id INTEGER REFERENCES dim_customer(customer_id),
  initiated_on TEXT, executed_on TEXT, amount REAL, currency TEXT,
  channel TEXT, status TEXT, return_code TEXT);

CREATE TABLE lnd_loan (
  loan_id INTEGER PRIMARY KEY, loan_number TEXT,
  customer_id INTEGER REFERENCES dim_customer(customer_id),
  product TEXT, principal REAL, interest_rate REAL, originated_on TEXT,
  maturity_on TEXT, status TEXT);

CREATE TABLE lnd_repayment_schedule (
  schedule_id INTEGER PRIMARY KEY,
  loan_id INTEGER REFERENCES lnd_loan(loan_id),
  due_on TEXT, principal_due REAL, interest_due REAL, paid_on TEXT,
  days_past_due INTEGER);

CREATE TABLE lnd_provision (
  provision_id INTEGER PRIMARY KEY,
  loan_id INTEGER REFERENCES lnd_loan(loan_id),
  as_of_date TEXT, stage INTEGER, expected_credit_loss REAL);

-- ============ regulatory: restricted ============

CREATE TABLE kyc_review (
  review_id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES dim_customer(customer_id),
  reviewed_on TEXT, risk_rating TEXT, pep_flag INTEGER, outcome TEXT,
  reviewer TEXT);

CREATE TABLE aml_alert (
  alert_id INTEGER PRIMARY KEY,
  customer_id INTEGER REFERENCES dim_customer(customer_id),
  raised_on TEXT, scenario TEXT, score REAL, disposition TEXT, analyst TEXT);

CREATE TABLE aml_sanctions_hit (
  hit_id INTEGER PRIMARY KEY,
  payment_id INTEGER REFERENCES pay_payment(payment_id),
  list_name TEXT, matched_name TEXT, match_score REAL, cleared INTEGER);

-- ============ the accumulated junk ============

CREATE TABLE gl_journal_line_bkp (
  line_id INTEGER PRIMARY KEY, journal_id INTEGER, account_id INTEGER,
  debit_amount REAL, credit_amount REAL);

CREATE TABLE gl_journal_line_v2 (
  line_id INTEGER PRIMARY KEY, journal_id INTEGER, account_id INTEGER,
  debit_amount REAL, credit_amount REAL, migration_batch TEXT);

CREATE TABLE trd_trade_old (
  trade_id INTEGER PRIMARY KEY, trade_ref TEXT, instrument_id INTEGER,
  quantity REAL, price REAL);

CREATE TABLE trd_position_tmp (
  position_id INTEGER, instrument_id INTEGER, quantity REAL);

CREATE TABLE stg_trade (
  trade_ref TEXT, isin TEXT, lei TEXT, trade_date TEXT, quantity REAL,
  price REAL, load_batch TEXT);

CREATE TABLE stg_payment (
  payment_ref TEXT, customer_number TEXT, amount REAL, currency TEXT,
  load_batch TEXT);

CREATE TABLE dim_customer_old (
  customer_id INTEGER PRIMARY KEY, name TEXT, segment TEXT);

-- ============ views ============

CREATE VIEW v_trial_balance AS
  SELECT b.balance_date, c.account_code, c.account_name, c.account_type,
         SUM(b.closing_balance) AS balance
  FROM gl_daily_balance b JOIN ref_chart_of_accounts c ON c.account_id = b.account_id
  GROUP BY b.balance_date, c.account_code, c.account_name, c.account_type;

CREATE VIEW v_unbalanced_journals AS
  SELECT h.journal_number, SUM(l.debit_amount) AS debits, SUM(l.credit_amount) AS credits
  FROM gl_journal_header h JOIN gl_journal_line l ON l.journal_id = h.journal_id
  GROUP BY h.journal_number
  HAVING ABS(SUM(l.debit_amount) - SUM(l.credit_amount)) > 0.005;

CREATE VIEW v_fx_rate_as_of AS
  SELECT from_currency, to_currency, rate_date, mid_rate,
         LEAD(rate_date) OVER (PARTITION BY from_currency, to_currency ORDER BY rate_date) AS valid_until
  FROM fx_rate_daily;

CREATE VIEW v_failed_settlements AS
  SELECT s.settlement_id, t.trade_ref, s.amount, s.currency, s.fail_reason
  FROM trd_settlement s JOIN trd_trade t ON t.trade_id = s.trade_id
  WHERE s.status = 'FAILED';

CREATE VIEW v_counterparty_exposure AS
  SELECT cp.name AS counterparty, cp.rating, SUM(p.market_value) AS exposure
  FROM trd_position p
  JOIN trd_trade t ON t.instrument_id = p.instrument_id
  JOIN dim_counterparty cp ON cp.counterparty_id = t.counterparty_id
  GROUP BY cp.name, cp.rating;

CREATE VIEW v_loans_in_arrears AS
  SELECT l.loan_number, c.name AS customer, MAX(r.days_past_due) AS days_past_due,
         SUM(r.principal_due + r.interest_due) AS overdue_amount
  FROM lnd_loan l
  JOIN lnd_repayment_schedule r ON r.loan_id = l.loan_id
  JOIN dim_customer c ON c.customer_id = l.customer_id
  WHERE r.paid_on IS NULL AND r.days_past_due > 0
  GROUP BY l.loan_number, c.name;

CREATE VIEW v_expected_credit_loss AS
  SELECT p.as_of_date, p.stage, SUM(p.expected_credit_loss) AS ecl
  FROM lnd_provision p GROUP BY p.as_of_date, p.stage;

CREATE VIEW v_payment_returns AS
  SELECT channel, return_code, COUNT(*) AS returned
  FROM pay_payment WHERE return_code IS NOT NULL
  GROUP BY channel, return_code;
"""

HINTS = {
    "gl_journal_line": "one row per ledger posting line; the base grain for debits and credits",
    "gl_journal_header": "one row per journal; who posted it, when, from which system",
    "gl_daily_balance": "closing balance per account per day; use for balances, not for postings",
    "trd_trade": "what we did: executed trades, one row each",
    "trd_position": "what we hold: end-of-day positions by instrument and book",
    "trd_settlement": "what actually moved: cash and securities settlement per trade",
    "dim_counterparty": "canonical counterparty dimension; cpty_mstr is the retired legacy copy",
    "fx_rate_daily": "one mid rate per currency pair per day",
    "v_fx_rate_as_of": "rates with validity ranges; use for point-in-time conversion",
}

GOLDEN = [
    # grain: postings vs balances vs journals
    ("debits and credits by account",                 {"gl_journal_line"}),
    ("closing balance per account per day",           {"gl_daily_balance"}),
    ("who posted each journal and from which system", {"gl_journal_header"}),
    ("trial balance by account type",                 {"v_trial_balance"}),
    ("journals that do not balance",                  {"v_unbalanced_journals"}),
    # did / hold / moved
    ("trades executed by each trader",                {"trd_trade"}),
    ("end of day positions and unrealised pnl",       {"trd_position"}),
    ("settlements that failed and why",               {"v_failed_settlements"}),
    ("daily realised and unrealised pnl by book",     {"trd_pnl_daily"}),
    ("exposure to each counterparty by rating",       {"v_counterparty_exposure"}),
    # fx
    ("exchange rate valid on a given date",           {"v_fx_rate_as_of"}),
    # lending
    ("loans in arrears and overdue amount",           {"v_loans_in_arrears"}),
    ("expected credit loss by stage",                 {"v_expected_credit_loss"}),
    ("repayment schedule and days past due",          {"lnd_repayment_schedule"}),
    # payments
    ("returned payments by channel and reason",       {"v_payment_returns"}),
]

DECOYS = [
    ("debits and credits by account",        "gl_journal_line", "gl_journal_line_bkp"),
    ("debits and credits by account",        "gl_journal_line", "gl_journal_line_v2"),
    ("trades executed by each trader",       "trd_trade",       "trd_trade_old"),
    ("trades executed by each trader",       "trd_trade",       "stg_trade"),
    ("end of day positions and unrealised pnl", "trd_position", "trd_position_tmp"),
    ("counterparty name country and rating", "dim_counterparty", "cpty_mstr"),
    ("customer segment and relationship manager", "dim_customer", "dim_customer_old"),
]

#: Questions in the language of the business, not the ledger.
GOLDEN_NEEDS_DESCRIPTIONS = [
    ("what do we owe and what is owed to us, by account",  {"gl_daily_balance", "v_trial_balance"}),
    ("deals that never actually completed",                {"v_failed_settlements"}),
    ("how much are we on the hook for with each bank",     {"v_counterparty_exposure"}),
    ("customers behind on their loan payments",            {"v_loans_in_arrears"}),
]

RESTRICTED = {
    "kyc_review": ["compliance"],
    "aml_alert": ["compliance"],
    "aml_sanctions_hit": ["compliance"],
}
