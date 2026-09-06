"""A second test schema from an unrelated domain.

The commerce fixture in ``schema_fixture.py`` shares a lot of vocabulary with
the questions asked of it (invoice, warehouse, supplier). That flatters any
lexical retriever. This schema is deliberately different in kind:

  * clinical/administrative vocabulary with heavy abbreviation
    (``dx_code``, ``npi``, ``los_days``) where the question words rarely
    match the identifiers
  * code-lookup tables that carry the meaning the question actually asks
    about, one join away from the fact table
  * two near-identical claim tables (submitted vs adjudicated) that a naive
    matcher conflates -- the same decoy trap as invoice vs invoice_draft,
    but in vocabulary the first fixture never sees
  * an eligibility table that is role-restricted, for isolation tests in a
    second domain

If selection only worked because the commerce schema's names happened to
look like its questions, this schema exposes it. Both are synthetic and
domain-generic: no organisation's real schema appears in this project.
"""
from __future__ import annotations

DDL = """
CREATE TABLE ref_payer (
  id INTEGER PRIMARY KEY, payer_code TEXT, name TEXT, plan_type TEXT);

CREATE TABLE ref_dx_code (
  id INTEGER PRIMARY KEY, dx_code TEXT, short_desc TEXT, chapter TEXT,
  is_chronic INTEGER);

CREATE TABLE ref_proc_code (
  id INTEGER PRIMARY KEY, proc_code TEXT, short_desc TEXT, category TEXT,
  rvu REAL);

CREATE TABLE ref_place_of_service (
  id INTEGER PRIMARY KEY, pos_code TEXT, label TEXT);

CREATE TABLE ref_denial_reason (
  id INTEGER PRIMARY KEY, reason_code TEXT, description TEXT, is_appealable INTEGER);

CREATE TABLE org_provider (
  id INTEGER PRIMARY KEY, npi TEXT, last_name TEXT, first_name TEXT,
  taxonomy_code TEXT, is_active INTEGER);

CREATE TABLE org_facility (
  id INTEGER PRIMARY KEY, facility_code TEXT, name TEXT, bed_count INTEGER,
  id_pos INTEGER REFERENCES ref_place_of_service(id));

CREATE TABLE org_provider_facility (
  id INTEGER PRIMARY KEY, id_provider INTEGER REFERENCES org_provider(id),
  id_facility INTEGER REFERENCES org_facility(id), affiliated_from TEXT);

CREATE TABLE mbr_member (
  id INTEGER PRIMARY KEY, member_number TEXT, birth_year INTEGER, sex TEXT,
  id_payer INTEGER REFERENCES ref_payer(id), enrolled_on TEXT, status TEXT);

CREATE TABLE mbr_eligibility (
  id INTEGER PRIMARY KEY, id_member INTEGER REFERENCES mbr_member(id),
  effective_from TEXT, effective_to TEXT, coverage_tier TEXT,
  premium_amount REAL);

CREATE TABLE enc_encounter (
  id INTEGER PRIMARY KEY, encounter_number TEXT,
  id_member INTEGER REFERENCES mbr_member(id),
  id_provider INTEGER REFERENCES org_provider(id),
  id_facility INTEGER REFERENCES org_facility(id),
  admitted_on TEXT, discharged_on TEXT, los_days INTEGER, encounter_type TEXT);

CREATE TABLE enc_diagnosis (
  id INTEGER PRIMARY KEY, id_encounter INTEGER REFERENCES enc_encounter(id),
  id_dx INTEGER REFERENCES ref_dx_code(id), seq_no INTEGER, is_primary INTEGER);

CREATE TABLE enc_procedure (
  id INTEGER PRIMARY KEY, id_encounter INTEGER REFERENCES enc_encounter(id),
  id_proc INTEGER REFERENCES ref_proc_code(id), performed_on TEXT,
  id_provider INTEGER REFERENCES org_provider(id));

CREATE TABLE clm_claim_submitted (
  id INTEGER PRIMARY KEY, claim_number TEXT,
  id_encounter INTEGER REFERENCES enc_encounter(id),
  submitted_on TEXT, billed_amount REAL, status TEXT);

CREATE TABLE clm_claim (
  id INTEGER PRIMARY KEY, claim_number TEXT,
  id_encounter INTEGER REFERENCES enc_encounter(id),
  id_payer INTEGER REFERENCES ref_payer(id),
  adjudicated_on TEXT, billed_amount REAL, allowed_amount REAL,
  paid_amount REAL, patient_resp REAL, status TEXT);

CREATE TABLE clm_claim_line (
  id INTEGER PRIMARY KEY, id_claim INTEGER REFERENCES clm_claim(id),
  line_no INTEGER, id_proc INTEGER REFERENCES ref_proc_code(id),
  units REAL, billed_amount REAL, allowed_amount REAL);

CREATE TABLE clm_denial (
  id INTEGER PRIMARY KEY, id_claim INTEGER REFERENCES clm_claim(id),
  id_reason INTEGER REFERENCES ref_denial_reason(id), denied_on TEXT,
  appealed_on TEXT, overturned INTEGER);

CREATE TABLE clm_remittance (
  id INTEGER PRIMARY KEY, id_claim INTEGER REFERENCES clm_claim(id),
  remitted_on TEXT, check_number TEXT, amount REAL);

CREATE TABLE rx_drug (
  id INTEGER PRIMARY KEY, ndc TEXT, label_name TEXT, generic_name TEXT,
  is_specialty INTEGER);

CREATE TABLE rx_prescription (
  id INTEGER PRIMARY KEY, id_member INTEGER REFERENCES mbr_member(id),
  id_prescriber INTEGER REFERENCES org_provider(id),
  id_drug INTEGER REFERENCES rx_drug(id), written_on TEXT,
  days_supply INTEGER, quantity REAL);

CREATE TABLE rx_fill (
  id INTEGER PRIMARY KEY,
  id_prescription INTEGER REFERENCES rx_prescription(id),
  filled_on TEXT, ingredient_cost REAL, copay REAL);

CREATE TABLE qm_measure (
  id INTEGER PRIMARY KEY, measure_code TEXT, title TEXT, steward TEXT);

CREATE TABLE qm_measure_result (
  id INTEGER PRIMARY KEY, id_measure INTEGER REFERENCES qm_measure(id),
  id_provider INTEGER REFERENCES org_provider(id), period TEXT,
  numerator INTEGER, denominator INTEGER, rate REAL);

CREATE VIEW v_claim_denial_rate AS
  SELECT p.payer_code,
         COUNT(d.id) AS denied, COUNT(c.id) AS total
  FROM clm_claim c
  JOIN ref_payer p ON p.id = c.id_payer
  LEFT JOIN clm_denial d ON d.id_claim = c.id
  GROUP BY p.payer_code;

CREATE VIEW v_readmission AS
  SELECT e.id_member, e.id_facility, e.discharged_on, e.los_days,
         e.encounter_type
  FROM enc_encounter e
  WHERE e.encounter_type = 'INPATIENT' AND e.los_days IS NOT NULL;

CREATE VIEW v_chronic_cohort AS
  SELECT m.id AS id_member, d.dx_code, d.short_desc
  FROM mbr_member m
  JOIN enc_encounter e ON e.id_member = m.id
  JOIN enc_diagnosis ed ON ed.id_encounter = e.id
  JOIN ref_dx_code d ON d.id = ed.id_dx
  WHERE d.is_chronic = 1;

CREATE VIEW v_generic_dispensing AS
  SELECT dr.generic_name, COUNT(*) AS fills, SUM(f.ingredient_cost) AS cost
  FROM rx_fill f
  JOIN rx_prescription pr ON pr.id = f.id_prescription
  JOIN rx_drug dr ON dr.id = pr.id_drug
  GROUP BY dr.generic_name;
"""

HINTS = {
    "clm_claim": "adjudicated claims; the authoritative paid-amount record",
    "clm_claim_submitted": "as-submitted claims before adjudication; NOT payment data",
    "mbr_eligibility": "coverage spans and premiums; restricted",
    "v_readmission": "inpatient discharges used for readmission analysis",
}

# question -> objects that MUST appear in the selection
GOLDEN = [
    ("denial rate by payer",                    {"v_claim_denial_rate"}),
    ("average length of stay for inpatients",   {"v_readmission"}),
    ("members with chronic conditions",         {"v_chronic_cohort"}),
    ("generic dispensing cost",                 {"v_generic_dispensing"}),
    ("paid amount per claim line",              {"clm_claim_line"}),
    ("which providers performed which procedures",
     {"enc_procedure", "org_provider"}),
    ("quality measure rates by provider",       {"qm_measure_result"}),
    ("remittance checks issued",                {"clm_remittance"}),
    ("prescriptions written per prescriber",    {"rx_prescription"}),
    ("primary diagnosis on each encounter",     {"enc_diagnosis"}),
]

# Same trap as the commerce fixture: the decoy shares vocabulary with the
# question but is the wrong table to answer it.
DECOYS = [
    ("total paid amount by payer", "clm_claim", "clm_claim_submitted"),
]
