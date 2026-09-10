"""Restricting a single column.

The object-level rule cannot express the common case: the table is the right
answer and one column in it is not. Salary on an employee table. A national
insurance number on a patient. Restricting the whole object makes the question
unanswerable; leaving it open puts the column in the prompt.
"""
import pytest

from schemagate import Catalog, Principal
from schemagate.models import Column, ForeignKey, ObjectDoc, allowed


def emp():
    return ObjectDoc(
        name="employee", schema="hr",
        columns=[Column("id", "INTEGER", pk=True),
                 Column("full_name", "TEXT"),
                 Column("salary", "NUMERIC", roles=["payroll"]),
                 Column("manager_id", "INTEGER")],
        foreign_keys=[ForeignKey(["manager_id"], "employee"),
                      ForeignKey(["salary"], "pay_band")])


PAYROLL = Principal("okta:hr", roles={"payroll"})
ANALYST = Principal("okta:analyst")


def test_a_restricted_column_is_absent_not_masked():
    """Not `REDACTED`, not renamed, not commented out. The name is itself the
    disclosure: `ssn REDACTED` tells a model the table holds one, and a model
    that knows a column exists can ask about it, join on it, or mention it in
    an explanation. A name never in the prompt cannot be referenced."""
    ddl = emp().render_ddl(principal=ANALYST)
    assert "salary" not in ddl
    for leak in ("REDACTED", "restricted", "hidden", "***", "witheld", "withheld"):
        assert leak.lower() not in ddl.lower()
    assert "full_name" in ddl          # the rest of the table still renders


def test_the_role_holder_sees_it():
    assert "salary NUMERIC" in emp().render_ddl(principal=PAYROLL)


def test_no_principal_fails_closed():
    """The important half. An anonymous caller failing open hands every
    restricted column to the first request that forgot to say who it was
    for."""
    assert "salary" not in emp().render_ddl()
    assert "salary" not in emp().render_ddl(principal=None)


def test_the_foreign_key_line_does_not_put_the_name_back():
    """The easy bug: render_ddl removes `salary`, and then a line reading
    `-- FK employee(salary) -> pay_band` reintroduces the exact identifier."""
    ddl = emp().render_ddl(principal=ANALYST)
    assert "pay_band" not in ddl and "salary" not in ddl
    assert "-- FK employee(manager_id) -> employee" in ddl    # unaffected FK stays

    full = emp().render_ddl(principal=PAYROLL)
    assert "pay_band" in full


def test_visible_columns_is_the_same_answer_render_uses():
    assert [c.name for c in emp().visible_columns(ANALYST)] == \
        ["id", "full_name", "manager_id"]
    assert [c.name for c in emp().visible_columns(PAYROLL)] == \
        ["id", "full_name", "salary", "manager_id"]


def test_max_columns_counts_visible_ones_not_all_of_them():
    """Otherwise a restricted column silently eats one of the slots the
    caller was entitled to see."""
    d = ObjectDoc(name="t", columns=[Column("a", "INT"), Column("b", "INT", roles=["x"]),
                                     Column("c", "INT"), Column("d", "INT")])
    ddl = d.render_ddl(max_columns=3, principal=ANALYST)
    assert "a INT" in ddl and "c INT" in ddl and "d INT" in ddl
    assert "more columns" not in ddl


def test_one_rule_governs_objects_and_columns():
    """Two copies of a visibility rule drift, and a drifted ACL is the bug
    this library exists to prevent."""
    assert allowed(None, None) is True
    assert allowed([], None) is True
    assert allowed(["payroll"], None) is False
    assert allowed(["payroll"], ANALYST) is False
    assert allowed(["payroll"], PAYROLL) is True


# --- the catalog API -------------------------------------------------------

def _cat():
    cat = Catalog()
    cat.add(emp())
    return cat


def test_restrict_column_applies_by_bare_name_or_qname():
    for table in ("employee", "hr.employee"):
        cat = _cat()
        cat.restrict_column(table, "salary", ["payroll"])
        assert cat._docs["hr.employee"].columns[2].roles == ["payroll"]


def test_an_unknown_table_raises_rather_than_passing():
    """A typo in an ACL that reports success is a restriction that silently
    is not there."""
    with pytest.raises(KeyError, match="nosuch"):
        _cat().restrict_column("nosuch", "salary", ["payroll"])


def test_an_unknown_column_raises_too():
    with pytest.raises(KeyError, match="wages"):
        _cat().restrict_column("employee", "wages", ["payroll"])

# The end-to-end check -- that a restriction survives into prompt_fragment --
# lives in test_selection_record.py, because it needs Selection to carry the
# principal, which is the next phase's job.
