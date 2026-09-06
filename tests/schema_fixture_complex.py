"""A large, deliberately hostile schema for stress and robustness testing.

The two hand-written fixtures are realistic but small. This one is generated,
so it can be big enough to break things, and every structure in it is one
that has broken a schema tool somewhere:

  * ~260 objects across 4 schemas, so top_k is a brutal filter
  * the SAME table name in three different schemas (billing.account,
    crm.account, sec.account) -- anything keyed on bare names collapses here
  * a foreign-key chain 8 levels deep
  * a circular reference cycle a -> b -> c -> a
  * self-referencing hierarchies, two of them
  * composite (multi-column) foreign keys
  * a 320-column table, and a table with a single column
  * identifiers at 100+ characters, and cryptic 8-character legacy ones
  * non-ASCII identifiers and comments (Spanish, Japanese)
  * views built on views built on views
  * an _archive and _stg near-duplicate of every bulk table, which is
    what actually drowns retrieval in a mature warehouse
  * near-duplicate decoy pairs in four different domains
  * homonym columns: status/state/code/type repeat across ~200 tables

Everything is synthetic and invented. It models a generic multi-domain
enterprise (sales, billing, logistics, HR, security, telemetry) and is not
derived from any real system.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

_DOMAINS = {
    "sales": ["lead", "campaign", "territory", "quota", "commission",
              "pipeline_stage", "forecast", "discount_band", "rebate",
              "channel_partner", "renewal", "upsell_play"],
    "billing": ["account", "subscription", "usage_record", "rate_plan",
                "dunning_event", "collection_case", "write_off", "adjustment",
                "settlement", "chargeback", "revenue_schedule", "proration"],
    "logistics": ["consignment", "manifest", "customs_entry", "dock_slot",
                  "route_leg", "carrier_rate", "pallet", "container_seal",
                  "temperature_log", "damage_claim", "yard_move", "gate_pass"],
    "hr": ["applicant", "requisition", "onboarding_task", "shift_pattern",
           "absence_request", "training_record", "certification", "grievance",
           "performance_cycle", "succession_plan", "exit_interview", "badge"],
    "security": ["principal_grant", "role_definition", "access_review",
                 "policy_binding", "secret_rotation", "audit_trail",
                 "session_token", "mfa_enrolment", "risk_signal",
                 "quarantine_hold", "consent_record", "retention_rule"],
    "telemetry": ["device", "reading", "alarm", "threshold_profile",
                  "calibration", "firmware_build", "downlink_command",
                  "gateway_hop", "signal_quality", "battery_state",
                  "maintenance_window", "anomaly_score"],
}

# columns sprinkled everywhere so that homonyms are genuinely ambiguous
_NOISE = ["status", "state", "code", "type", "created_at", "updated_at",
          "is_active", "version", "source_system", "external_ref"]


def _table(name, cols, fks=(), schema=None):
    body = ",\n  ".join(cols)
    fk_sql = "".join(f",\n  {f}" for f in fks)
    qualified = f'"{schema}"."{name}"' if schema else f'"{name}"'
    return f'CREATE TABLE {qualified} (\n  {body}{fk_sql}\n);'


def _build(forward_refs: bool = True):
    """Return ``(create_statements, alter_statements)``.

    ``forward_refs=True`` closes the reference cycle with an inline foreign
    key pointing at a table that does not exist yet. SQLite allows that;
    PostgreSQL, Oracle and SQL Server do not. For those, pass ``False`` and
    apply the returned ALTER statements afterwards -- the resulting cycle
    is identical, it is just built in two steps.
    """
    out = []
    alters = []

    # ---- the ordinary bulk, plus the archive/staging copies that make
    # ---- real catalogues so much worse than the diagram suggests -------
    for domain, tables in _DOMAINS.items():
        for i, table in enumerate(tables):
            cols = ["id INTEGER PRIMARY KEY",
                    f"{table}_number TEXT",
                    "amount REAL",
                    "quantity REAL"]
            cols += [f"{c} TEXT" for c in _NOISE]
            fks = []
            if i > 0:                      # link each table to the previous
                prev = tables[i - 1]
                cols.append(f"id_{prev} INTEGER")
                fks.append(f'FOREIGN KEY (id_{prev}) REFERENCES "{domain}_{prev}"(id)')
            out.append(_table(f"{domain}_{table}", cols, fks))
            # near-duplicates of every single table: this is what actually
            # drowns retrieval in a mature warehouse
            for suffix in ("_archive", "_stg"):
                out.append(_table(f"{domain}_{table}{suffix}",
                                  ["id INTEGER PRIMARY KEY",
                                   f"{table}_number TEXT", "amount REAL",
                                   "quantity REAL", "status TEXT",
                                   "loaded_at TEXT"]))

    # ---- same bare name in three schemas: the qualified-name trap -------
    for schema, extra in [("billing", "balance REAL"),
                          ("crm", "owner_name TEXT"),
                          ("sec", "last_login_at TEXT")]:
        out.append(_table("account",
                          ["id INTEGER PRIMARY KEY", "account_number TEXT",
                           extra, "status TEXT"],
                          schema=schema))

    # ---- an 8-deep foreign-key chain -----------------------------------
    for level in range(8):
        cols = ["id INTEGER PRIMARY KEY", "label TEXT", "status TEXT"]
        fks = []
        if level:
            cols.append(f"id_parent_l{level - 1} INTEGER")
            fks.append(f'FOREIGN KEY (id_parent_l{level - 1}) '
                       f'REFERENCES "chain_level_{level - 1}"(id)')
        out.append(_table(f"chain_level_{level}", cols, fks))

    # ---- a reference cycle: a -> b -> c -> a ---------------------------
    cycle = [("cycle_alpha", "cycle_beta"),
             ("cycle_beta", "cycle_gamma"),
             ("cycle_gamma", "cycle_alpha")]
    for this, nxt in cycle:
        cols = ["id INTEGER PRIMARY KEY", f"id_{nxt} INTEGER", "status TEXT"]
        fk = f'FOREIGN KEY (id_{nxt}) REFERENCES "{nxt}"(id)'
        if forward_refs:
            out.append(_table(this, cols, [fk]))
        else:
            # every edge of a cycle points at something not yet created,
            # so all three constraints have to be added afterwards
            out.append(_table(this, cols))
            alters.append(
                f'ALTER TABLE "{this}" ADD CONSTRAINT {this}_fk '
                f'FOREIGN KEY (id_{nxt}) REFERENCES "{nxt}"(id)')

    # ---- self-referencing hierarchies ----------------------------------
    for name in ["org_unit", "product_taxonomy"]:
        out.append(_table(name,
                          ["id INTEGER PRIMARY KEY", "name TEXT",
                           "id_parent INTEGER", "depth INTEGER", "status TEXT"],
                          [f'FOREIGN KEY (id_parent) REFERENCES "{name}"(id)']))

    # ---- composite primary and foreign keys ----------------------------
    out.append(_table(
        "composite_order_line",
        ["id_order INTEGER NOT NULL", "line_no INTEGER NOT NULL",
         "id_product INTEGER", "quantity REAL", "unit_price REAL",
         "PRIMARY KEY (id_order, line_no)"]))
    out.append(_table(
        "composite_shipment_line",
        ["id INTEGER PRIMARY KEY", "id_order INTEGER", "line_no INTEGER",
         "shipped_qty REAL"],
        ['FOREIGN KEY (id_order, line_no) REFERENCES '
         '"composite_order_line"(id_order, line_no)']))

    # ---- pathological shapes -------------------------------------------
    wide = ["id INTEGER PRIMARY KEY"] + [
        f"attribute_{i:03d} TEXT" for i in range(320)]
    out.append(_table("wide_measurement_matrix", wide))
    out.append(_table("single_column_flag", ["only_column TEXT"]))

    # a 100+ character identifier, which several tools truncate or reject
    long_name = ("enterprise_consolidated_quarterly_revenue_recognition_"
                 "adjustment_workpaper_line_detail_archive")
    out.append(_table(long_name,
                      ["id INTEGER PRIMARY KEY", "amount REAL",
                       "period TEXT", "status TEXT"]))

    # cryptic legacy names of the kind mainframe migrations leave behind
    for legacy, comment in [("dm_ct_mst", "cost type master"),
                            ("ft_txn_ln", "transaction lines"),
                            ("wk_bal_agg", "balance aggregate"),
                            ("xr_ccy_dly", "daily currency rates")]:
        out.append(_table(legacy,
                          ["id INTEGER PRIMARY KEY", "cd TEXT", "dsc TEXT",
                           "amt REAL", "dt TEXT", "flg INTEGER"]))

    # ---- non-ASCII identifiers -----------------------------------------
    out.append(_table("facturación_mensual",
                      ["id INTEGER PRIMARY KEY", "año INTEGER", "mes INTEGER",
                       "importe_neto REAL", "estado TEXT"]))
    out.append(_table("売上明細",
                      ["id INTEGER PRIMARY KEY", "金額 REAL", "数量 REAL",
                       "状態 TEXT"]))

    # ---- decoy pairs: near-identical names, different meaning ----------
    for real, decoy in [("payment_received", "payment_pending"),
                        ("inventory_on_hand", "inventory_forecast"),
                        ("employee_active", "employee_applicant"),
                        ("shipment_delivered", "shipment_planned")]:
        for name in (real, decoy):
            out.append(_table(name,
                              ["id INTEGER PRIMARY KEY", "amount REAL",
                               "quantity REAL", "recorded_on TEXT",
                               "status TEXT"]))

    # ---- views, including views on views on views ----------------------
    out.append('CREATE VIEW v_cash_collected AS '
               'SELECT id, amount, recorded_on FROM "payment_received";')
    out.append('CREATE VIEW v_cash_collected_monthly AS '
               'SELECT substr(recorded_on,1,7) AS month, SUM(amount) AS total '
               'FROM v_cash_collected GROUP BY 1;')
    out.append('CREATE VIEW v_cash_collected_trend AS '
               'SELECT month, total, total - LAG(total) OVER (ORDER BY month) '
               'AS delta FROM v_cash_collected_monthly;')

    out.append('CREATE VIEW v_stock_position AS '
               'SELECT id, quantity AS on_hand FROM "inventory_on_hand";')
    out.append('CREATE VIEW v_replenishment_need AS '
               'SELECT id, on_hand FROM v_stock_position WHERE on_hand < 10;')

    out.append('CREATE VIEW v_headcount_by_unit AS '
               'SELECT o.name AS unit, COUNT(e.id) AS headcount '
               'FROM "org_unit" o LEFT JOIN "employee_active" e ON 1=0 '
               'GROUP BY o.name;')

    out.append('CREATE VIEW v_late_deliveries AS '
               'SELECT id, recorded_on, status FROM "shipment_delivered" '
               "WHERE status = 'LATE';")

    out.append('CREATE VIEW v_currency_exposure AS '
               'SELECT x.cd AS currency, SUM(f.amt) AS exposure '
               'FROM "ft_txn_ln" f JOIN "xr_ccy_dly" x ON x.id = f.id '
               'GROUP BY x.cd;')

    # A view that lives in a non-default schema, whose meaning is entirely
    # in its SQL rather than its output columns. Engines differ on whether
    # a view may cross schemas, so this one stays inside billing.
    out.append('CREATE VIEW "billing"."v_at_risk_accounts" AS '
               'SELECT a.id, a.balance FROM "billing"."account" a '
               'WHERE a.balance < 0 AND a.status = \'DUNNING\';')

    return out, alters


#: schemas that must be ATTACHed before the DDL runs (SQLite) or created
#: as real schemas (Postgres/Oracle)
ATTACHED_SCHEMAS = ["billing", "crm", "sec"]

def statements(forward_refs: bool = True):
    """All DDL as a list: creates first, then any ALTERs that close cycles."""
    creates, alters = _build(forward_refs=forward_refs)
    return creates + alters


#: convenience for SQLite, which tolerates forward references
DDL = "\n\n".join(_build(forward_refs=True)[0])


HINTS = {
    "payment_received": "cash actually collected; the authoritative payment record",
    "payment_pending": "not yet collected; excluded from cash figures",
    "inventory_on_hand": "current physical stock counts",
    "inventory_forecast": "projected stock, not actual; never use for current levels",
    "employee_active": "people currently employed",
    "employee_applicant": "candidates, not employees",
    "ft_txn_ln": "legacy general-ledger transaction lines from the retired system",
    "dm_ct_mst": "legacy cost-type master; cd holds the cost type code",
}

#: question -> objects that must appear. Kept to objects a person could
#: reasonably name, since the generated bulk exists to be noise.
GOLDEN = [
    ("cash collected each month",              {"v_cash_collected_monthly"}),
    ("which accounts are in dunning",          {"v_at_risk_accounts"}),
    ("items needing replenishment",            {"v_replenishment_need"}),
    ("headcount per organisational unit",      {"v_headcount_by_unit"}),
    ("late deliveries",                        {"v_late_deliveries"}),
    ("currency exposure from the legacy ledger", {"v_currency_exposure"}),
    ("shipment lines against order lines",     {"composite_shipment_line"}),
    ("device alarm thresholds",                {"telemetry_threshold_profile"}),
    ("customs entries for consignments",       {"logistics_customs_entry"}),
    ("multi factor enrolment records",         {"security_mfa_enrolment"}),
    ("commission and quota by territory",      {"sales_commission", "sales_quota"}),
]

#: Questions in one language about objects named in another. The offline
#: embedder matches characters, so it cannot bridge this on its own -- and
#: pretending otherwise would be dishonest. AI descriptions are the fix,
#: and tests/test_complex_schema.py measures exactly that.
GOLDEN_CROSS_LANGUAGE = [
    ("monthly invoicing amounts", {"facturación_mensual"}),
    ("sales detail lines",        {"売上明細"}),
]

#: (question, the object that should win, the decoy that must not)
DECOYS = [
    ("how much cash did we actually collect", "payment_received", "payment_pending"),
    ("current stock on hand right now", "inventory_on_hand", "inventory_forecast"),
    ("how many people work here", "employee_active", "employee_applicant"),
]

#: objects that only a privileged caller may see
RESTRICTED = {
    "security_audit_trail": ["security"],
    "security_session_token": ["security"],
    "sec.account": ["security"],
    "hr_grievance": ["hr"],
    "hr_exit_interview": ["hr"],
}
