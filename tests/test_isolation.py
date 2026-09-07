"""The core thesis: an object the caller may not read must never reach the prompt."""
import pytest
from schemagate import Principal

HR = Principal("okta:analyst")
PAYROLL = Principal("okta:hrlead", roles={"payroll"})

def test_restricted_object_hidden_from_unauthorised(cat):
    cat.restrict("hr_compensation", ["payroll"])
    sel = cat.select("salary and pay grade by employee", top_k=10, principal=HR)
    assert "hr_compensation" not in {d.name for d in sel.objects}

def test_restricted_object_visible_to_authorised(cat):
    cat.restrict("hr_compensation", ["payroll"])
    sel = cat.select("salary and pay grade by employee", top_k=10, principal=PAYROLL)
    assert "hr_compensation" in {d.name for d in sel.objects}

def test_restricted_name_absent_from_prompt_text(cat):
    """Not just the object list -- the rendered prompt must not mention it,
    including via a foreign key pulled in from a permitted table."""
    cat.restrict("hr_compensation", ["payroll"])
    sel = cat.select("annual compensation per employee", top_k=10, principal=HR)
    assert "hr_compensation" not in sel.prompt_fragment().lower()

def test_no_principal_means_restricted_objects_are_denied(cat):
    """Fail closed: an unauthenticated caller gets less, never more."""
    cat.restrict("hr_compensation", ["payroll"])
    sel = cat.select("salary by employee", top_k=10, principal=None)
    assert "hr_compensation" not in {d.name for d in sel.objects}

def test_fk_expansion_cannot_smuggle_restricted_objects(cat):
    cat.restrict("crm_customer", ["sales"])
    sel = cat.select("order lines and totals", top_k=8, principal=HR)
    assert "crm_customer" not in {d.name for d in sel.objects}

def test_unrestricted_objects_visible_to_everyone(cat):
    a = {d.name for d in cat.select("revenue by month", principal=HR).objects}
    b = {d.name for d in cat.select("revenue by month", principal=PAYROLL).objects}
    assert a == b
