"""A healthcare claims star schema built to make table choice genuinely hard.

The other fixtures test whether selection can find a table. This one tests
whether it can find the *right* one when several plausible ones exist -- the
situation in every real data warehouse:

  * the same fact at four grains: claim line, claim header, monthly
    aggregate, member-month (PMPM). A "total paid by month" question is
    answerable from three of them; only one is right for "paid per line"
  * slowly-changing dimensions: dim_member is the current row, and
    dim_member_history holds every version. "Address at time of service"
    needs the history table; "current address" needs the current one
  * role-playing dates: one dim_date joined as service date, paid date,
    admit date and discharge date. The fact carries four date keys
  * the same entity under two names (dim_provider vs dim_physician,
    dim_payer vs dim_plan_sponsor) -- one is canonical, one is a leftover
  * bridge tables for many-to-many (claim <-> diagnosis), which a question
    about "diagnoses on a claim" needs and never names
  * _bkp, _old, _v2, _tmp and _new copies of the important facts and
    dims, exactly as a real warehouse accumulates them
  * views at several levels of abstraction over the same facts

Everything is synthetic and invented. It models a generic payer claims
warehouse and is not derived from any real system.
"""
from __future__ import annotations

DDL = """
-- ============ conformed dimensions ============

CREATE TABLE dim_date (
  date_key INTEGER PRIMARY KEY, full_date TEXT, year INTEGER, quarter INTEGER,
  month INTEGER, month_name TEXT, day_of_week TEXT, is_weekend INTEGER,
  fiscal_year INTEGER, fiscal_period INTEGER);

CREATE TABLE dim_member (
  member_key INTEGER PRIMARY KEY, member_id TEXT, birth_year INTEGER, sex TEXT,
  city TEXT, state TEXT, zip TEXT, plan_tier TEXT, risk_score REAL,
  effective_from TEXT, is_current INTEGER);

CREATE TABLE dim_member_history (
  member_history_key INTEGER PRIMARY KEY, member_id TEXT, birth_year INTEGER,
  sex TEXT, city TEXT, state TEXT, zip TEXT, plan_tier TEXT, risk_score REAL,
  valid_from TEXT, valid_to TEXT, version_no INTEGER, is_current INTEGER);

CREATE TABLE dim_provider (
  provider_key INTEGER PRIMARY KEY, npi TEXT, last_name TEXT, first_name TEXT,
  specialty TEXT, taxonomy_code TEXT, network_status TEXT, city TEXT, state TEXT);

CREATE TABLE dim_physician (
  physician_key INTEGER PRIMARY KEY, npi TEXT, full_name TEXT, specialty TEXT,
  state TEXT);

CREATE TABLE dim_facility (
  facility_key INTEGER PRIMARY KEY, facility_id TEXT, name TEXT,
  facility_type TEXT, bed_count INTEGER, city TEXT, state TEXT);

CREATE TABLE dim_payer (
  payer_key INTEGER PRIMARY KEY, payer_id TEXT, payer_name TEXT,
  line_of_business TEXT, plan_type TEXT);

CREATE TABLE dim_plan_sponsor (
  sponsor_key INTEGER PRIMARY KEY, sponsor_id TEXT, sponsor_name TEXT,
  segment TEXT);

CREATE TABLE dim_diagnosis (
  diagnosis_key INTEGER PRIMARY KEY, icd_code TEXT, description TEXT,
  chapter TEXT, is_chronic INTEGER, hcc_category TEXT);

CREATE TABLE dim_procedure (
  procedure_key INTEGER PRIMARY KEY, cpt_code TEXT, description TEXT,
  category TEXT, rvu REAL);

CREATE TABLE dim_drug (
  drug_key INTEGER PRIMARY KEY, ndc TEXT, label_name TEXT, generic_name TEXT,
  therapeutic_class TEXT, is_specialty INTEGER);

CREATE TABLE dim_place_of_service (
  pos_key INTEGER PRIMARY KEY, pos_code TEXT, description TEXT);

CREATE TABLE dim_claim_status (
  status_key INTEGER PRIMARY KEY, status_code TEXT, description TEXT,
  is_final INTEGER);

CREATE TABLE dim_denial_reason (
  denial_key INTEGER PRIMARY KEY, reason_code TEXT, description TEXT,
  is_appealable INTEGER);

-- ============ facts at different grains ============

CREATE TABLE fact_claim_line (
  claim_line_key INTEGER PRIMARY KEY, claim_id TEXT, line_no INTEGER,
  member_key INTEGER REFERENCES dim_member(member_key),
  member_history_key INTEGER REFERENCES dim_member_history(member_history_key),
  provider_key INTEGER REFERENCES dim_provider(provider_key),
  facility_key INTEGER REFERENCES dim_facility(facility_key),
  payer_key INTEGER REFERENCES dim_payer(payer_key),
  procedure_key INTEGER REFERENCES dim_procedure(procedure_key),
  pos_key INTEGER REFERENCES dim_place_of_service(pos_key),
  status_key INTEGER REFERENCES dim_claim_status(status_key),
  service_date_key INTEGER REFERENCES dim_date(date_key),
  paid_date_key INTEGER REFERENCES dim_date(date_key),
  received_date_key INTEGER REFERENCES dim_date(date_key),
  units REAL, billed_amount REAL, allowed_amount REAL, paid_amount REAL,
  member_responsibility REAL, copay REAL, deductible REAL, coinsurance REAL);

CREATE TABLE fact_claim_header (
  claim_key INTEGER PRIMARY KEY, claim_id TEXT,
  member_key INTEGER REFERENCES dim_member(member_key),
  payer_key INTEGER REFERENCES dim_payer(payer_key),
  status_key INTEGER REFERENCES dim_claim_status(status_key),
  received_date_key INTEGER REFERENCES dim_date(date_key),
  adjudicated_date_key INTEGER REFERENCES dim_date(date_key),
  line_count INTEGER, total_billed REAL, total_allowed REAL, total_paid REAL,
  days_to_adjudicate INTEGER);

CREATE TABLE fact_claim_monthly_agg (
  agg_key INTEGER PRIMARY KEY, year INTEGER, month INTEGER,
  payer_key INTEGER REFERENCES dim_payer(payer_key),
  provider_key INTEGER REFERENCES dim_provider(provider_key),
  claim_count INTEGER, line_count INTEGER, total_billed REAL,
  total_allowed REAL, total_paid REAL);

CREATE TABLE fact_member_month (
  member_month_key INTEGER PRIMARY KEY,
  member_key INTEGER REFERENCES dim_member(member_key),
  payer_key INTEGER REFERENCES dim_payer(payer_key),
  month_date_key INTEGER REFERENCES dim_date(date_key),
  is_eligible INTEGER, premium_amount REAL, capitation_amount REAL,
  risk_adjusted_amount REAL);

CREATE TABLE fact_encounter (
  encounter_key INTEGER PRIMARY KEY, encounter_id TEXT,
  member_key INTEGER REFERENCES dim_member(member_key),
  facility_key INTEGER REFERENCES dim_facility(facility_key),
  attending_provider_key INTEGER REFERENCES dim_provider(provider_key),
  admit_date_key INTEGER REFERENCES dim_date(date_key),
  discharge_date_key INTEGER REFERENCES dim_date(date_key),
  encounter_type TEXT, length_of_stay INTEGER, drg_code TEXT,
  is_readmission INTEGER, readmit_days INTEGER);

CREATE TABLE fact_pharmacy_claim (
  rx_claim_key INTEGER PRIMARY KEY, rx_number TEXT,
  member_key INTEGER REFERENCES dim_member(member_key),
  prescriber_key INTEGER REFERENCES dim_provider(provider_key),
  drug_key INTEGER REFERENCES dim_drug(drug_key),
  fill_date_key INTEGER REFERENCES dim_date(date_key),
  days_supply INTEGER, quantity REAL, ingredient_cost REAL,
  dispensing_fee REAL, paid_amount REAL, copay REAL);

CREATE TABLE fact_denial (
  denial_fact_key INTEGER PRIMARY KEY,
  claim_line_key INTEGER REFERENCES fact_claim_line(claim_line_key),
  denial_key INTEGER REFERENCES dim_denial_reason(denial_key),
  denied_date_key INTEGER REFERENCES dim_date(date_key),
  appealed INTEGER, overturned INTEGER, recovered_amount REAL);

CREATE TABLE fact_quality_measure (
  measure_fact_key INTEGER PRIMARY KEY, measure_code TEXT,
  provider_key INTEGER REFERENCES dim_provider(provider_key),
  period_date_key INTEGER REFERENCES dim_date(date_key),
  numerator INTEGER, denominator INTEGER, rate REAL);

-- ============ bridges ============

CREATE TABLE bridge_claim_diagnosis (
  claim_line_key INTEGER REFERENCES fact_claim_line(claim_line_key),
  diagnosis_key INTEGER REFERENCES dim_diagnosis(diagnosis_key),
  sequence_no INTEGER, is_primary INTEGER,
  PRIMARY KEY (claim_line_key, diagnosis_key));

CREATE TABLE bridge_member_condition (
  member_key INTEGER REFERENCES dim_member(member_key),
  diagnosis_key INTEGER REFERENCES dim_diagnosis(diagnosis_key),
  first_seen_date_key INTEGER REFERENCES dim_date(date_key),
  PRIMARY KEY (member_key, diagnosis_key));

CREATE TABLE bridge_provider_facility (
  provider_key INTEGER REFERENCES dim_provider(provider_key),
  facility_key INTEGER REFERENCES dim_facility(facility_key),
  affiliation_type TEXT,
  PRIMARY KEY (provider_key, facility_key));

-- ============ the accumulated junk ============

CREATE TABLE fact_claim_line_bkp (
  claim_line_key INTEGER PRIMARY KEY, claim_id TEXT, line_no INTEGER,
  member_key INTEGER, provider_key INTEGER, service_date_key INTEGER,
  paid_amount REAL, backup_taken_on TEXT);

CREATE TABLE fact_claim_line_old (
  claim_line_key INTEGER PRIMARY KEY, claim_id TEXT, line_no INTEGER,
  member_key INTEGER, provider_key INTEGER, service_date_key INTEGER,
  paid_amount REAL);

CREATE TABLE fact_claim_line_v2 (
  claim_line_key INTEGER PRIMARY KEY, claim_id TEXT, line_no INTEGER,
  member_key INTEGER, provider_key INTEGER, service_date_key INTEGER,
  paid_amount REAL, allowed_amount REAL, migration_batch TEXT);

CREATE TABLE fact_claim_line_tmp (
  claim_line_key INTEGER, claim_id TEXT, paid_amount REAL);

CREATE TABLE fact_claim_header_new (
  claim_key INTEGER PRIMARY KEY, claim_id TEXT, member_key INTEGER,
  total_paid REAL, loaded_at TEXT);

CREATE TABLE dim_member_bkp (
  member_key INTEGER PRIMARY KEY, member_id TEXT, city TEXT, state TEXT,
  plan_tier TEXT);

CREATE TABLE dim_member_v2 (
  member_key INTEGER PRIMARY KEY, member_id TEXT, birth_year INTEGER,
  sex TEXT, city TEXT, state TEXT, zip TEXT, plan_tier TEXT);

CREATE TABLE dim_member_old (
  member_key INTEGER PRIMARY KEY, member_id TEXT, city TEXT, state TEXT);

CREATE TABLE dim_provider_old (
  provider_key INTEGER PRIMARY KEY, npi TEXT, last_name TEXT, specialty TEXT);

CREATE TABLE dim_provider_tmp (
  provider_key INTEGER, npi TEXT);

CREATE TABLE dim_date_bkp (
  date_key INTEGER PRIMARY KEY, full_date TEXT, year INTEGER, month INTEGER);

CREATE TABLE stg_claim_line (
  claim_id TEXT, line_no INTEGER, member_id TEXT, npi TEXT, cpt_code TEXT,
  service_date TEXT, paid_amount REAL, load_batch TEXT);

CREATE TABLE stg_member (
  member_id TEXT, birth_year INTEGER, sex TEXT, city TEXT, state TEXT,
  zip TEXT, load_batch TEXT);

CREATE TABLE stg_provider (
  npi TEXT, last_name TEXT, first_name TEXT, specialty TEXT, load_batch TEXT);

CREATE TABLE stg_pharmacy_claim (
  rx_number TEXT, member_id TEXT, ndc TEXT, fill_date TEXT, paid_amount REAL,
  load_batch TEXT);

CREATE TABLE etl_load_log (
  load_id INTEGER PRIMARY KEY, table_name TEXT, batch TEXT, row_count INTEGER,
  started_at TEXT, finished_at TEXT, status TEXT);

CREATE TABLE etl_reject (
  reject_id INTEGER PRIMARY KEY, table_name TEXT, batch TEXT, reason TEXT,
  raw_row TEXT);

-- ============ views over the facts ============

CREATE VIEW v_claim_line_current AS
  SELECT f.claim_id, f.line_no, f.paid_amount, m.member_id, m.city, m.state
  FROM fact_claim_line f JOIN dim_member m ON m.member_key = f.member_key;

CREATE VIEW v_claim_line_as_of_service AS
  SELECT f.claim_id, f.line_no, f.paid_amount, h.member_id, h.city, h.state,
         h.plan_tier, h.version_no
  FROM fact_claim_line f
  JOIN dim_member_history h ON h.member_history_key = f.member_history_key;

CREATE VIEW v_paid_by_month AS
  SELECT d.year, d.month, SUM(f.paid_amount) AS paid
  FROM fact_claim_line f JOIN dim_date d ON d.date_key = f.paid_date_key
  GROUP BY d.year, d.month;

CREATE VIEW v_service_lag AS
  SELECT f.claim_id, s.full_date AS service_date, p.full_date AS paid_date,
         julianday(p.full_date) - julianday(s.full_date) AS days_to_pay
  FROM fact_claim_line f
  JOIN dim_date s ON s.date_key = f.service_date_key
  JOIN dim_date p ON p.date_key = f.paid_date_key;

CREATE VIEW v_pmpm AS
  SELECT mm.month_date_key, SUM(f.paid_amount) / COUNT(DISTINCT mm.member_key) AS pmpm
  FROM fact_member_month mm
  LEFT JOIN fact_claim_line f ON f.member_key = mm.member_key
  WHERE mm.is_eligible = 1
  GROUP BY mm.month_date_key;

CREATE VIEW v_readmissions AS
  SELECT e.encounter_id, e.member_key, e.length_of_stay, e.readmit_days
  FROM fact_encounter e WHERE e.is_readmission = 1;

CREATE VIEW v_chronic_members AS
  SELECT DISTINCT b.member_key, d.icd_code, d.description
  FROM bridge_member_condition b JOIN dim_diagnosis d ON d.diagnosis_key = b.diagnosis_key
  WHERE d.is_chronic = 1;

CREATE VIEW v_denial_rate_by_provider AS
  SELECT p.npi, COUNT(dn.denial_fact_key) AS denied, COUNT(f.claim_line_key) AS total
  FROM fact_claim_line f
  JOIN dim_provider p ON p.provider_key = f.provider_key
  LEFT JOIN fact_denial dn ON dn.claim_line_key = f.claim_line_key
  GROUP BY p.npi;

CREATE VIEW v_specialty_drug_spend AS
  SELECT dr.therapeutic_class, SUM(rx.paid_amount) AS spend
  FROM fact_pharmacy_claim rx JOIN dim_drug dr ON dr.drug_key = rx.drug_key
  WHERE dr.is_specialty = 1 GROUP BY dr.therapeutic_class;
"""

#: Hints a data engineer would actually write for this warehouse. They are
#: deliberately about *grain and role*, because that is what the names fail
#: to convey and what the questions turn on.
HINTS = {
    "fact_claim_line": "one row per claim line; the base grain for all paid amounts",
    "fact_claim_header": "one row per claim; use for claim-level counts and adjudication timing, not line detail",
    "fact_claim_monthly_agg": "pre-aggregated paid totals by month, payer and provider; fastest for monthly trend",
    "fact_member_month": "one row per member per month; the denominator for PMPM and enrollment counts",
    "dim_member": "current member attributes only; for point-in-time use dim_member_history",
    "dim_member_history": "every version of each member; join via member_history_key for as-of-service attributes",
    "dim_provider": "canonical provider dimension; dim_physician is a retired duplicate",
    "dim_payer": "canonical payer dimension; dim_plan_sponsor is a retired duplicate",
    "dim_date": "conformed date; joined as service, paid, received, admit, discharge and fill dates",
    "bridge_claim_diagnosis": "many-to-many between claim lines and diagnoses; required for any diagnosis question",
}

#: Every question here has at least one plausible wrong answer in the schema.
GOLDEN = [
    # grain
    ("paid amount per claim line",                     {"fact_claim_line"}),
    ("how many claims did each payer submit",          {"fact_claim_header"}),
    ("monthly paid trend by payer",                    {"fact_claim_monthly_agg"}),
    ("per member per month cost",                      {"v_pmpm"}),
    ("eligible member count by month",                 {"fact_member_month"}),
    # slowly-changing dimension
    ("member address at the time of service",          {"v_claim_line_as_of_service"}),
    ("current plan tier for each member",              {"dim_member"}),
    # role-playing date
    ("days between service and payment",               {"v_service_lag"}),
    # bridge
    ("diagnoses on each claim line",                   {"bridge_claim_diagnosis"}),
    ("members with chronic conditions",                {"v_chronic_members"}),
    # the rest of the model
    ("inpatient readmissions",                         {"v_readmissions"}),
    ("denial rate by provider",                        {"v_denial_rate_by_provider"}),
    ("specialty drug spend by therapeutic class",      {"v_specialty_drug_spend"}),
    ("quality measure rates per provider",             {"fact_quality_measure"}),
    ("pharmacy claims with days supply",               {"fact_pharmacy_claim"}),
]

#: (question, must win, must not win). The decoy is a copy, a staging
#: table, a retired duplicate, or the wrong grain.
DECOYS = [
    ("paid amount per claim line",              "fact_claim_line", "fact_claim_line_bkp"),
    ("paid amount per claim line",              "fact_claim_line", "fact_claim_line_v2"),
    ("paid amount per claim line",              "fact_claim_line", "stg_claim_line"),
    ("provider specialty and network status",   "dim_provider",    "dim_physician"),
    ("provider specialty and network status",   "dim_provider",    "dim_provider_old"),
    ("member city and state",                   "dim_member",      "dim_member_bkp"),
    ("payer line of business",                  "dim_payer",       "dim_plan_sponsor"),
]

#: Questions the offline embedder should NOT be expected to get right,
#: because the identifiers carry nothing the question says. This is where
#: grain-aware AI descriptions earn their keep, and the test measures it.
GOLDEN_NEEDS_DESCRIPTIONS = [
    ("what did we pay out last month, quickly",         {"fact_claim_monthly_agg"}),
    ("how much does each person cost us monthly",       {"v_pmpm"}),
    ("where did the member live when they were treated", {"v_claim_line_as_of_service"}),
    ("how long do we take to pay",                       {"v_service_lag"}),
]

RESTRICTED = {
    "fact_member_month": ["actuarial"],       # premium and capitation
    "dim_member_history": ["phi"],
    "stg_member": ["phi"],
}
