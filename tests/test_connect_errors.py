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


def test_only_the_first_line_comes_back():
    out = safe_error(Boom("DPY-6005: cannot connect\nHelp: https://example/dpy-6005\nstack"))
    assert "stack" not in out and "DPY-6005" in out


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
