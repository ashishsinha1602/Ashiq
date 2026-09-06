import pytest
from ashiq import Principal, IdentityError

@pytest.mark.parametrize("bad", ["", "   ", "nonamespace", "no colon here", "okta:"])
def test_rejects_unnamespaced_subjects(bad):
    with pytest.raises(IdentityError):
        Principal(bad)

def test_accepts_namespaced():
    for good in ["okta:jdoe", "db:APP_USER", "tenant:42", "svc-1:bot"]:
        assert Principal(good).subject == good

def test_scope_is_deterministic_and_stable():
    a, b = Principal("okta:jdoe"), Principal("okta:jdoe", roles={"ops"})
    assert a.scope() == b.scope()          # roles must not change identity
    assert len(a.scope()) == 16
    assert a.scope() != Principal("db:jdoe").scope()   # namespace matters

def test_scope_does_not_leak_subject():
    assert "jdoe" not in Principal("okta:jdoe").scope()

def test_repr_hides_roles():
    assert "secret" not in repr(Principal("okta:jdoe", roles={"secret"}))

def test_roles_frozen_and_hashable():
    p = Principal("okta:jdoe", roles={"a"})
    assert hash(p) is not None
    with pytest.raises(Exception):
        p.subject = "okta:other"
