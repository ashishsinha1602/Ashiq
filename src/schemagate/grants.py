"""Derive object visibility from the database's own GRANTs.

At forty tables a hand-written `restrict` map is fine. At four hundred it is a
second copy of an access-control list that already exists -- in the directory,
and in the database. Two copies drift, and drift in an ACL is the failure this
library exists to prevent: the catalog says a table is restricted long after
the grant was revoked, or worse, says nothing while the grant was added.

So read the real one. `restrict_from_grants` fills in `doc.roles` from the
privileges the server actually holds, and reports what it could not match
rather than guessing.

Two rules matter more than the SQL.

An object absent from the grant map is left untouched. Silence is not a
denial: the reader may lack visibility into a schema, the object may be a view
the dialect's grant view does not cover, or the reflection may have come from
somewhere else entirely. Restricting on absence would break a working catalog
the first time a connection could not see everything, and it would look like
the library was working.

A grant to PUBLIC clears roles instead of inventing a `public` role. `not
doc.roles` already means everyone, and a role named "public" that every
principal must remember to hold is a trap that fails closed for real users.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

__all__ = ["GrantReport", "GRANT_READERS", "ROLE_GRAPH_READERS",
           "grantees_by_object", "restrict_from_grants", "expand_roles"]

#: dialect name -> callable(engine) -> {(schema, object): {grantee, ...}}
GRANT_READERS: Dict[str, Callable] = {}
#: dialect name -> callable(engine) -> {role: {roles that INHERIT it, ...}}
#:
#: The direction is the whole correctness of this feature and it is easy to
#: get backwards -- I did, and a live PostgreSQL caught it. `GRANT a TO b`
#: makes b a member of a, so b inherits a's privileges. An object granted to
#: `a` is therefore reachable by `a` and by everything that inherits `a`.
#: Mapping "roles a is a member of" instead expands upward and hands the
#: object to every parent role -- an over-grant, which is the direction that
#: actually leaks.
ROLE_GRAPH_READERS: Dict[str, Callable] = {}


@dataclass
class GrantReport:
    """What was read, what was applied, and what could not be."""
    dialect: str = ""
    objects_seen: int = 0
    objects_restricted: int = 0
    objects_public: int = 0
    #: In the catalog but not in the grant map. Left untouched, and named here
    #: so "nothing happened" is visible rather than silent.
    objects_unmatched: List[str] = field(default_factory=list)
    roles_expanded: int = 0
    warnings: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        bits = [f"{self.dialect}: {self.objects_seen} object(s) seen",
                f"{self.objects_restricted} restricted",
                f"{self.objects_public} public",
                f"{len(self.objects_unmatched)} unmatched",
                f"{self.roles_expanded} role(s) expanded"]
        out = ", ".join(bits)
        for w in self.warnings:
            out += f"\n  warning: {w}"
        return out


# --------------------------------------------------------------------------
# role graphs
# --------------------------------------------------------------------------

def expand_roles(direct: Set[str], graph: Dict[str, Set[str]]) -> Set[str]:
    """``direct`` plus every role that inherits one of them.

    Nested roles are the Entra case: a user whose group maps to a role that
    inherits the granted one must still reach the object. Walking one level
    puts it out of reach of exactly the people the directory says should have
    it.

    ``graph`` maps a role to the roles that inherit it, not the other way
    round. The inverse expands upward and grants the object to every parent
    role, which is an over-grant -- and an over-grant in an ACL is the one
    direction that leaks rather than annoys.

    Cycle-safe, because a role graph is user data and `GRANT a TO b` alongside
    `GRANT b TO a` is legal in both engines and would otherwise hang.
    """
    seen, stack = set(), list(direct)
    while stack:
        role = stack.pop()
        if role in seen:
            continue
        seen.add(role)
        stack.extend(graph.get(role, ()))
    return seen


#: The public name is `expand_roles` and so is the parameter that switches it
#: on, so inside `restrict_from_grants` the parameter shadows the function.
#: An alias is clearer than renaming either -- the brief names both.
_expand = expand_roles


# --------------------------------------------------------------------------
# reading the grants
# --------------------------------------------------------------------------

def grantees_by_object(engine) -> Dict[Tuple[Optional[str], str], Set[str]]:
    """{(schema, object) -> {grantee}} for this engine's dialect."""
    name = engine.dialect.name
    reader = GRANT_READERS.get(name)
    if reader is None:
        raise NotImplementedError(
            f"reading grants is not implemented for {name!r}; "
            f"supported: {', '.join(sorted(GRANT_READERS)) or 'none'}")
    return reader(engine)


def restrict_from_grants(catalog, engine, *, expand_roles: bool = True,
                         public_grantees: Sequence[str] = ("PUBLIC",),
                         report: bool = False):
    """Set ``doc.roles`` on every catalog object from the database's grants.

    Returns the catalog, or a ``GrantReport`` when ``report=True``.
    """
    rep = GrantReport(dialect=engine.dialect.name)
    grants = grantees_by_object(engine)
    rep.objects_seen = len(grants)

    graph: Dict[str, Set[str]] = {}
    if expand_roles:
        reader = ROLE_GRAPH_READERS.get(engine.dialect.name)
        if reader is not None:
            try:
                graph = reader(engine)
            except Exception as e:                    # noqa: BLE001
                # An unreadable role graph must never look like "no nesting".
                # Silently flattening one level under-grants, which reads as
                # the library working and the directory being wrong.
                rep.warnings.append(
                    f"could not read the role graph ({type(e).__name__}: {e}); "
                    "nested roles were not expanded")

    public = {g.casefold() for g in public_grantees}
    # The catalog and the dictionary rarely agree on case: Oracle stores
    # identifiers upper, PostgreSQL lower, and reflection preserves whatever
    # it found. Match on casefold so a correct grant is not missed over it.
    folded = {(s.casefold() if s else None, o.casefold()): v
              for (s, o), v in grants.items()}

    for doc in list(catalog._docs.values()):
        key = (doc.schema.casefold() if doc.schema else None, doc.name.casefold())
        grantees = folded.get(key)
        if grantees is None and key[0] is not None:
            grantees = folded.get((None, key[1]))     # unqualified reflection
        if grantees is None:
            rep.objects_unmatched.append(doc.qname)
            continue
        if any(g.casefold() in public for g in grantees):
            doc.roles = None
            rep.objects_public += 1
            continue
        roles = {g for g in grantees}
        if graph:
            before = len(roles)
            roles = _expand(roles, graph)
            rep.roles_expanded += max(0, len(roles) - before)
        doc.roles = sorted(roles)
        rep.objects_restricted += 1

    catalog._stale = True
    return rep if report else catalog


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------

#: `aclexplode` on `pg_class.relacl`, not `information_schema.role_table_grants`.
#: That view only returns rows where the connected user is the grantor, the
#: grantee, or a member of the grantee -- so it hides grants the reader is not
#: party to, and hidden grants make us *under*-restrict, which is the direction
#: that matters. A NULL relacl means default privileges, which is owner-only,
#: so the owner pass is not an optimisation: without it every untouched table
#: reads as having no grants at all.
_PG_SQL = """
    SELECT n.nspname, c.relname,
           -- grantee 0 is PUBLIC, and pg_get_userbyid(0) renders it as
           -- 'unknown (OID=0)', not 'PUBLIC'. Found against a live server:
           -- without this the PUBLIC rule never fires on PostgreSQL and every
           -- world-readable table comes back restricted to a role nobody holds.
           CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                ELSE pg_get_userbyid(a.grantee) END AS grantee
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      CROSS JOIN LATERAL aclexplode(c.relacl) AS a
     WHERE c.relkind IN ('r','v','m','p','f')
       AND c.relacl IS NOT NULL
       AND a.privilege_type = 'SELECT'
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    UNION
    SELECT n.nspname, c.relname, pg_get_userbyid(c.relowner)
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r','v','m','p','f')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""

#: `roleid` is the role being granted; `member` is the role that receives it
#: and therefore inherits its privileges. Keyed by the granted role, valued by
#: the ones that inherit it -- see ROLE_GRAPH_READERS for why the direction is
#: the whole ballgame.
_PG_ROLES = """
    SELECT r.rolname AS granted, m.rolname AS inheritor
      FROM pg_auth_members am
      JOIN pg_roles m ON m.oid = am.member
      JOIN pg_roles r ON r.oid = am.roleid
"""


def _pg_grants(engine):
    out: Dict[Tuple[Optional[str], str], Set[str]] = {}
    with engine.connect() as conn:
        for schema, obj, grantee in conn.exec_driver_sql(_PG_SQL).fetchall():
            out.setdefault((schema, obj), set()).add(str(grantee))
    return out


def _pg_role_graph(engine):
    graph: Dict[str, Set[str]] = {}
    with engine.connect() as conn:
        for granted, inheritor in conn.exec_driver_sql(_PG_ROLES).fetchall():
            graph.setdefault(str(granted), set()).add(str(inheritor))
    return graph


# --------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------

#: Oracle does not record a self-grant, so an owner has every privilege on its
#: own tables and `all_tab_privs` says nothing about it. Without the owner pass
#: every table a user owns would come back with no grantees and be reported as
#: unmatched.
_ORA_SQL = """
    SELECT table_schema, table_name, grantee
      FROM all_tab_privs
     WHERE privilege = 'SELECT'
    UNION
    SELECT owner, table_name, owner FROM all_tables
    UNION
    SELECT owner, view_name, owner FROM all_views
"""

#: `grantee` receives `granted_role` and so inherits its privileges. Keyed by
#: the granted role, valued by the ones that inherit it.
_ORA_ROLES = "SELECT granted_role, grantee FROM dba_role_privs"
_ORA_ROLES_FALLBACK = "SELECT granted_role, username FROM user_role_privs"


def _ora_grants(engine):
    out: Dict[Tuple[Optional[str], str], Set[str]] = {}
    with engine.connect() as conn:
        for schema, obj, grantee in conn.exec_driver_sql(_ORA_SQL).fetchall():
            out.setdefault((schema, obj), set()).add(str(grantee))
    return out


def _ora_role_graph(engine):
    graph: Dict[str, Set[str]] = {}
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql(_ORA_ROLES).fetchall()
        except Exception:
            # dba_role_privs needs a privilege an application user rarely has.
            # user_role_privs covers the connected user only, which is less
            # than the whole graph but more than nothing.
            rows = conn.exec_driver_sql(_ORA_ROLES_FALLBACK).fetchall()
        for granted, inheritor in rows:
            graph.setdefault(str(granted), set()).add(str(inheritor))
    return graph


GRANT_READERS["postgresql"] = _pg_grants
GRANT_READERS["oracle"] = _ora_grants
ROLE_GRAPH_READERS["postgresql"] = _pg_role_graph
ROLE_GRAPH_READERS["oracle"] = _ora_role_graph
