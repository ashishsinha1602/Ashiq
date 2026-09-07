"""Identity scoping.

Every write and every read in schemagate carries a Principal. This is the whole
point of the library: schema selection happens *before* the database gets a
chance to enforce row-level security, so if selection is not identity-aware
the model can be handed tables the caller cannot read. The resulting SQL is
valid, RLS/VPD empties it, and the user is told "no records found" instead of
"access denied" -- a silent wrong answer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

_SUBJECT_RE = re.compile(r"^[a-z0-9_-]+:[^\s]+$", re.IGNORECASE)

PUBLIC_SCOPE = None  # rows visible to every principal


class IdentityError(ValueError):
    """Raised when a subject is missing or badly formed."""


@dataclass(frozen=True)
class Principal:
    """A namespaced caller identity.

    ``subject`` must be namespaced by source to prevent collision between
    identity providers -- ``okta:jdoe`` and ``db:JDOE`` are different
    people even though the local parts look alike.

    >>> Principal("okta:jdoe", roles={"ops"}).scope()
    '237bd30aac90b8f8'
    """

    subject: str
    roles: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.subject or not self.subject.strip():
            raise IdentityError("subject must be a non-empty string")
        if not _SUBJECT_RE.match(self.subject):
            raise IdentityError(
                f"subject {self.subject!r} must be namespaced as '<source>:<id>', "
                "e.g. 'okta:jdoe', 'db:APP_USER', 'tenant:42'"
            )
        object.__setattr__(self, "roles", frozenset(self.roles))

    @property
    def source(self) -> str:
        return self.subject.split(":", 1)[0].lower()

    def scope(self) -> str:
        """Stable 16-hex-char scope key. Stored, not the raw subject.

        Hashing means the catalog table never holds usernames, which keeps it
        out of scope for most PII review.
        """
        return hashlib.sha256(self.subject.encode("utf-8")).hexdigest()[:16]

    def has_any_role(self, roles: Optional[FrozenSet[str]]) -> bool:
        if not roles:
            return True
        return bool(self.roles & frozenset(roles))

    def __repr__(self) -> str:  # never leak roles into logs by accident
        return f"Principal({self.subject!r})"


def require(principal: Optional[Principal]) -> Principal:
    """Guard for call sites that must not run unscoped."""
    if principal is None:
        raise IdentityError(
            "a Principal is required here; pass principal=... "
            "(use Principal('system:bootstrap') for admin/bootstrap work)"
        )
    return principal
