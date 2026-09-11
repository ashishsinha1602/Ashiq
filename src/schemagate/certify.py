"""Certify schemagate against a real database, whatever the dialect.

Lives in the package rather than in `scripts/` because it did not work
otherwise. `schemagate certify` looked for the file three directories above
the installed module, which exists in a git checkout and nowhere else -- so
every user who installed from PyPI got a link to GitHub instead of a
certification run, and the comment explaining the fallback said the script was
in the sdist, which it also was not.

`scripts/certify_dialect.py` still runs; it now calls this.
"""
from __future__ import annotations


PREFIX = "schemagate_cert_"


def build_metadata(sa):
    meta = sa.MetaData()
    sa.Table(
        f"{PREFIX}customer", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_number", sa.String(40), nullable=False),
        sa.Column("segment", sa.String(40)),
    )
    sa.Table(
        f"{PREFIX}invoice", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("id_customer", sa.Integer,
                  sa.ForeignKey(f"{PREFIX}customer.id")),
        sa.Column("total_gross", sa.Numeric(12, 2)),
        sa.Column("status", sa.String(20)),
    )
    sa.Table(
        f"{PREFIX}payroll", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("annual_amount", sa.Numeric(12, 2)),
    )
    return meta


def main(url: str) -> int:
    try:
        import sqlalchemy as sa
    except ImportError:
        print("SQLAlchemy is required: pip install schemagate")
        return 2

    from schemagate import Catalog, Principal
    from schemagate.introspect import reflect

    checks, failures = [], []

    def check(name, fn):
        try:
            fn()
            checks.append(name)
            print(f"  PASS  {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")

    # Driver options that do not fit in a URL -- an Oracle wallet, for one:
    #   SCHEMAGATE_CONNECT_ARGS='{"config_dir": "/w", "wallet_location": "/w", "wallet_password": "..."}'
    from schemagate.introspect import connect_args_from_env
    connect_args = connect_args_from_env()
    engine = sa.create_engine(url, connect_args=connect_args)
    print(f"dialect: {engine.dialect.name}  driver: {engine.dialect.driver}")
    try:
        with engine.connect() as conn:
            version = conn.exec_driver_sql("SELECT 1").scalar()
        print(f"connection: OK (SELECT 1 -> {version})")
    except Exception as e:
        print(f"connection: FAILED -- {type(e).__name__}: {e}")
        return 2

    meta = build_metadata(sa)
    meta.drop_all(engine, checkfirst=True)
    meta.create_all(engine)
    print(f"created {len(meta.tables)} tables prefixed {PREFIX!r}\n")

    try:
        docs = reflect(engine, include=[f"{PREFIX}%"])
        by_name = {d.name.lower(): d for d in docs}

        check("reflects every table",
              lambda: _assert(len(by_name) >= 3, f"found {sorted(by_name)}"))

        check("captures columns and types", lambda: _assert(
            all(c.type for c in by_name[f"{PREFIX}customer"].columns),
            "a column type came back empty"))

        check("captures primary keys", lambda: _assert(
            any(c.pk for c in by_name[f"{PREFIX}customer"].columns),
            "no primary key detected"))

        check("captures foreign keys", lambda: _assert(
            any(f"{PREFIX}customer" in fk.ref_table.lower()
                for fk in by_name[f"{PREFIX}invoice"].foreign_keys),
            "the invoice -> customer foreign key was not reflected"))

        check("include patterns filter", lambda: _assert(
            {d.name.lower() for d in reflect(engine, include=[f"{PREFIX}inv%"])}
            == {f"{PREFIX}invoice"}, "include pattern did not filter"))

        cat = Catalog(name="certify")
        cat.add_all(docs)
        cat.index()

        check("selection returns relevant objects", lambda: _assert(
            f"{PREFIX}invoice" in
            {d.name.lower() for d in cat.select("invoice totals", top_k=3).objects},
            "the invoice table was not selected for an invoice question"))

        check("foreign-key expansion works", lambda: _assert(
            any(h.reason == "fk" for h in
                cat.select("invoice totals per customer", top_k=2).hits)
            or f"{PREFIX}customer" in
            {d.name.lower() for d in cat.select("invoice totals per customer",
                                                top_k=3).objects},
            "the joined customer table was never pulled in"))

        cat.restrict(f"{PREFIX}payroll", ["payroll"])

        check("restricted object hidden from an unauthorised caller",
              lambda: _assert(
                  f"{PREFIX}payroll" not in
                  {d.name.lower() for d in cat.select(
                      "annual amount", top_k=10,
                      principal=Principal("db:ANALYST")).objects},
                  "a restricted object reached the prompt"))

        check("restricted object visible to an authorised caller",
              lambda: _assert(
                  f"{PREFIX}payroll" in
                  {d.name.lower() for d in cat.select(
                      "annual amount", top_k=10,
                      principal=Principal("db:HR", roles=frozenset({"payroll"}))).objects},
                  "an authorised caller could not see the object"))

        check("prompt fragment renders", lambda: _assert(
            "(" in cat.select("invoice totals", top_k=2).prompt_fragment(),
            "prompt_fragment produced nothing usable"))
    finally:
        meta.drop_all(engine, checkfirst=True)
        engine.dispose()
        print(f"\ndropped {PREFIX!r} tables")

    print(f"\n{len(checks)} passed, {len(failures)} failed")
    if failures:
        print(f"\n{engine.dialect.name.upper()} NOT CERTIFIED")
        print("Please open an issue with the output above:")
        print("  https://github.com/ashishsinha1602/schemagate/issues")
        return 1
    print(f"\n{engine.dialect.name.upper()} CERTIFIED "
          f"(SQLAlchemy {__import__('sqlalchemy').__version__})")
    return 0


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)
