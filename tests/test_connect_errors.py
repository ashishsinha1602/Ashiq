"""What a failed connection is allowed to say.

The whole driver message was being dropped because it quotes the connect
string, and a connect string carries a password. What reached the user was the
exception class: "OperationalError". That is the same word for a blocked port,
a wrong password, and an alias that does not resolve -- three problems with
nothing in common except that you cannot tell which one you have.
"""
import pytest

from schemagate.connect import safe_error


class Boom(Exception):
    pass


@pytest.mark.parametrize("msg,code", [
    ("DPY-6005: cannot connect to database. Cannot connect to host x:1522", "DPY-6005"),
    ("ORA-01017: invalid username/password; logon denied", "ORA-01017"),
    ("ORA-12154: TNS:could not resolve the connect identifier specified", "ORA-12154"),
    ("ORA-12506: TNS:listener rejected connection", "ORA-12506"),
    ("DPY-4011: the database or network closed the connection", "DPY-4011"),
])
def test_the_driver_code_survives(msg, code):
    """The code is the diagnosis. It is also not a secret."""
    assert code in safe_error(Boom(msg))


def test_the_password_does_not():
    out = safe_error(Boom("ORA-01017: logon denied for user=admin password=hunter2"),
                     "hunter2")
    assert "hunter2" not in out
    assert "***" in out
    assert "ORA-01017" in out


def test_every_secret_given_is_masked():
    out = safe_error(Boom("failed: pw=s3cret wallet=w4llet"), "s3cret", "w4llet")
    assert "s3cret" not in out and "w4llet" not in out


def test_a_none_secret_is_not_a_crash():
    assert "ORA-12154" in safe_error(Boom("ORA-12154: bad alias"), None, "")


def test_an_uncoded_error_still_names_its_type():
    out = safe_error(Boom("something opaque"))
    assert "Boom" in out and "something opaque" in out


def test_a_stack_trace_does_not_come_along():
    """Bounded, not first-line-only: the reason lives on line two, so taking
    one line threw away the diagnosis. What must not follow is an unbounded
    tail -- a traceback can drag a connect string, and that carries a
    password."""
    out = safe_error(Boom("DPY-6005: cannot connect\ntimed out\n" +
                          "\n".join(f"  File \"x.py\", line {i}" for i in range(40))))
    assert "DPY-6005" in out and "timed out" in out
    assert len(out) <= 400
    assert out.count("File ") <= 1


def test_a_code_further_down_is_hoisted_to_the_front():
    """A form field is narrow; the code must not be the part that gets cut."""
    out = safe_error(Boom("sqlalchemy wrapped this\nDPY-4011: closed"))
    assert out.startswith("DPY-4011")


def test_an_empty_message_degrades_to_the_class():
    assert safe_error(Boom("")) == "Boom"


def test_the_sqlalchemy_class_prefix_is_stripped():
    """SQLAlchemy wraps the driver message with the exception class in
    parentheses. Left in, it pushes the useful half out of a narrow field and
    makes the hoisted code appear twice."""
    out = safe_error(Boom(
        "(oracledb.exceptions.OperationalError) DPY-6005: cannot connect to database"))
    assert out.startswith("DPY-6005")
    assert "oracledb.exceptions" not in out
    assert out.count("DPY-6005") == 1


def test_a_short_secret_is_not_masked_into_the_message():
    """Masking by substring means a one-character password rewrites every
    occurrence of that letter: "oracledb.exceptions" became
    "oracledb.exce***tions", destroying the diagnosis to hide a fragment that
    was never recoverable from the text anyway."""
    out = safe_error(Boom("(oracledb.exceptions.OperationalError) DPY-6005: cannot connect"), "p")
    assert "exceptions" in out or "DPY-6005" in out
    assert "exce***tions" not in out


def test_a_real_password_is_still_masked():
    out = safe_error(Boom("ORA-01017: denied pw=Str0ng#Passw0rd_2026"),
                     "Str0ng#Passw0rd_2026")
    assert "Str0ng#Passw0rd_2026" not in out and "***" in out


# ---- the cause, not just the headline -----------------------------------

def test_the_reason_on_the_second_line_survives():
    """oracledb puts the headline on line one and the reason on line two.
    "timed out" and "Connection refused" are different problems -- a firewall
    swallowing packets versus nothing listening -- and reporting both as
    "DPY-6005: cannot connect" is exactly the collapsing this function exists
    to undo."""
    timed = safe_error(Boom("DPY-6005: cannot connect to database (CONNECTION_ID=a).\ntimed out"))
    refused = safe_error(Boom("DPY-6005: cannot connect to database (CONNECTION_ID=a).\n[Errno 111] Connection refused"))
    assert "timed out" in timed
    assert "Connection refused" in refused
    assert timed != refused


def test_help_urls_are_dropped():
    out = safe_error(Boom("ORA-28759: failure to open file\nHelp: https://docs.oracle.com/x"))
    assert "ORA-28759" in out and "docs.oracle.com" not in out


def test_a_proxy_is_taken_from_the_environment_for_oracle():
    """A network that blocks 1522 outbound usually still allows an HTTPS
    proxy, and oracledb can tunnel through one."""
    import os

    from schemagate.connect import with_timeout

    for var in ("SCHEMAGATE_ORACLE_PROXY", "https_proxy", "HTTPS_PROXY"):
        os.environ.pop(var, None)
    assert "https_proxy" not in with_timeout("oracle+oracledb://@", {})

    os.environ["SCHEMAGATE_ORACLE_PROXY"] = "proxy.corp.example:8080"
    try:
        out = with_timeout("oracle+oracledb://@", {})
        assert out["https_proxy"] == "proxy.corp.example"
        assert out["https_proxy_port"] == 8080
        # and it is Oracle-specific: psycopg would reject the keyword
        assert "https_proxy" not in with_timeout("postgresql+psycopg://h/d", {})
    finally:
        os.environ.pop("SCHEMAGATE_ORACLE_PROXY", None)


def test_an_explicit_proxy_is_not_overridden():
    import os

    from schemagate.connect import with_timeout

    os.environ["SCHEMAGATE_ORACLE_PROXY"] = "env.example:8080"
    try:
        out = with_timeout("oracle+oracledb://@", {"https_proxy": "mine.example",
                                                   "https_proxy_port": 3128})
        assert out["https_proxy"] == "mine.example" and out["https_proxy_port"] == 3128
    finally:
        os.environ.pop("SCHEMAGATE_ORACLE_PROXY", None)
