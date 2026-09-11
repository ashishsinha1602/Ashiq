"""Remember the last connection, so restarting the Studio is not retyping it.

The Studio holds its connection in the server process and nowhere else. That
is fine until the process ends -- and it ends often, because it is a local
tool people Ctrl+C and restart. Every restart put the page back on "Nothing
connected yet" and made someone re-enter a URL, or worse, a wallet directory
plus two passwords. The connection was never lost in the sense of a dropped
socket; it was simply never written down.

So it gets written down. What is stored is the connect request itself -- the
same body the page POSTs to /api/connect -- because that is the thing that is
replayable. Storing a resolved SQLAlchemy URL instead would lose the wallet
fields, which do not fit in a URL and are the case that hurts most to retype.

On secrets
----------
This file can contain a database password and a wallet password. That is a
real trade -- a credential on disk is a credential that can be read -- and it
is not one to make on someone's behalf, in a tool whose whole pitch is that it
reads your schema and stores nothing. So **nothing is written unless it is
asked for**: tick "Remember this connection" in the page, or start with
`--remember`. Without that the connection lives in the process, as it always
has, and a restart asks again.

When it is asked for:

* It is written 0600, and to the user's own home directory.
* An AI provider key is never included. Those stay in memory for the life of
  the process, as they always have -- they are not needed to reconnect.
* `--forget` deletes what is there.

Replaying is not gated the same way: if the file exists, someone already said
yes to it, and asking a second time on every start would just be a prompt to
click through.

On Windows, `chmod` cannot express 0600 -- the file inherits the ACL of the
user profile directory instead, which is user-scoped but not the same promise.
`describe()` says so rather than implying a protection that is not there.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["save", "load", "forget", "path", "enabled", "describe"]

#: Opt-in, not opt-out. The default is to write nothing.
ENV_ON = "SCHEMAGATE_REMEMBER"
ENV_HOME = "SCHEMAGATE_HOME"

#: Only fields that belong to *connecting* are kept. An allow-list rather than
#: a deny-list: the page posts a whole form body and new fields get added to
#: it, and the failure mode of a deny-list here is silently persisting
#: something that should never have been written.
#: These are exactly the keys `connBody()` in the page builds and `resolve()`
#: in connect.py reads. They are spelled the same on purpose -- what is stored
#: is the request, replayed verbatim, so any drift here shows up as a
#: connection that reconnects with a field missing.
_KEEP = frozenset({
    "kind",                                   # url | jdbc | wallet | ords | <dialect>
    "url", "jdbc",                            # url / jdbc / ords
    "wallet", "alias", "wallet_password",     # Autonomous Database wallet
    "host", "port", "database", "service_name", "driver",   # host-and-port
    "user", "password",                       # all of them
    "schema",                                 # ords
    "schemas", "restrict_from_grants", "sample_values",     # applies to all
})

#: Never written, whatever the caller passes.
_NEVER = frozenset({"api_key", "provider", "model"})


def enabled() -> bool:
    """Whether saving was asked for by the environment. Default: no."""
    return str(os.environ.get(ENV_ON, "")).strip().lower() in (
        "1", "true", "yes", "on")


def path() -> Path:
    base = os.environ.get(ENV_HOME)
    root = Path(base) if base else Path.home() / ".schemagate"
    return root / "connection.json"


def _harden(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)     # 0600
    except OSError:
        pass                                          # best effort, Windows


def save(body: Dict[str, Any], allow: bool = False) -> Optional[Path]:
    """Persist a connect request, if saving was asked for.

    `allow` is the per-request answer -- the page's checkbox, or --remember.
    The environment variable is the standing one. Either is enough; neither
    means nothing is written, which is the default.
    """
    if not (allow or enabled()) or not isinstance(body, dict):
        return None
    keep = {k: v for k, v in body.items()
            if k in _KEEP and k not in _NEVER and v not in (None, "", [], {})}
    if not keep:
        return None
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _harden(p.parent)
        # Open with the mode set at creation rather than chmod-ing afterwards,
        # so there is no window where the file exists world-readable with a
        # password already in it.
        fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "connection": keep}, fh, indent=2)
        _harden(p)
        return p
    except OSError:
        # Never let an unwritable home directory take down a connection that
        # otherwise worked.
        return None


def load() -> Optional[Dict[str, Any]]:
    """The last connect request, or None if there is not one to replay.

    Deliberately not gated on `enabled()`: the file only exists because
    someone asked for it, and refusing to read it back would make --remember
    a no-op on the very next run, which is the run it exists for.
    """
    p = path()
    try:
        raw = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    conn = raw.get("connection") if isinstance(raw, dict) else None
    if not isinstance(conn, dict) or not conn:
        return None
    return {k: v for k, v in conn.items() if k in _KEEP}


def forget() -> bool:
    """Delete the stored connection. True if there was one."""
    try:
        path().unlink()
        return True
    except OSError:
        return False


def describe() -> str:
    """One line for the CLI, with no secret in it."""
    p = path()
    if not p.exists():
        return ("nothing remembered -- tick 'Remember this connection', or "
                f"start with --remember ({p})")
    conn = load() or {}
    what = conn.get("kind") or ("url" if conn.get("url") else "connection")
    note = "" if os.name != "nt" else "  (Windows: protected by profile ACL, not 0600)"
    return f"remembered {what} at {p}{note}"
