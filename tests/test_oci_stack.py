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
    assert run.index("start --no-block schemagate\n") < run.index("schemagate-catalog")


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


def test_one_way_tls_database_always_has_an_access_control_list():
    """0.1.7 set whitelisted_ips to null when adb_allowed_cidrs was empty, and
    every apply failed: "One-way TLS connections require a private endpoint or
    a public IP with an ACL". mTLS off and no list is not a legal combination,
    so the list must never be conditional on a variable the user can leave
    empty."""
    main = (STACK / "main.tf").read_text()
    adb = main[main.index('resource "oci_database_autonomous_database"') :]
    adb = adb[: adb.index("\n}")]
    assert "is_mtls_connection_required = false" in adb
    acl = [ln for ln in adb.splitlines() if "whitelisted_ips" in ln]
    assert len(acl) == 1, acl
    assert "null" not in acl[0], (
        "an empty access-control list with mTLS off is rejected at apply time"
    )
    assert "oci_core_public_ip" in acl[0], (
        "the list must name the instance's reserved public IP, or the instance "
        "cannot reach the database it was given"
    )


def test_the_database_does_not_feed_cloud_init():
    """The ACL names the instance's IP, so the database is created after the
    instance. Referencing the database from cloud-init would be a dependency
    cycle Terraform refuses to plan; the descriptor is resolved at boot."""
    main = (STACK / "main.tf").read_text()
    block = main[main.index("locals {") : main.index("templatefile(")]
    assert "oci_database_autonomous_database" not in block
    ci = _cloud_init()
    assert "/opt/resolve-db.sh" in ci
    assert "read autonomous-database-family" in main, (
        "the boot-time lookup needs the instance principal to read the database"
    )


def test_the_availability_domain_is_one_that_offers_the_shape():
    """A live Phoenix tenancy had VM.Standard.E2.1.Micro in AD-3 only. Taking
    availability_domains[0] asked AD-1 for a shape it does not have, and
    LaunchInstance returned 404-NotAuthorizedOrNotFound one second in."""
    main = (STACK / "main.tf").read_text()
    vm = main[main.index('resource "oci_core_instance"') :]
    vm = vm[: vm.index("\n}")]
    ad = [ln for ln in vm.splitlines() if ln.strip().startswith("availability_domain")]
    assert len(ad) == 1, ad
    assert "availability_domains[0]" not in ad[0], (
        "the first availability domain does not necessarily offer the shape"
    )
    assert "local.availability_domain" in ad[0]
    assert 'data "oci_core_shapes"' in main, (
        "choosing a domain requires asking each one what shapes it offers"
    )


def test_the_database_lookup_does_not_wait_for_the_install():
    """Oracle spends minutes provisioning the Autonomous Database. That clock
    runs whether or not anyone is watching it, so the wait belongs alongside
    the pip install, not after it. Letting schemagate.service pull resolve-db
    in as a dependency put it after -- a real apply spent the install time,
    then started the database wait from zero, and the endpoint missed a
    ten-minute window."""
    ci = _cloud_init()
    run = ci[ci.index("runcmd:") :]
    started = run.index("start --no-block schemagate-resolve-db")
    installed = run.index("/opt/pick-extras.sh")
    assert started < installed, (
        "start the database lookup before the install, not after it"
    )


def test_nothing_on_the_boot_path_installs_the_oci_cli():
    """The lookup needs two API calls. The SDK that makes them is already in
    the venv as schemagate[oci]; reaching for the CLI instead would add a
    repository and a package install to every first boot."""
    ci = _cloud_init()
    assert "dnf install" not in ci and "yum install" not in ci, (
        "the only packages a first boot installs are the ones cloud-init's "
        "`packages:` list declares, before runcmd"
    )
    assert "/opt/resolve-db.py" in ci, "the lookup is Python, against the SDK"


def test_the_install_marker_is_what_the_lookup_waits_for():
    """`import oci` starts succeeding part-way through the install, so waiting
    on it would race a half-unpacked venv."""
    ci = _cloud_init()
    extras = ci[ci.index("/opt/pick-extras.sh") : ci.index("- path: /opt/catalog-once.sh")]
    assert "touch /opt/schemagate/.ready" in extras
    resolve = ci[ci.index("- path: /opt/resolve-db.sh") : ci.index("schemagate-catalog.service")]
    assert "/opt/schemagate/.ready" in resolve


def test_the_install_does_not_byte_compile_on_a_one_ocpu_box():
    """Byte-compiling the OCI SDK's thousands of modules on VM.Standard.E2.1.Micro
    costs minutes of the first boot, and buys nothing -- Python compiles what it
    imports, and this venv imports a fraction of it. A live run spent eighteen
    minutes here and the endpoint never answered."""
    ci = _cloud_init()
    extras = ci[ci.index("/opt/pick-extras.sh") : ci.index("- path: /opt/catalog-once.sh")]
    assert "--no-compile" in extras


def test_the_oci_sdk_is_installed_only_when_something_needs_it():
    """It is the largest thing on the boot path. Cataloguing through OCI
    Generative AI needs it, and so does the boot-time database lookup; a stack
    pointed at your own database with neither does not."""
    ci = _cloud_init()
    extras = ci[ci.index("/opt/pick-extras.sh") : ci.index("- path: /opt/catalog-once.sh")]
    assert 'SCHEMAGATE_CREATE_ADB" = "true"' in extras
    assert "mcp,oci" in extras and '"$extras,mcp"' in extras


def test_tenancy_unique_names_are_unique_per_apply_not_per_compartment():
    """A dynamic group and a policy are tenancy-scoped, and an Autonomous
    Database name must be unique across the region. Deriving all three from
    md5(compartment_ocid) made them identical on every run, so one leftover --
    a destroy that did not finish -- failed every later apply with
    "DynamicResourceGroup with the same displayName already exists"."""
    main = (STACK / "main.tf").read_text()
    assert "md5(var.compartment_ocid)" not in main, (
        "a name keyed on the compartment is the same on every run in it"
    )
    assert "substr(md5(oci_core_vcn.vcn.id), 0, 8)" in main
    for unique in ('name           = "schemagate-mcp-dg-${local.suffix}"',
                   'name           = "schemagate-genai-policy-${local.suffix}"',
                   'db_name                     = "sg${local.suffix}"'):
        assert unique in main, unique


def test_the_boot_lookup_and_the_database_agree_on_the_display_name():
    """resolve-db finds the database by display name. A name shared with a
    leftover from an earlier run would resolve the wrong database."""
    main = (STACK / "main.tf").read_text()
    assert main.count("adb_display_name = \"schemagate-demo-${local.suffix}\"") == 1
    assert "display_name                = local.adb_display_name" in main
    assert "adb_display_name  = local.adb_display_name" in main
