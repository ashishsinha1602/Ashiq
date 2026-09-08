"""Business-language retrieval evaluation across every test schema.

The GOLDEN sets ask questions in the schema's own vocabulary, so they measure
whether ranking works, not whether the words connect. Real users do not talk
like a data model. These questions deliberately share as little vocabulary
with the identifiers as an English speaker plausibly can:

    "boxes that stopped talking to us"  ->  v_silent_devices

Discipline that makes the numbers mean something:

  TUNE     commerce, health        -- may be inspected while changing ranking
  HELDOUT  warehouse, finance, telemetry, complex -- measured, never tuned on

A gold set is a set: any one of its members counts as a hit, because more
than one object can honestly answer a vague question.
"""
from __future__ import annotations

COMMERCE = [
    ("who hasn't paid us yet",                    {"billing_invoice", "v_customer_balance"}),
    ("money coming in each month",                {"v_monthly_revenue"}),
    ("things we're running out of",               {"v_stock_shortfall"}),
    ("how many people work in each team",         {"v_employee_headcount"}),
    ("parcels that arrived late",                 {"ship_shipment", "v_order_fulfilment"}),
    ("what we spend with vendors",                {"v_supplier_spend"}),
    ("what do we charge for each item",           {"cat_price_list_item", "cat_price_list"}),
    ("which deals might close soon",              {"crm_opportunity"}),
    ("who do we buy things from",                 {"sup_supplier"}),
    ("what is sitting in each depot",             {"inv_stock_level"}),
    ("how much tax do we add on top",             {"billing_tax_rate"}),
    ("money we gave back to shoppers",            {"billing_credit_note"}),
]

HEALTH = [
    ("which claims were turned down and why",     {"clm_denial", "v_claim_denial_rate", "ref_denial_reason"}),
    ("patients who came back within a month",     {"v_readmission"}),
    ("people living with long term conditions",   {"v_chronic_cohort"}),
    ("how often we hand out cheaper copies",      {"v_generic_dispensing"}),
    ("who is allowed to be covered right now",    {"mbr_eligibility"}),
    ("what the doctor wrote down as the problem", {"enc_diagnosis", "ref_dx_code"}),
    ("where did the visit take place",            {"ref_place_of_service", "org_facility"}),
    ("money the insurer actually sent us",        {"clm_remittance"}),
    ("what medicines were handed over",           {"rx_fill"}),
    ("how well are we scoring on standards",      {"qm_measure_result", "qm_measure"}),
]

WAREHOUSE = [
    ("how much did we pay out each month",        {"v_paid_by_month"}),
    ("how long between the visit and the money",  {"v_service_lag"}),
    ("average cost per person per month",         {"v_pmpm"}),
    ("patients coming back too soon",             {"v_readmissions"}),
    ("which doctors get turned down most",        {"v_denial_rate_by_provider"}),
    ("spending on very expensive medicines",      {"v_specialty_drug_spend"}),
    ("people with ongoing illnesses",             {"v_chronic_members"}),
    ("what did the person look like back then",   {"dim_member_history", "v_claim_line_as_of_service"}),
    ("rows that failed to load last night",       {"etl_reject", "etl_load_log"}),
    ("which conditions does each person carry",   {"bridge_member_condition"}),
]

FINANCE = [
    ("do the books balance",                      {"v_trial_balance"}),
    ("entries that do not add up",                {"v_unbalanced_journals"}),
    ("deals that failed to complete",             {"v_failed_settlements", "trd_settlement"}),
    ("how much are we exposed to each party",     {"v_counterparty_exposure"}),
    ("borrowers behind on their payments",        {"v_loans_in_arrears"}),
    ("how much do we expect to lose on lending",  {"v_expected_credit_loss", "lnd_provision"}),
    ("payments that bounced back",                {"v_payment_returns"}),
    ("exchange rate on a particular day",         {"v_fx_rate_as_of", "fx_rate_daily"}),
    ("customers flagged for extra checks",        {"kyc_review", "aml_alert"}),
    ("daily profit and loss",                     {"trd_pnl_daily"}),
]

TELEMETRY = [
    ("which warnings are still open",             {"v_open_alarms"}),
    ("how long until someone responds",           {"v_alarm_time_to_ack"}),
    ("boxes that stopped talking to us",          {"v_silent_devices"}),
    ("units about to run flat",                   {"v_low_battery"}),
    ("kit still running old software",            {"v_firmware_drift"}),
    ("instruments overdue for a check",           {"v_calibration_overdue"}),
    ("average per hour at each location",         {"v_hourly_site_average"}),
    ("where is each unit installed now",          {"v_device_current_site"}),
    ("when may we take things offline",           {"ops_maintenance_window"}),
    ("limits that trigger a warning",             {"tel_threshold"}),
]

COMPLEX = [
    ("cash we actually received",                 {"v_cash_collected", "v_cash_collected_monthly"}),
    ("how much stock do we hold",                 {"v_stock_position"}),
    ("what needs reordering",                     {"v_replenishment_need"}),
    ("staff numbers per division",                {"v_headcount_by_unit"}),
    ("deliveries that missed the date",           {"v_late_deliveries"}),
    ("our exposure to foreign money",             {"v_currency_exposure"}),
]

TUNE = {"commerce": COMMERCE, "health": HEALTH}
HELDOUT = {"warehouse": WAREHOUSE, "finance": FINANCE,
           "telemetry": TELEMETRY, "complex": COMPLEX}
ALL = {**TUNE, **HELDOUT}
