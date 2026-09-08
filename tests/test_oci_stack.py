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


def test_one_route_table_never_mixes_an_internet_and_a_service_gateway():
    """A live apply returned 400-InvalidParameter: "Internet Gateway target
    cannot be used together with Service Gateway target for All Services in
    the same routing table". The instance needs the internet gateway to
    install anything, so the service gateway is the one that cannot be there.
    The plan does not catch this -- only the API does."""
    main = (STACK / "main.tf").read_text()
    rt = main[main.index('resource "oci_core_route_table"') :]
    rt = rt[: rt.index("\nresource ")]
    assert "internet_gateway" in rt
    assert "service_gateway" not in rt, (
        "a service gateway route alongside the internet gateway default route "
        "is rejected by the OCI API at apply time"
    )
    assert 'resource "oci_core_service_gateway"' not in main


def test_console_form_and_terraform_declare_the_same_variables():
    """schema.yaml drives the Resource Manager form. A variable in one and not
    the other is either an input nobody can set or a form field that fails."""
    import re

    import yaml

    schema = yaml.safe_load((STACK / "schema.yaml").read_text())
    tf = set(re.findall(r'^variable "(\w+)"', (STACK / "variables.tf").read_text(), re.M))
    assert set(schema["variables"]) == tf


def test_the_stack_refuses_a_wildcard_cidr():
    """The MCP endpoint has no authentication of its own."""
    v = (STACK / "variables.tf").read_text()
    assert v.count('!= "0.0.0.0/0"') >= 2
