"""Turn what people actually have into something SQLAlchemy can open.

The Connect box asks for a SQLAlchemy URL, and most people do not have one.
An Oracle team has a wallet zip and a TNS alias. A SQL Server team has a JDBC
string from a config file. Telling them to hand-translate it is where the tool
stops being usable, and it is the kind of translation that is easy to get
subtly wrong -- a JDBC Oracle URL and a SQLAlchemy one disagree about where
the service name goes, and the failure reads as a login error.

Two things come out of here, always together:

    url, connect_args = resolve(spec)
    engine = create_engine(url, connect_args=connect_args)

`connect_args` matters because some connections cannot be a URL at all. An
Autonomous Database needs a wallet directory and a wallet password, which have
nowhere to live in a URL, so the URL degenerates to `oracle+oracledb://@` and
everything real travels beside it. That is the same shape
`SCHEMAGATE_CONNECT_ARGS` has always used; this builds it rather than making
someone write the JSON.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, quote_plus, urlsplit

__all__ = ["resolve", "from_jdbc", "oracle_wallet", "SUPPORTED", "ConnectError"]

#: What the Studio form offers. Each is a real driver that has to be installed
#: -- the extra is named so the error can say which.
SUPPORTED = {
    "postgresql": ("postgresql+psycopg", "schemagate[postgres]"),
    "oracle": ("oracle+oracledb", "schemagate[oracle]"),
    "mssql": ("mssql+pyodbc", "schemagate[mssql]"),
    "mysql": ("mysql+pymysql", "schemagate[mysql]"),
    "sqlite": ("sqlite", None),
}


class ConnectError(ValueError):
    """A connection spec that cannot be turned into a URL."""


def _q(v: Optional[str]) -> str:
    """Percent-encode a credential for a URL.

    Passwords contain `@`, `/` and `:` often enough that not doing this is a
    guaranteed bug report, and one that reads as "wrong password" rather than
    as a parsing problem.
    """
    return quote_plus(v or "")


def _auth(user: Optional[str], password: Optional[str]) -> str:
    if not user:
        return ""
    return f"{_q(user)}:{_q(password)}@" if password else f"{_q(user)}@"


# --------------------------------------------------------------------------
# JDBC
# --------------------------------------------------------------------------

def from_jdbc(jdbc: str, user: Optional[str] = None,
              password: Optional[str] = None,
              driver: str = "ODBC Driver 18 for SQL Server") -> Tuple[str, Dict[str, Any]]:
    """Translate a JDBC URL into a SQLAlchemy one.

    Credentials in a JDBC string usually ride as `?user=&password=` or, for
    SQL Server, as `;user=;password=`. They are read from there when the
    caller does not pass them explicitly, because copying a string out of a
    config file and having it half-work is worse than it not working.
    """
    raw = jdbc.strip()
    if not raw.lower().startswith("jdbc:"):
        raise ConnectError("not a JDBC URL")
    body = raw[5:]

    # SQL Server uses semicolons for properties, everyone else uses a query
    # string. Normalise both into one dict.
    props: Dict[str, str] = {}
    if ";" in body:
        head, _, tail = body.partition(";")
        for part in tail.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                props[k.strip().lower()] = v.strip()
        body = head
    if "?" in body:
        head, _, tail = body.partition("?")
        props.update({k.lower(): v for k, v in parse_qsl(tail)})
        body = head

    user = user or props.get("user") or props.get("username")
    password = password or props.get("password")

    if body.startswith("oracle:thin:"):
        return _jdbc_oracle(body, user, password, props)
    if body.startswith("sqlserver://"):
        host = body[len("sqlserver://"):]
        db = props.get("databasename") or props.get("database") or ""
        return (f"mssql+pyodbc://{_auth(user, password)}{host}/{db}"
                f"?driver={quote_plus(driver)}", {})
    if body.startswith(("postgresql://", "postgres://")):
        host = body.split("://", 1)[1]
        return f"postgresql+psycopg://{_auth(user, password)}{host}", {}
    if body.startswith(("mysql://", "mariadb://")):
        host = body.split("://", 1)[1]
        return f"mysql+pymysql://{_auth(user, password)}{host}", {}
    if body.startswith("sqlite:"):
        return f"sqlite:///{body.split(':', 1)[1]}", {}
    raise ConnectError(f"unsupported JDBC driver in {raw.split(':')[1]!r}")


def _jdbc_oracle(body: str, user, password, props) -> Tuple[str, Dict[str, Any]]:
    """`jdbc:oracle:thin:@...` in the three shapes people paste.

    The service-name form is the one worth care: JDBC writes
    `@//host:port/service` and SQLAlchemy wants `?service_name=`. Treating the
    trailing segment as a SID instead -- which is what the obvious translation
    does -- connects to nothing and reports a login failure.
    """
    at = body.find("@")
    if at < 0:
        raise ConnectError("JDBC Oracle URL has no @")
    target = body[at + 1:]

    # A wallet alias: @dbname_high with TNS_ADMIN pointing at the wallet.
    tns_admin = props.get("tns_admin") or props.get("oracle.net.tns_admin")
    if tns_admin and "/" not in target and ":" not in target:
        return oracle_wallet(tns_admin, target, user, password,
                             props.get("wallet_password") or props.get("oracle.net.wallet_password"))

    if target.startswith("//"):                       # //host:port/service
        hostport, _, service = target[2:].partition("/")
        if not service:
            raise ConnectError("JDBC Oracle URL has no service name")
        return (f"oracle+oracledb://{_auth(user, password)}{hostport}"
                f"/?service_name={quote_plus(service)}", {})

    if ":" in target:                                 # host:port:SID
        bits = target.split(":")
        if len(bits) == 3:
            host, port, sid = bits
            return (f"oracle+oracledb://{_auth(user, password)}{host}:{port}"
                    f"/?service_name={quote_plus(sid)}", {})
    # A bare alias with no TNS_ADMIN: resolvable only if the environment
    # already points at a tnsnames.ora, which is worth saying out loud.
    return (f"oracle+oracledb://{_auth(user, password)}",
            {"dsn": target, **({"user": user} if user else {}),
             **({"password": password} if password else {})})


# --------------------------------------------------------------------------
# Oracle wallet
# --------------------------------------------------------------------------

def oracle_wallet(wallet: str, alias: str, user: Optional[str] = None,
                  password: Optional[str] = None,
                  wallet_password: Optional[str] = None
                  ) -> Tuple[str, Dict[str, Any]]:
    """An Autonomous Database wallet: a directory or the zip as downloaded.

    None of this fits in a URL, so the URL is empty and the connection lives
    entirely in `connect_args` -- which is why `resolve` returns both and why
    callers must pass the second to `create_engine`.

    A zip is extracted next to itself rather than into a temporary directory:
    the driver reads `sqlnet.ora` and the wallet files at connect time and
    every time it reconnects, so a directory that disappears when this
    function returns produces a connection that works once.
    """
    import os
    import zipfile

    path = os.path.abspath(os.path.expanduser(wallet))
    if not os.path.exists(path):
        raise ConnectError(f"no wallet at {wallet!r}")
    if zipfile.is_zipfile(path):
        target = os.path.splitext(path)[0] + "_extracted"
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            # Refuse a zip that writes outside its directory. A wallet is a
            # file someone downloaded from a console; it is not hostile, but
            # nothing here needs to trust that.
            for name in z.namelist():
                dest = os.path.abspath(os.path.join(target, name))
                if not dest.startswith(target + os.sep) and dest != target:
                    raise ConnectError(f"wallet zip escapes its directory: {name!r}")
            z.extractall(target)
        path = target
        # A wallet downloaded from the OCI console is flat, but a zip someone
        # made themselves usually has one folder inside it. Point at whichever
        # directory actually holds tnsnames.ora, or the driver looks in the
        # wrong place and reports that the alias does not exist.
        if not os.path.isfile(os.path.join(path, "tnsnames.ora")):
            nested = [os.path.join(path, n) for n in os.listdir(path)
                      if os.path.isdir(os.path.join(path, n))
                      and os.path.isfile(os.path.join(path, n, "tnsnames.ora"))]
            if len(nested) == 1:
                path = nested[0]
    if not os.path.isdir(path):
        raise ConnectError(f"wallet must be a directory or a zip: {wallet!r}")
    if not alias:
        raise ConnectError("a wallet needs a TNS alias, e.g. mydb_high")

    args: Dict[str, Any] = {"config_dir": path, "wallet_location": path,
                            "dsn": alias}
    if wallet_password:
        args["wallet_password"] = wallet_password
    if user:
        args["user"] = user
    if password:
        args["password"] = password
    return "oracle+oracledb://@", args


def tns_aliases(wallet_dir: str) -> "list[str]":
    """Alias names from a wallet's tnsnames.ora, so a form can offer them
    rather than asking someone to remember `_high` versus `_low`."""
    import os

    p = os.path.join(os.path.abspath(os.path.expanduser(wallet_dir)), "tnsnames.ora")
    if not os.path.isfile(p):
        return []
    text = open(p, encoding="utf-8", errors="replace").read()
    return sorted({m.group(1) for m in
                   re.finditer(r"^\s*([A-Za-z0-9_$.]+)\s*=", text, re.M)})


# --------------------------------------------------------------------------
# the one entry point
# --------------------------------------------------------------------------

def resolve(spec: "str | Mapping[str, Any]") -> Tuple[str, Dict[str, Any]]:
    """``(url, connect_args)`` from a URL, a JDBC string, or a form."""
    if isinstance(spec, str):
        raw = spec.strip()
        if not raw:
            raise ConnectError("empty connection")
        if raw.lower().startswith("jdbc:"):
            return from_jdbc(raw)
        if "://" not in raw:
            raise ConnectError(
                "not a connection string. Use a SQLAlchemy URL "
                "(postgresql+psycopg://...), a JDBC URL (jdbc:oracle:thin:@...), "
                "or the wallet fields.")
        return raw, {}

    kind = str(spec.get("kind") or "url").lower()
    user = spec.get("user") or None
    password = spec.get("password") or None

    if kind == "url":
        return resolve(str(spec.get("url") or ""))
    if kind == "jdbc":
        return from_jdbc(str(spec.get("jdbc") or spec.get("url") or ""),
                         user, password)
    if kind in ("wallet", "oracle-wallet"):
        return oracle_wallet(str(spec.get("wallet") or ""),
                             str(spec.get("alias") or ""), user, password,
                             spec.get("wallet_password") or None)
    if kind in SUPPORTED:
        prefix, _ = SUPPORTED[kind]
        host = str(spec.get("host") or "localhost")
        port = spec.get("port")
        database = str(spec.get("database") or "")
        if kind == "sqlite":
            return f"sqlite:///{database}", {}
        hostport = f"{host}:{port}" if port else host
        if kind == "oracle":
            service = spec.get("service_name") or database
            return (f"{prefix}://{_auth(user, password)}{hostport}"
                    f"/?service_name={quote_plus(str(service))}", {})
        if kind == "mssql":
            driver = str(spec.get("driver") or "ODBC Driver 18 for SQL Server")
            return (f"{prefix}://{_auth(user, password)}{hostport}/{database}"
                    f"?driver={quote_plus(driver)}", {})
        return f"{prefix}://{_auth(user, password)}{hostport}/{database}", {}
    raise ConnectError(f"unknown connection kind {kind!r}")


def driver_hint(url: str) -> Optional[str]:
    """The extra to install for this URL, when the driver is missing."""
    scheme = urlsplit(url).scheme.split("+")[0]
    for kind, (prefix, extra) in SUPPORTED.items():
        if prefix.split("+")[0] == scheme:
            return extra
    return None
