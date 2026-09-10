"""Deriving visibility from the database's own GRANTs.

No database needed: the readers are a registry, so a fake reader exercises
everything except the vendor SQL itself. That SQL is verified separately
against live PostgreSQL and Oracle, because a query no instance has ever run
is not tested.
"""
import pytest

from schemagate import Catalog, Principal
from schemagate.grants import (GRANT_READERS, ROLE_GRAPH_READERS, expand_roles, grantees_by_object,
                               restrict_from_grants)
from schemagate.models import Column, ObjectDoc


class FakeEngine:
    def __init__(self, name="fakedb"):
        class D:
            pass
        self.dialect = D()
        self.dialect.name = name


def catalog_of(*names):
    cat = Catalog()
    for n in names:
        schema, _, table = n.rpartition(".")
        cat.add(ObjectDoc(name=table, schema=schema or None,
                          columns=[Column("id", "INTEGER", pk=True)]))
    cat.index()
    return cat


@pytest.fixture
def registered():
    """A dialect with a fake reader, cleaned up afterwards."""
    made = []

    def register(grants, roles=None, name="fakedb"):
        GRANT_READERS[name] = lambda e: grants
        if roles is not None:
            ROLE_GRAPH_READERS[name] = lambda e: roles
        made.append(name)
        return FakeEngine(name)

    yield register
    for n in made:
        GRANT_READERS.pop(n, None)
        ROLE_GRAPH_READERS.pop(n, None)


# --- matching --------------------------------------------------------------

def test_roles_come_from_the_grant_map(registered):
    eng = registered({("hr", "employee"): {"payroll"}})
    cat = catalog_of("hr.employee")
    restrict_from_grants(cat, eng)
    assert cat._docs["hr.employee"].roles == ["payroll"]


def test_matching_is_case_insensitive(registered):
    """Oracle stores identifiers upper, PostgreSQL lower, and reflection keeps
    whatever it found. A correct grant must not be missed over case."""
    eng = registered({("HR", "EMPLOYEE"): {"PAYROLL"}})
    cat = catalog_of("hr.employee")
    restrict_from_grants(cat, eng)
    assert cat._docs["hr.employee"].roles == ["PAYROLL"]


def test_an_unqualified_reflection_still_matches(registered):
    eng = registered({(None, "employee"): {"payroll"}})
    cat = catalog_of("hr.employee")
    restrict_from_grants(cat, eng)
    assert cat._docs["hr.employee"].roles == ["payroll"]


# --- the two rules that matter --------------------------------------------

def test_a_grant_to_public_clears_roles_rather_than_inventing_one(registered):
    """`not doc.roles` already means everyone. A role literally named "public"
    that every principal must remember to hold fails closed for real users."""
    eng = registered({("hr", "holiday"): {"PUBLIC"}})
    cat = catalog_of("hr.holiday")
    cat._docs["hr.holiday"].roles = ["stale"]
    rep = restrict_from_grants(cat, eng, report=True)
    assert cat._docs["hr.holiday"].roles is None
    assert rep.objects_public == 1


def test_an_object_with_no_grant_row_is_left_untouched(registered):
    """Silence is not a denial. The reader may lack visibility into a schema,
    or the object may not be covered by the dialect's grant view. Restricting
    on absence breaks a working catalog and looks like the library working."""
    eng = registered({("hr", "employee"): {"payroll"}})
    cat = catalog_of("hr.employee", "hr.orphan")
    cat._docs["hr.orphan"].roles = ["kept"]
    rep = restrict_from_grants(cat, eng, report=True)
    assert cat._docs["hr.orphan"].roles == ["kept"]
    assert rep.objects_unmatched == ["hr.orphan"]


# --- nested roles ----------------------------------------------------------

def test_nested_roles_are_flattened_transitively(registered):
    """The Entra case: a user whose group maps to a role that inherits the
    granted one must still reach the object. One level of walking puts it out
    of reach of exactly the people the directory says should have it.

    The graph maps a role to the roles that INHERIT it. Here the object is
    granted to `hr_staff`; `payroll_reader` inherits `hr_staff`, and `intern`
    inherits `payroll_reader`, so all three reach it."""
    eng = registered({("hr", "employee"): {"hr_staff"}},
                     roles={"hr_staff": {"payroll_reader"},
                            "payroll_reader": {"intern"}})
    cat = catalog_of("hr.employee")
    rep = restrict_from_grants(cat, eng, report=True)
    assert set(cat._docs["hr.employee"].roles) == {"hr_staff", "payroll_reader",
                                                   "intern"}
    assert rep.roles_expanded == 2


def test_expansion_never_walks_upward_to_a_parent_role(registered):
    """The bug a live PostgreSQL caught. Expanding the wrong way hands an
    object granted to a narrow role to every broader role above it -- an
    over-grant, which is the direction that leaks rather than annoys.

    `everyone` does not inherit `payroll_reader`, so an object granted to
    `payroll_reader` must not become visible to holders of `everyone`."""
    eng = registered({("hr", "salary"): {"payroll_reader"}},
                     roles={"everyone": {"hr_staff"}, "hr_staff": {"payroll_reader"}})
    cat = catalog_of("hr.salary")
    restrict_from_grants(cat, eng)
    assert cat._docs["hr.salary"].roles == ["payroll_reader"]


def test_role_expansion_can_be_switched_off(registered):
    eng = registered({("hr", "employee"): {"payroll_reader"}},
                     roles={"payroll_reader": {"hr_staff"}})
    cat = catalog_of("hr.employee")
    restrict_from_grants(cat, eng, expand_roles=False)
    assert cat._docs["hr.employee"].roles == ["payroll_reader"]


def test_a_cyclic_role_graph_terminates():
    """`GRANT a TO b` alongside `GRANT b TO a` is legal in both engines."""
    assert expand_roles({"a"}, {"a": {"b"}, "b": {"a"}}) == {"a", "b"}


def test_an_unreadable_role_graph_warns_rather_than_under_granting(registered):
    """Silently flattening one level under-grants, which reads as the library
    working and the directory being wrong."""
    def boom(engine):
        raise PermissionError("ORA-01031: insufficient privileges")

    eng = registered({("hr", "employee"): {"payroll"}})
    ROLE_GRAPH_READERS["fakedb"] = boom
    rep = restrict_from_grants(cat := catalog_of("hr.employee"), eng, report=True)
    assert rep.warnings and "role graph" in rep.warnings[0]
    assert cat._docs["hr.employee"].roles == ["payroll"]


# --- the report ------------------------------------------------------------

def test_the_report_counts_what_happened(registered):
    eng = registered({("s", "a"): {"r1"}, ("s", "b"): {"PUBLIC"}})
    rep = restrict_from_grants(catalog_of("s.a", "s.b", "s.c"), eng, report=True)
    assert (rep.objects_seen, rep.objects_restricted, rep.objects_public) == (2, 1, 1)
    assert rep.objects_unmatched == ["s.c"]
    assert "fakedb" in str(rep) and "unmatched" in str(rep)


# --- end to end ------------------------------------------------------------

def test_selection_is_gated_by_the_derived_roles(registered):
    """The point of the whole module: a grant read from the server has to
    actually keep the object out of the prompt."""
    eng = registered({("hr", "employee"): {"payroll"}, ("hr", "holiday"): {"PUBLIC"}})
    cat = catalog_of("hr.employee", "hr.holiday")
    restrict_from_grants(cat, eng)
    cat.index()

    anon = [d.name for d in cat.select("employee holiday").objects]
    assert "employee" not in anon and "holiday" in anon

    who = Principal("okta:hr", roles={"payroll"})
    assert "employee" in [d.name for d in cat.select("employee holiday",
                                                     principal=who).objects]


# --- unsupported dialects --------------------------------------------------

def test_an_unsupported_dialect_names_itself_and_the_supported_ones():
    with pytest.raises(NotImplementedError) as e:
        grantees_by_object(FakeEngine("sqlite"))
    msg = str(e.value)
    assert "sqlite" in msg and "postgresql" in msg and "oracle" in msg


def test_both_shipped_dialects_are_registered():
    assert {"postgresql", "oracle"} <= set(GRANT_READERS)
    assert {"postgresql", "oracle"} <= set(ROLE_GRAPH_READERS)


def test_public_is_recognised_by_its_oid_not_its_rendered_name():
    """`pg_get_userbyid(0)` renders PUBLIC as 'unknown (OID=0)'. Found against
    a live server: without the CASE, the PUBLIC rule never fires on PostgreSQL
    and every world-readable table comes back restricted to a role nobody
    holds."""
    from schemagate.grants import _PG_SQL
    assert "a.grantee = 0" in _PG_SQL and "'PUBLIC'" in _PG_SQL


def test_the_role_graph_is_keyed_by_the_granted_role(registered):
    """Direction is the whole correctness of this feature. pg_auth_members
    roleid is the role granted; member is the role that inherits it."""
    from schemagate.grants import _PG_ROLES, _ORA_ROLES
    assert "granted" in _PG_ROLES and "inheritor" in _PG_ROLES
    assert _ORA_ROLES.index("granted_role") < _ORA_ROLES.index("grantee")


def test_postgres_reads_the_acl_not_the_information_schema_view():
    """`information_schema.role_table_grants` only returns rows where the
    connected user is grantor, grantee, or a member of the grantee. It hides
    grants, and hidden grants make us under-restrict -- the direction that
    matters."""
    from schemagate.grants import _PG_SQL
    assert "aclexplode" in _PG_SQL and "relacl" in _PG_SQL
    assert "role_table_grants" not in _PG_SQL
    assert "relowner" in _PG_SQL          # NULL relacl means owner-only


def test_oracle_covers_the_owner_because_self_grants_are_not_recorded():
    from schemagate.grants import _ORA_SQL
    assert "all_tab_privs" in _ORA_SQL
    assert "all_tables" in _ORA_SQL and "all_views" in _ORA_SQL
