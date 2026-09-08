"""The OCI stack is Terraform and cloud-init, not Python, so nothing else in
the suite would notice if it broke. These pin the two contracts that a live
apply has actually caught."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STACK = Path(__file__).resolve().parent.parent / "oci" / "stack"

pytestmark = pytest.mark.skipif(not STACK.is_dir(), reason="stack not in this tree")


def _cloud_init() -> str:
    return (STACK / "cloud-init.yaml").read_text()


def test_every_template_placeholder_is_a_variable_main_tf_passes():
    """`templatefile()` fails the whole plan on an unknown `${...}`, and a
    shell variable written `${VAR}` inside cloud-init looks exactly like one."""
    main = (STACK / "main.tf").read_text()
    block = main[main.index("templatefile(") : main.index("templatefile(") + 800]
    passed = set(re.findall(r"^\s+(\w+)\s+=", block, re.M))
    used = set(re.findall(r"\$\{(\w+)\}", _cloud_init()))
    assert not (used - passed), (
        f"cloud-init uses ${{{sorted(used - passed)}}}, which templatefile() does "
        "not pass. Write shell variables as $VAR, not ${VAR}."
    )


def test_cataloguing_retries_because_it_races_the_policy_that_authorises_it():
    """The dynamic group's matching rule needs the instance OCID, so Terraform
    cannot create it -- or the GenAI policy -- until the instance already
    exists and is running cloud-init. A single attempt loses that race."""
    ci = _cloud_init()
    catalog = ci[ci.index("/opt/catalog-once.sh") : ci.index("schemagate.service")]
    assert "for attempt in" in catalog, "cataloguing must retry"
    assert "sleep 60" in catalog, "retries must outlast IAM propagation"


def test_the_endpoint_starts_before_cataloguing_not_after():
    """Cataloguing now retries for up to twelve minutes. Running it inline in
    runcmd would hold the MCP server down for all of it."""
    run = _cloud_init()[_cloud_init().index("runcmd:") :]
    assert run.index("enable --now schemagate\n") < run.index("schemagate-catalog")


def test_the_stack_refuses_a_wildcard_cidr():
    """The MCP endpoint has no authentication of its own."""
    v = (STACK / "variables.tf").read_text()
    assert v.count('!= "0.0.0.0/0"') >= 2
