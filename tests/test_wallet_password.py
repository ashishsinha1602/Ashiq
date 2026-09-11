"""The wallet password precheck.

This exists because of one failure that cost a day. A wallet password that
does not match *this* wallet does not produce a wallet error: the driver reads
the wallet, cannot decrypt ewallet.pem, never presents a client certificate,
TLS completes anyway, and the mTLS-required database hangs up. What arrives is
"DPY-4011: the database or network closed the connection" -- which points at
the network, and the network is fine.
"""
import zipfile

import pytest

from schemagate.connect import ConnectError, check_wallet_password, oracle_wallet

crypto = pytest.importorskip("cryptography")


def _wallet(tmp_path, pem_password=b"right-password", alias="db_high"):
    """A wallet directory with a real encrypted PEM in it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (serialization.BestAvailableEncryption(pem_password) if pem_password
           else serialization.NoEncryption())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8, enc)
    d = tmp_path / "wallet"
    d.mkdir()
    (d / "ewallet.pem").write_bytes(pem)
    (d / "tnsnames.ora").write_text(f"{alias} = (description=(address=(host=x)))\n")
    (d / "sqlnet.ora").write_text("SSL_SERVER_DN_MATCH=yes\n")
    return d


def test_the_right_password_passes(tmp_path):
    check_wallet_password(str(_wallet(tmp_path)), "right-password")


def test_a_password_from_a_different_download_is_named_as_such(tmp_path):
    """Two wallets for the same database are both called Wallet_DBNAME.zip and
    have different passwords. That is the trap."""
    with pytest.raises(ConnectError) as e:
        check_wallet_password(str(_wallet(tmp_path)), "the-previous-download")
    msg = str(e.value)
    assert "does not open this wallet" in msg
    assert "not the database password" in msg


def test_no_password_for_an_encrypted_wallet_says_which_password(tmp_path):
    with pytest.raises(ConnectError) as e:
        check_wallet_password(str(_wallet(tmp_path)), None)
    assert "download dialog" in str(e.value)


def test_an_unencrypted_pem_needs_no_password(tmp_path):
    check_wallet_password(str(_wallet(tmp_path, pem_password=None)), None)


def test_a_wallet_without_a_pem_is_left_alone(tmp_path):
    """No ewallet.pem means the wallet was downloaded without a password --
    a different problem, with its own message, further down."""
    d = tmp_path / "w"
    d.mkdir()
    (d / "tnsnames.ora").write_text("db_high = (description=())\n")
    check_wallet_password(str(d), "anything")


def test_the_check_runs_before_the_connection_is_built(tmp_path):
    """It has to fire from oracle_wallet, not from the caller: the whole point
    is that the driver's own error arrives too late to be useful."""
    w = _wallet(tmp_path)
    with pytest.raises(ConnectError, match="does not open this wallet"):
        oracle_wallet(str(w), "db_high", "user", "pw", "wrong-one")


def test_the_right_password_still_builds_the_connection(tmp_path):
    url, args = oracle_wallet(str(_wallet(tmp_path)), "db_high",
                              "appuser", "dbpw", "right-password")
    assert url == "oracle+oracledb://@"
    assert args["dsn"] == "db_high"
    assert args["wallet_password"] == "right-password"
    assert args["user"] == "appuser"


def test_a_zip_is_checked_after_extraction(tmp_path):
    """The zip is extracted first, so the PEM to check is the extracted one."""
    w = _wallet(tmp_path)
    z = tmp_path / "Wallet_DB.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in w.iterdir():
            zf.write(f, f.name)
    with pytest.raises(ConnectError, match="does not open this wallet"):
        oracle_wallet(str(z), "db_high", "user", "pw", "wrong-one")
