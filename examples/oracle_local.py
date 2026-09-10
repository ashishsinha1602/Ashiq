#!/usr/bin/env python3
"""schemagate on Oracle, start to finish, in one file.

    pip install "schemagate[oracle]"
    python3 schemagate_oracle.py                         # interactive
    python3 schemagate_oracle.py "which claims were denied and why"

Starts Oracle 26ai Free in Docker if it is not already up, builds a 219-object
hospital schema, then answers questions against it. Re-running is cheap: the
container and schema are reused if they are already there.

    python3 schemagate_oracle.py --rebuild   # drop and rebuild the schema
    python3 schemagate_oracle.py --stop      # remove the container

Needs Docker. The first run pulls a ~2 GB image; after that it is about a
minute. Nothing here touches a database other than the one it starts.
"""
import subprocess
import sys
import time

CONTAINER = "orafree"
IMAGE = "gvenzl/oracle-free:23-slim"
DSN = "localhost:1521/FREEPDB1"
SYS_PW = "Sg_Test_2026"
USER, PW = "med", "Med_2026x"
URL = f"oracle+oracledb://{USER}:{PW}@localhost:1521/?service_name=FREEPDB1"


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def container_state():
    r = sh("docker", "inspect", "-f", "{{.State.Running}}", CONTAINER)
    if r.returncode != 0:
        return "absent"
    return "running" if r.stdout.strip() == "true" else "stopped"


def start_oracle():
    state = container_state()
    if state == "running":
        print(f"Oracle already running ({CONTAINER}).")
    else:
        if state == "stopped":
            print("starting the existing container...")
            sh("docker", "start", CONTAINER)
        else:
            print(f"starting {IMAGE} -- the first pull is ~2 GB and slow, once.")
            r = sh("docker", "run", "-d", "--name", CONTAINER, "-p", "1521:1521",
                   "-e", f"ORACLE_PASSWORD={SYS_PW}", IMAGE)
            if r.returncode != 0:
                sys.exit("could not start the container:\n" + r.stderr.strip())

    print("waiting for the database", end="", flush=True)
    for i in range(180):
        logs = sh("docker", "logs", CONTAINER).stdout + sh("docker", "logs", CONTAINER).stderr
        if "DATABASE IS READY TO USE" in logs:
            print(f" ready ({i*5}s)")
            return
        print(".", end="", flush=True)
        time.sleep(5)
    sys.exit("\nthe database did not come up; try: docker logs " + CONTAINER)



def schema_exists():
    import oracledb
    try:
        c = oracledb.connect(user=USER, password=PW, dsn=DSN)
    except Exception:
        return False
    cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM user_tables")
    n = cur.fetchone()[0]
    c.close()
    return n > 100


def build_schema():
    """A dense hospital schema: clinical, pharmacy, lab and billing tables with
    foreign keys between them, the Oracle types SQLAlchemy cannot render on its
    own, two tables that must not reach the wrong caller, and a pile of audit
    and staging tables so selection has something to be wrong about."""
    import oracledb
    admin = oracledb.connect(user="system", password=SYS_PW, dsn=DSN)
    a = admin.cursor()
    for stmt in (f"DROP USER {USER} CASCADE",
                 f"CREATE USER {USER} IDENTIFIED BY {PW}",
                 f"GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE TO {USER}",
                 f"GRANT CREATE VIEW TO {USER}"):
        try:
            a.execute(stmt)
        except Exception as e:
            if "DROP USER" not in stmt:
                print("  admin:", str(e).split("\n")[0])
    admin.close()

    c = oracledb.connect(user=USER, password=PW, dsn=DSN)
    cur = c.cursor()

    def run(sql, quiet=False):
        try:
            cur.execute(sql)
            return True
        except Exception as e:
            if not quiet:
                print("  failed:", str(e).split("\n")[0])
            return False

    DDL = [
     # --- people and places ---------------------------------------------------
     """CREATE TABLE department (department_id NUMBER(8) PRIMARY KEY,
          name VARCHAR2(120) NOT NULL, cost_centre VARCHAR2(20), floor NUMBER(3))""",
     """CREATE TABLE ward (ward_id NUMBER(8) PRIMARY KEY,
          department_id NUMBER(8) REFERENCES department(department_id),
          name VARCHAR2(120), bed_count NUMBER(4), speciality VARCHAR2(80))""",
     """CREATE TABLE bed (bed_id NUMBER(8) PRIMARY KEY,
          ward_id NUMBER(8) REFERENCES ward(ward_id),
          label VARCHAR2(20), is_occupied NUMBER(1), last_cleaned TIMESTAMP)""",
     """CREATE TABLE provider (provider_id NUMBER(8) PRIMARY KEY,
          full_name VARCHAR2(200) NOT NULL, npi_number VARCHAR2(20),
          department_id NUMBER(8) REFERENCES department(department_id),
          speciality VARCHAR2(120), hired_on DATE)""",
     """CREATE TABLE provider_credential (credential_id NUMBER(10) PRIMARY KEY,
          provider_id NUMBER(8) REFERENCES provider(provider_id),
          credential_type VARCHAR2(60), issued_on DATE, expires_on DATE,
          issuing_body VARCHAR2(160))""",
     """CREATE TABLE staff_shift (shift_id NUMBER(12) PRIMARY KEY,
          provider_id NUMBER(8) REFERENCES provider(provider_id),
          ward_id NUMBER(8) REFERENCES ward(ward_id),
          starts_at TIMESTAMP, ends_at TIMESTAMP, role_on_shift VARCHAR2(60))""",

     # --- patients ------------------------------------------------------------
     """CREATE TABLE patient (patient_id NUMBER(12) PRIMARY KEY,
          medical_record_number VARCHAR2(32) UNIQUE,
          given_name VARCHAR2(120), family_name VARCHAR2(120),
          date_of_birth DATE, sex VARCHAR2(16), registered_on DATE)""",
     """CREATE TABLE patient_contact (contact_id NUMBER(12) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          relationship VARCHAR2(60), phone VARCHAR2(40), email VARCHAR2(320),
          is_emergency NUMBER(1))""",
     """CREATE TABLE patient_address (address_id NUMBER(12) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          line1 VARCHAR2(200), city VARCHAR2(120), postcode VARCHAR2(24),
          country VARCHAR2(80))""",
     """CREATE TABLE patient_allergy (allergy_id NUMBER(12) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          substance VARCHAR2(160), severity VARCHAR2(40), noted_on DATE)""",

     # --- restricted ----------------------------------------------------------
     """CREATE TABLE patient_identifier (identifier_id NUMBER(12) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          national_insurance_no VARCHAR2(32), passport_no VARCHAR2(40),
          ssn VARCHAR2(16))""",
     """CREATE TABLE staff_salary (salary_id NUMBER(10) PRIMARY KEY,
          provider_id NUMBER(8) REFERENCES provider(provider_id),
          annual_amount NUMBER(12,2), currency VARCHAR2(3),
          pay_grade VARCHAR2(20), effective_from DATE)""",

     # --- the clinical record -------------------------------------------------
     """CREATE TABLE encounter (encounter_id NUMBER(14) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          provider_id NUMBER(8) REFERENCES provider(provider_id),
          ward_id NUMBER(8) REFERENCES ward(ward_id),
          admitted_at TIMESTAMP, discharged_at TIMESTAMP,
          encounter_type VARCHAR2(40), triage_level NUMBER(2))""",
     """CREATE TABLE encounter_note (note_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          authored_by NUMBER(8) REFERENCES provider(provider_id),
          authored_at TIMESTAMP, body CLOB, structured_note XMLTYPE)""",
     """CREATE TABLE icd10_code (code VARCHAR2(10) PRIMARY KEY,
          title VARCHAR2(400), chapter VARCHAR2(120))""",
     """CREATE TABLE diagnosis (diagnosis_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          code VARCHAR2(10) REFERENCES icd10_code(code),
          is_primary NUMBER(1), diagnosed_at TIMESTAMP)""",
     """CREATE TABLE procedure_catalogue (procedure_code VARCHAR2(12) PRIMARY KEY,
          title VARCHAR2(300), typical_minutes NUMBER(5), theatre_required NUMBER(1))""",
     """CREATE TABLE procedure_performed (performed_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          procedure_code VARCHAR2(12) REFERENCES procedure_catalogue(procedure_code),
          performed_by NUMBER(8) REFERENCES provider(provider_id),
          started_at TIMESTAMP, ended_at TIMESTAMP)""",
     """CREATE TABLE vital_sign (vital_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          recorded_at TIMESTAMP, heart_rate NUMBER(4), systolic NUMBER(4),
          diastolic NUMBER(4), temperature_c NUMBER(4,1), oxygen_saturation NUMBER(4,1))""",

     # --- pharmacy ------------------------------------------------------------
     """CREATE TABLE medication (medication_id NUMBER(10) PRIMARY KEY,
          name VARCHAR2(200), form VARCHAR2(60), strength VARCHAR2(60),
          is_controlled NUMBER(1))""",
     """CREATE TABLE prescription (prescription_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          medication_id NUMBER(10) REFERENCES medication(medication_id),
          prescribed_by NUMBER(8) REFERENCES provider(provider_id),
          dose VARCHAR2(80), frequency VARCHAR2(80), started_on DATE, ended_on DATE)""",
     """CREATE TABLE medication_administration (administration_id NUMBER(14) PRIMARY KEY,
          prescription_id NUMBER(14) REFERENCES prescription(prescription_id),
          administered_by NUMBER(8) REFERENCES provider(provider_id),
          administered_at TIMESTAMP, dose_given VARCHAR2(80), was_refused NUMBER(1))""",
     """CREATE TABLE pharmacy_stock (stock_id NUMBER(12) PRIMARY KEY,
          medication_id NUMBER(10) REFERENCES medication(medication_id),
          batch_no VARCHAR2(40), expires_on DATE, quantity_on_hand NUMBER(8))""",

     # --- laboratory ----------------------------------------------------------
     """CREATE TABLE lab_panel (panel_code VARCHAR2(16) PRIMARY KEY,
          title VARCHAR2(200), specimen_type VARCHAR2(80))""",
     """CREATE TABLE lab_order (lab_order_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          panel_code VARCHAR2(16) REFERENCES lab_panel(panel_code),
          ordered_by NUMBER(8) REFERENCES provider(provider_id),
          ordered_at TIMESTAMP, urgency VARCHAR2(20))""",
     """CREATE TABLE lab_result (result_id NUMBER(14) PRIMARY KEY,
          lab_order_id NUMBER(14) REFERENCES lab_order(lab_order_id),
          analyte VARCHAR2(120), value_numeric NUMBER(14,4), unit VARCHAR2(40),
          reference_low NUMBER(14,4), reference_high NUMBER(14,4),
          is_abnormal NUMBER(1), resulted_at TIMESTAMP)""",
     """CREATE TABLE imaging_study (study_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          modality VARCHAR2(20), body_part VARCHAR2(80), performed_at TIMESTAMP,
          report_text CLOB, dicom_metadata JSON, image_embedding VECTOR(512, FLOAT32))""",

     # --- money ---------------------------------------------------------------
     """CREATE TABLE payer (payer_id NUMBER(8) PRIMARY KEY,
          name VARCHAR2(200), payer_type VARCHAR2(60), contact_email VARCHAR2(320))""",
     """CREATE TABLE patient_insurance (policy_id NUMBER(12) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          payer_id NUMBER(8) REFERENCES payer(payer_id),
          policy_number VARCHAR2(60), valid_from DATE, valid_to DATE)""",
     """CREATE TABLE claim (claim_id NUMBER(14) PRIMARY KEY,
          encounter_id NUMBER(14) REFERENCES encounter(encounter_id),
          payer_id NUMBER(8) REFERENCES payer(payer_id),
          submitted_on DATE, total_amount NUMBER(14,2),
          status VARCHAR2(30), denial_reason VARCHAR2(300))""",
     """CREATE TABLE claim_line (claim_id NUMBER(14) REFERENCES claim(claim_id),
          line_no NUMBER(6), procedure_code VARCHAR2(12)
            REFERENCES procedure_catalogue(procedure_code),
          charge_amount NUMBER(14,2), allowed_amount NUMBER(14,2),
          CONSTRAINT pk_claim_line PRIMARY KEY (claim_id, line_no))""",
     """CREATE TABLE payment (payment_id NUMBER(14) PRIMARY KEY,
          claim_id NUMBER(14) REFERENCES claim(claim_id),
          received_on DATE, amount NUMBER(14,2), method VARCHAR2(40))""",
     """CREATE TABLE patient_balance (balance_id NUMBER(14) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          as_of DATE, outstanding_amount NUMBER(14,2), days_overdue NUMBER(6))""",

     # --- scheduling ----------------------------------------------------------
     """CREATE TABLE appointment (appointment_id NUMBER(14) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          provider_id NUMBER(8) REFERENCES provider(provider_id),
          scheduled_for TIMESTAMP, status VARCHAR2(30), reason VARCHAR2(300))""",
     """CREATE TABLE referral (referral_id NUMBER(14) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          from_provider NUMBER(8) REFERENCES provider(provider_id),
          to_department NUMBER(8) REFERENCES department(department_id),
          raised_on DATE, priority VARCHAR2(20))""",
     """CREATE TABLE waiting_list (entry_id NUMBER(14) PRIMARY KEY,
          patient_id NUMBER(12) REFERENCES patient(patient_id),
          procedure_code VARCHAR2(12) REFERENCES procedure_catalogue(procedure_code),
          added_on DATE, target_days NUMBER(5))""",
    ]
    for s in DDL:
        run(s)

    # --- views ------------------------------------------------------------------
    for v in [
     """CREATE VIEW v_current_inpatients AS
          SELECT e.encounter_id, p.medical_record_number, p.family_name,
                 w.name AS ward_name, e.admitted_at
            FROM encounter e JOIN patient p ON p.patient_id = e.patient_id
                 JOIN ward w ON w.ward_id = e.ward_id
           WHERE e.discharged_at IS NULL""",
     """CREATE VIEW v_claim_recovery AS
          SELECT c.claim_id, c.total_amount,
                 NVL(SUM(pm.amount), 0) AS paid,
                 c.total_amount - NVL(SUM(pm.amount), 0) AS outstanding
            FROM claim c LEFT JOIN payment pm ON pm.claim_id = c.claim_id
           GROUP BY c.claim_id, c.total_amount""",
     """CREATE VIEW v_abnormal_results AS
          SELECT lr.result_id, lo.encounter_id, lr.analyte, lr.value_numeric, lr.unit
            FROM lab_result lr JOIN lab_order lo ON lo.lab_order_id = lr.lab_order_id
           WHERE lr.is_abnormal = 1""",
    ]:
        run(v)

    noise = 0
    for i in range(120):
        noise += run(f"CREATE TABLE audit_event_{i:03d} ("
                     f"event_id NUMBER(14) PRIMARY KEY, table_name VARCHAR2(60), "
                     f"changed_by VARCHAR2(80), changed_at TIMESTAMP, "
                     f"before_image CLOB)", quiet=True)
    for i in range(60):
        noise += run(f"CREATE TABLE stg_feed_{i:03d} ("
                     f"row_id NUMBER(14) PRIMARY KEY, payload VARCHAR2(4000), "
                     f"loaded_at TIMESTAMP, batch_ref VARCHAR2(40))", quiet=True)
    c.commit()
    cur.execute("SELECT COUNT(*) FROM user_tables")
    tables = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM user_constraints WHERE constraint_type='R'")
    fks = cur.fetchone()[0]
    c.close()
    print(f"  {tables} tables, {fks} foreign keys ({noise} of them audit/staging noise)")


# --------------------------------------------------------------------------

def seed_schema():
    """Rows, so a question has an answer and not just a table list. Small and
    deterministic: "four claims were denied" is checkable by eye."""
    import datetime as dt

    import oracledb
    c = oracledb.connect(user=USER, password=PW, dsn=DSN)
    cur = c.cursor()
    D = lambda s: dt.date.fromisoformat(s)                      # noqa: E731
    T = lambda s: dt.datetime.fromisoformat(s)                  # noqa: E731

    def ins(sql, rows):
        try:
            cur.executemany(sql, rows)
        except Exception as e:
            print("  ", sql.split()[2], "->", str(e).split("\n")[0])

    ins("INSERT INTO department VALUES (:1,:2,:3,:4)", [
        (1, "Emergency", "CC-100", 0), (2, "Cardiology", "CC-200", 3),
        (3, "Oncology", "CC-300", 4), (4, "Radiology", "CC-400", 1)])
    ins("INSERT INTO ward VALUES (:1,:2,:3,:4,:5)", [
        (1, 1, "Resus", 8, "emergency"), (2, 2, "Coronary Care", 12, "cardiology"),
        (3, 3, "Day Unit", 20, "oncology")])
    ins("INSERT INTO provider VALUES (:1,:2,:3,:4,:5,:6)", [
        (1, "Dr Amina Choudhury", "NPI-1001", 2, "cardiology", D("2015-04-01")),
        (2, "Dr Peter Lindqvist", "NPI-1002", 1, "emergency", D("2018-09-15")),
        (3, "Dr Rosa Marquez", "NPI-1003", 3, "oncology", D("2012-01-20")),
        (4, "Dr Yusuf Bello", "NPI-1004", 4, "radiology", D("2020-06-05"))])
    ins("INSERT INTO staff_salary VALUES (:1,:2,:3,:4,:5,:6)", [
        (1, 1, 142000, "GBP", "Consultant", D("2026-01-01")),
        (2, 2, 118500, "GBP", "Consultant", D("2026-01-01")),
        (3, 3, 155000, "GBP", "Senior Consultant", D("2026-01-01")),
        (4, 4, 96000, "GBP", "Registrar", D("2026-01-01"))])

    names = [("Ada","Okafor"),("Ben","Sharma"),("Chloe","Ferreira"),("Dmitri","Novak"),
             ("Elena","Rossi"),("Farid","Haddad"),("Grace","Mbeki"),("Hugo","Lindberg")]
    ins("INSERT INTO patient VALUES (:1,:2,:3,:4,:5,:6,:7)", [
        (i+1, f"MRN{1000+i}", g, f, D(f"19{60+i}-0{(i%9)+1}-1{i%9}"),
         "F" if i % 2 else "M", D("2024-03-01")) for i, (g, f) in enumerate(names)])
    ins("INSERT INTO patient_identifier VALUES (:1,:2,:3,:4,:5)", [
        (i+1, i+1, f"NI{700000+i}A", f"P{9000000+i}", f"{100+i}-45-{6000+i}")
        for i in range(8)])
    ins("INSERT INTO encounter VALUES (:1,:2,:3,:4,:5,:6,:7,:8)", [
        (i+1, i+1, (i%4)+1, (i%3)+1, T(f"2026-08-{10+i:02d} 09:00"),
         None if i < 3 else T(f"2026-08-{20+i:02d} 14:00"),
         "inpatient" if i < 5 else "outpatient", (i%4)+1) for i in range(8)])

    ins("INSERT INTO lab_panel VALUES (:1,:2,:3)", [
        ("FBC","Full Blood Count","blood"), ("UE","Urea and Electrolytes","blood"),
        ("LFT","Liver Function Tests","blood")])
    panels = ["FBC","UE","LFT"]
    ins("INSERT INTO lab_order VALUES (:1,:2,:3,:4,:5,:6)", [
        (i+1, (i%8)+1, panels[i%3], (i%4)+1, T(f"2026-08-{11+(i%8):02d} 10:30"),
         "routine" if i%3 else "urgent") for i in range(12)])
    analytes = [("haemoglobin",9.1,"g/dL",13.0,17.0,1), ("potassium",6.4,"mmol/L",3.5,5.3,1),
                ("alanine transaminase",88.0,"U/L",7.0,56.0,1), ("sodium",139.0,"mmol/L",135.0,145.0,0),
                ("creatinine",74.0,"umol/L",60.0,110.0,0), ("platelets",210.0,"10^9/L",150.0,400.0,0)]
    ins("INSERT INTO lab_result VALUES (:1,:2,:3,:4,:5,:6,:7,:8,:9)", [
        (i+1, (i%12)+1, *analytes[i%6], T(f"2026-08-{12+(i%8):02d} 16:00")) for i in range(18)])

    ins("INSERT INTO payer VALUES (:1,:2,:3,:4)", [
        (1,"NHS England","public","claims@nhs.example"),
        (2,"BUPA","private","claims@bupa.example"),
        (3,"AXA Health","private","claims@axa.example")])
    den = [None,"missing prior authorisation",None,"coding does not match diagnosis",
           None,"duplicate claim","patient not covered on date of service",None]
    ins("INSERT INTO claim VALUES (:1,:2,:3,:4,:5,:6,:7)", [
        (i+1, (i%8)+1, (i%3)+1, D(f"2026-08-{15+i:02d}"), round(400+i*137.5,2),
         "denied" if den[i] else "paid", den[i]) for i in range(8)])
    ins("INSERT INTO payment VALUES (:1,:2,:3,:4,:5)", [
        (i+1, i+1, D(f"2026-09-{1+i:02d}"), round(400+i*137.5,2), "bacs")
        for i in range(8) if not den[i]])

    ins("INSERT INTO medication VALUES (:1,:2,:3,:4,:5)", [
        (1,"Amoxicillin","capsule","500 mg",0), (2,"Morphine sulfate","injection","10 mg/mL",1),
        (3,"Furosemide","tablet","40 mg",0), (4,"Enoxaparin","injection","40 mg",0)])
    ins("INSERT INTO prescription VALUES (:1,:2,:3,:4,:5,:6,:7,:8)", [
        (i+1, (i%8)+1, (i%4)+1, (i%4)+1, "1 unit", "twice daily",
         D(f"2026-08-{12+i:02d}"), None) for i in range(10)])
    ins("INSERT INTO medication_administration VALUES (:1,:2,:3,:4,:5,:6)", [
        (i+1, (i%10)+1, (i%4)+1, T(f"2026-08-{13+(i%8):02d} 08:00"), "1 unit",
         1 if i == 5 else 0) for i in range(16)])
    ins("INSERT INTO procedure_catalogue VALUES (:1,:2,:3,:4)", [
        ("P100","Coronary angiography",60,1), ("P200","Hip replacement",150,1),
        ("P300","Cataract extraction",40,1), ("P400","Endoscopy",30,0)])
    ins("INSERT INTO waiting_list VALUES (:1,:2,:3,:4,:5)", [
        (i+1, (i%8)+1, ["P100","P200","P300","P400"][i%4],
         D(f"2026-0{5+(i%4)}-1{i%9}"), [42,126,84,28][i%4]) for i in range(10)])

    c.commit()
    cur.execute("SELECT COUNT(*) FROM claim WHERE status = 'denied'")
    denied = cur.fetchone()[0]
    c.close()
    print(f"  seeded: {denied} denied claims, 8 patients, 18 lab results")


def main():
    argv = sys.argv[1:]
    flags = {a for a in argv if a.startswith("--")}
    model = "claude-sonnet-4-5"
    args, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in ("--provider", "--model", "--sql"):
            skip = True
            continue
        if not a.startswith("--"):
            args.append(a)

    if "--stop" in flags:
        sh("docker", "rm", "-f", CONTAINER)
        print(f"removed {CONTAINER}.")
        return

    try:
        import oracledb  # noqa: F401
    except ImportError:
        sys.exit('pip install "schemagate[oracle]"  -- the driver is missing')

    start_oracle()

    if "--rebuild" in flags or not schema_exists():
        print("building the hospital schema...")
        build_schema()
        seed_schema()
    else:
        print("schema already there (--rebuild to recreate it).")

    from sqlalchemy import create_engine
    from schemagate import Catalog, Principal
    import schemagate

    print(f"\nreflecting with schemagate {schemagate.__version__}...",
          end=" ", flush=True)
    t0 = time.time()
    cat = Catalog().bootstrap(create_engine(URL))
    print(f"{len(cat)} objects in {time.time() - t0:.1f}s")

    # Two tables that must never reach the wrong caller. Applied at select
    # time: a restricted object is never in the candidate set, so no rewording
    # of a question can reach it.
    cat.restrict("patient_identifier", ["records"])
    cat.restrict("staff_salary", ["payroll"])
    WHO = {"nurse": Principal("hosp:nurse"),
           "payroll": Principal("hosp:hr", roles={"payroll"}),
           "records": Principal("hosp:records", roles={"records"})}

    whole = sum(len(d.render_ddl()) for d in cat.objects())

    def ask(question, who="nurse", show_prompt=False):
        sel = cat.select(question, top_k=6, principal=WHO[who])
        frag = sel.prompt_fragment()
        print(f"\nQ: {question}   [as {who}]")
        for d in sel.objects:
            print("     " + d.name)
        print(f"     {len(frag):,} chars instead of {whole:,} "
              f"({100 - len(frag) * 100 / whole:.1f}% less), "
              f"{len(sel.objects)} of {len(cat)} objects")
        if show_prompt:
            print("\n" + "-" * 66)
            print(frag, end="")
            print("-" * 66)
            return

        # The answer, not just the tables. The SQL only ever sees the objects
        # select() returned, so a table this caller may not see cannot appear
        # in it -- which is what makes the role check above worth anything.
        if "--answer" not in flags:
            return
        from schemagate.answer import (UnsafeSQL, format_rows, generate_sql,
                                       run_sql, sql_prompt)
        from sqlalchemy import create_engine
        engine = create_engine(URL)

        provider_name = None
        for i, a in enumerate(sys.argv):
            if a == "--provider" and i + 1 < len(sys.argv):
                provider_name = sys.argv[i + 1]
            if a == "--model" and i + 1 < len(sys.argv):
                model = sys.argv[i + 1]

        if not provider_name:
            print("\n-- No --provider given, so nothing is sent anywhere. Paste this")
            print("-- into any chat, then run the SQL it gives you with --sql \"...\"\n")
            print(sql_prompt(question, frag, "oracle"))
            return

        from schemagate.ai import providers as _p
        classes = {"anthropic": _p.AnthropicProvider, "openai": _p.OpenAIProvider,
                   "gemini": _p.GeminiProvider, "oci": _p.OCIGenAIProvider}
        try:
            provider = (_p.auto_provider(model) if provider_name == "auto"
                        else classes[provider_name](model=model))
            sql = generate_sql(provider, question, frag, "oracle")
        except UnsafeSQL as e:
            print(f"\n!! refused the generated SQL -- {e}")
            return
        except Exception as e:
            print(f"\n!! {type(e).__name__}: {e}")
            return
        print(f"\n-- SQL by {provider_name}/{model}, from {len(sel.objects)} tables")
        print(sql + "\n")
        cols, rows = run_sql(engine, sql, limit=50)
        print(format_rows(cols, rows))

    if "--sql" in sys.argv:
        from schemagate.answer import UnsafeSQL, format_rows, run_sql
        from sqlalchemy import create_engine
        q = sys.argv[sys.argv.index("--sql") + 1]
        try:
            cols, rows = run_sql(create_engine(URL), q, limit=50)
        except UnsafeSQL as e:
            print(f"refused that SQL -- {e}")
            return
        print(format_rows(cols, rows))
        return

    if args:
        ask(" ".join(args), show_prompt="--prompt" in flags)
        return

    print("""
Ask a question, or "quit". Put a role in front to change who is asking:

    what do we pay our doctors
    payroll: what do we pay our doctors
    records: patient national insurance and passport number

    which claims were denied and why
    which lab results came back abnormal
    who is currently admitted and on which ward
    how long do patients wait for a procedure
""")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("quit", "exit", ""):
            break
        who = "nurse"
        for role in WHO:
            if q.lower().startswith(role + ":"):
                who, q = role, q.split(":", 1)[1].strip()
        ask(q, who, show_prompt="--prompt" in flags)


if __name__ == "__main__":
    main()
