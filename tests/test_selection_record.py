"""The audit record for one selection.

What an auditor asks for after the fact, and the thing that is impossible to
reconstruct later: the catalog will have changed, roles will have changed, and
the question is gone.
"""
from schemagate import Catalog, Principal
from schemagate.models import Column, ForeignKey, ObjectDoc


def cat():
    c = Catalog()
    c.add(ObjectDoc(name="employee", schema="hr",
                    columns=[Column("id", "INTEGER", pk=True),
                             Column("full_name", "TEXT"),
                             Column("salary", "NUMERIC"),
                             Column("manager_id", "INTEGER")],
                    foreign_keys=[ForeignKey(["salary"], "pay_band")]))
    c.restrict_column("employee", "salary", ["payroll"])
    c.index()
    return c


PAYROLL = Principal("okta:hr", roles={"payroll", "staff"})
ANALYST = Principal("okta:analyst")


def test_the_record_has_the_shape_an_audit_needs():
    rec = cat().select("employee pay", principal=PAYROLL).to_dict()
    assert rec["question"] == "employee pay"
    assert rec["principal"] == "okta:hr"
    assert rec["roles"] == ["payroll", "staff"]          # sorted, not set order
    assert rec["total_objects"] == 1
    hit = rec["hits"][0]
    assert set(hit) == {"object", "kind", "score", "reason",
                        "columns_shown", "columns_withheld"}
    assert hit["object"] == "hr.employee" and hit["kind"] == "TABLE"


def test_a_withheld_column_is_counted_as_withheld_not_shown():
    rec = cat().select("employee pay", principal=ANALYST).to_dict()
    hit = rec["hits"][0]
    assert hit["columns_shown"] == 3 and hit["columns_withheld"] == 1


def test_the_role_holder_has_nothing_withheld():
    hit = cat().select("employee pay", principal=PAYROLL).to_dict()["hits"][0]
    assert hit["columns_shown"] == 4 and hit["columns_withheld"] == 0


def test_an_anonymous_selection_records_that_it_was_anonymous():
    rec = cat().select("employee pay").to_dict()
    assert rec["principal"] is None and rec["roles"] == []
    assert rec["hits"][0]["columns_withheld"] == 1        # still fails closed


def test_the_record_never_names_the_columns_it_withheld():
    """A log that lists what it withheld has disclosed it to everyone who can
    read the log -- which is usually more people than could read the column."""
    import json
    assert "salary" not in json.dumps(cat().select("employee pay").to_dict())


def test_the_fragment_and_the_record_describe_the_same_caller():
    """A selection rendered for someone other than the principal it was scored
    for is a hole. The principal travels on the Selection so the two cannot
    disagree."""
    sel = cat().select("employee pay", principal=ANALYST)
    assert "salary" not in sel.prompt_fragment()
    assert sel.to_dict()["hits"][0]["columns_withheld"] == 1

    sel = cat().select("employee pay", principal=PAYROLL)
    assert "salary" in sel.prompt_fragment()
    assert sel.to_dict()["hits"][0]["columns_withheld"] == 0


def test_a_restriction_survives_into_the_prompt_end_to_end():
    """Moved here from the column-restriction tests: it needs Selection to
    carry the principal."""
    c = cat()
    assert "salary" not in c.select("employee pay", principal=ANALYST).prompt_fragment()
    assert "salary" in c.select("employee pay", principal=PAYROLL).prompt_fragment()


def test_the_fk_line_is_dropped_in_the_fragment_too():
    frag = cat().select("employee pay", principal=ANALYST).prompt_fragment()
    assert "pay_band" not in frag
