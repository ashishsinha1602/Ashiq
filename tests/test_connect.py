"""Turning what people have into something SQLAlchemy can open.

The Connect box used to demand a SQLAlchemy URL, and most people do not have
one. An Oracle team has a wallet zip and a TNS alias. A SQL Server team has a
JDBC string out of a config file. Hand-translating those is where the tool
stops being usable, and Oracle in particular is easy to get subtly wrong.
"""
import os
import zipfile

import pytest

from schemagate.connect import (SUPPORTED, ConnectError, driver_hint,
                                from_jdbc, oracle_wallet, resolve,
                                tns_aliases)


# --- JDBC ------------------------------------------------------------------

@pytest.mark.parametrize("jdbc,expect", [
    ("jdbc:postgresql://h:5432/db", "postgresql+psycopg://h:5432/db"),
    ("jdbc:mysql://h:3306/db", "mysql+pymysql://h:3306/db"),
    ("jdbc:sqlserver://h:1433;databaseName=db",
     "mssql+pyodbc://h:1433/db?driver=ODBC+Driver+18+for+SQL+Server"),
])
def test_the_common_jdbc_shapes_translate(jdbc, expect):
    assert resolve(jdbc)[0] == expect


def test_oracle_service_name_does_not_become_a_sid():
    """The one worth care. JDBC writes `@//host:port/service`; SQLAlchemy
    wants `?service_name=`. The obvious translation treats the last segment as
    a SID, which connects to nothing and reports a login failure -- so the
    symptom points at the password, not at the URL."""
    url, _ = resolve("jdbc:oracle:thin:@//h:1521/ORCLPDB1")
    assert url == "oracle+oracledb://h:1521/?service_name=ORCLPDB1"


def test_the_older_host_port_sid_form_also_works():
    assert resolve("jdbc:oracle:thin:@h:1521:XE")[0] == \
        "oracle+oracledb://h:1521/?service_name=XE"


def test_credentials_ride_in_the_jdbc_string_and_are_picked_up():
    """They arrive as `?user=&password=`, or with semicolons on SQL Server.
    Copying a string out of a config file and having it half-work is worse
    than it not working at all."""
    url, _ = resolve("jdbc:postgresql://h/db?user=app&password=s3cret")
    assert url.startswith("postgresql+psycopg://app:s3cret@h/db")

    url, _ = resolve("jdbc:sqlserver://h;databaseName=d;user=sa;password=pw")
    assert "sa:pw@h" in url


def test_a_password_with_url_punctuation_is_encoded():
    """`@`, `:` and `/` in a password are common enough that not encoding them
    is a guaranteed bug report -- and it reads as a wrong password rather than
    as a parsing problem."""
    url, _ = resolve("jdbc:postgresql://h/db?user=u&password=p%40ss%2Fword")
    assert "p%40ss%2Fword@h" in url and url.count("@") == 1


def test_a_jdbc_driver_we_do_not_handle_says_so():
    with pytest.raises(ConnectError, match="unsupported"):
        from_jdbc("jdbc:db2://h:50000/db")


def test_something_that_is_not_a_connection_string_is_rejected():
    with pytest.raises(ConnectError, match="not a connection string"):
        resolve("my database")


# --- Oracle wallet ---------------------------------------------------------

def _wallet(tmp_path, nested=False):
    d = tmp_path / ("inner" if nested else "w")
    d.mkdir(parents=True)
    (d / "tnsnames.ora").write_text(
        "mydb_high = (description=(address=(host=a)))\n"
        "mydb_low  = (description=(address=(host=a)))\n")
    (d / "sqlnet.ora").write_text("WALLET_LOCATION = (SOURCE=(METHOD=file))\n")
    (d / "cwallet.sso").write_text("x")
    return d


def test_a_wallet_directory_becomes_connect_args_not_a_url(tmp_path):
    """None of this fits in a URL, which is why `resolve` returns two things.
    A caller that drops the second gets `oracle+oracledb://@` and a connection
    to nowhere."""
    d = _wallet(tmp_path)
    url, args = oracle_wallet(str(d), "mydb_high", "ADMIN", "pw", "walletpw")
    assert url == "oracle+oracledb://@"
    assert args["dsn"] == "mydb_high"
    assert args["config_dir"] == args["wallet_location"] == str(d)
    assert args["user"] == "ADMIN" and args["wallet_password"] == "walletpw"


def test_a_flat_zip_as_the_console_ships_it(tmp_path):
    d = _wallet(tmp_path)
    z = tmp_path / "flat.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in d.iterdir():
            zf.write(f, f.name)
    _, args = oracle_wallet(str(z), "mydb_high")
    assert os.path.isfile(os.path.join(args["config_dir"], "tnsnames.ora"))


def test_a_zip_with_one_folder_inside_it(tmp_path):
    """A wallet someone re-zipped themselves. Pointing config_dir at the
    parent means the driver never finds tnsnames.ora and reports that the
    alias does not exist."""
    d = _wallet(tmp_path, nested=True)
    z = tmp_path / "nested.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in d.iterdir():
            zf.write(f, f"inner/{f.name}")
    _, args = oracle_wallet(str(z), "mydb_high")
    assert os.path.isfile(os.path.join(args["config_dir"], "tnsnames.ora"))


def test_a_zip_that_writes_outside_its_directory_is_refused(tmp_path):
    """A wallet is a file downloaded from a console, not a hostile input --
    but nothing here needs to trust that."""
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(ConnectError, match="escapes"):
        oracle_wallet(str(z), "mydb_high")


def test_the_aliases_can_be_listed_so_nobody_has_to_remember_them(tmp_path):
    assert tns_aliases(str(_wallet(tmp_path))) == ["mydb_high", "mydb_low"]


def test_a_wallet_without_an_alias_is_refused(tmp_path):
    with pytest.raises(ConnectError, match="TNS alias"):
        oracle_wallet(str(_wallet(tmp_path)), "")


def test_a_missing_wallet_says_so(tmp_path):
    with pytest.raises(ConnectError, match="no wallet"):
        oracle_wallet(str(tmp_path / "nope"), "a")


# --- the form shapes -------------------------------------------------------

def test_host_and_port_forms_for_each_engine():
    assert resolve({"kind": "postgresql", "host": "h", "port": 5432,
                    "database": "db", "user": "u", "password": "p"})[0] == \
        "postgresql+psycopg://u:p@h:5432/db"
    assert resolve({"kind": "oracle", "host": "h", "port": 1521,
                    "database": "FREEPDB1"})[0] == \
        "oracle+oracledb://h:1521/?service_name=FREEPDB1"
    assert "driver=ODBC+Driver+18" in resolve(
        {"kind": "mssql", "host": "h", "database": "db"})[0]


def test_an_unknown_kind_is_named_in_the_error():
    with pytest.raises(ConnectError, match="db2"):
        resolve({"kind": "db2"})


def test_every_supported_engine_names_the_extra_that_provides_its_driver():
    """"driver not installed" is only useful if it says what to install."""
    for kind, (prefix, extra) in SUPPORTED.items():
        if kind == "sqlite":
            continue
        assert extra and extra.startswith("schemagate[")
        assert driver_hint(f"{prefix}://h/db") == extra


# --- taking the connection away with you -----------------------------------

def test_a_password_is_never_printed_back(monkeypatch):
    """Studio shows the command that reproduces a connection, and a command
    with a live password in it lands in a screenshot, a chat message and shell
    history -- the three places a password is hardest to recall from."""
    from schemagate.connect import PASSWORD_PLACEHOLDER, recipe, redact

    url = "postgresql+psycopg://app:s3cret@h:5432/db"
    assert "s3cret" not in redact(url)
    assert PASSWORD_PLACEHOLDER in redact(url)

    r = recipe(*resolve(url))
    assert "s3cret" not in r["cli"] and "s3cret" not in r["python"]


def test_the_placeholder_is_an_env_var_not_stars():
    """`***` is not runnable. The point is a command someone can paste and
    use, with the secret coming from the environment."""
    from schemagate.connect import recipe
    assert "$DB_PASSWORD" in recipe(*resolve(
        "postgresql+psycopg://u:p@h/db"))["cli"]


def test_the_wallet_password_is_masked_too(tmp_path):
    from schemagate.connect import recipe
    d = _wallet(tmp_path)
    url, args = oracle_wallet(str(d), "mydb_high", "ADMIN", "pw", "walletsecret")
    r = recipe(url, args)
    assert "walletsecret" not in r["cli"] and "$WALLET_PASSWORD" in r["cli"]
    assert "pw" not in r["cli"].replace("$DB_PASSWORD", "")


def test_the_flags_used_to_connect_are_in_the_command():
    """Otherwise the command is not the connection that was made -- it is a
    different, quieter one that happens to reach the same database."""
    from schemagate.connect import recipe
    r = recipe(*resolve("postgresql+psycopg://u:p@h/db"), schemas=["med", "hr"],
               restrict_from_grants=True, sample_values=True)
    assert "--schema med" in r["cli"] and "--schema hr" in r["cli"]
    assert "--restrict-from-grants" in r["cli"] and "--values" in r["cli"]


def test_a_wallet_connection_carries_its_connect_args(tmp_path):
    """A wallet is not a URL, so a command with only `--url` in it connects to
    nothing. The env var is the part that matters and it has to be there."""
    from schemagate.connect import recipe
    url, args = oracle_wallet(str(_wallet(tmp_path)), "mydb_high", "ADMIN", "pw")
    cli = recipe(url, args)["cli"]
    assert "SCHEMAGATE_CONNECT_ARGS" in cli and "mydb_high" in cli
