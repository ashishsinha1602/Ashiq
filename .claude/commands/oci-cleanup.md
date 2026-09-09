---
description: Find and destroy leftover schemagate resources in the tenancy
---

Clear resources left behind by an interrupted `verify.sh` run.

Always relevant when a run was killed, a Cloud Shell session dropped, or an
apply failed part-way. Always Free allows only two Autonomous Databases, so one
orphan blocks the next run for an unrelated-looking reason.

Ask the user to run this first:

```bash
oci resource-manager stack list --compartment-id "$OCI_TENANCY" --all \
  --query "data[?contains(\"display-name\",'schemagate-verify')].{name:\"display-name\",id:id}" --output table
oci compute instance list --compartment-id "$OCI_TENANCY" --all \
  --query "data[?contains(\"display-name\",'schemagate') && \"lifecycle-state\"!='TERMINATED'].{state:\"lifecycle-state\",id:id}" --output table
oci db autonomous-database list --compartment-id "$OCI_TENANCY" --all \
  --query "data[?contains(\"display-name\",'schemagate') && \"lifecycle-state\"!='TERMINATED'].{name:\"display-name\",id:id}" --output table
oci network public-ip list --compartment-id "$OCI_TENANCY" --scope REGION --all \
  --query "data[?contains(\"display-name\",'schemagate')].{ip:\"ip-address\",id:id}" --output table
```

Prefer destroying the **stack**, not individual resources — one destroy job
removes the instance, VCN, database, IAM and reserved IP together, in the right
order:

```bash
oci resource-manager job create-destroy-job --stack-id <STACK_OCID> \
  --execution-plan-strategy AUTO_APPROVED --query 'data.id' --raw-output
```

Only delete individually when the stack is already gone. Then, in this order —
the reserved IP is attached to the instance's VNIC and the policy references
the dynamic group:

```bash
oci db autonomous-database delete --autonomous-database-id <ID> --force
oci iam policy delete --policy-id <ID> --force
oci iam dynamic-group delete --dynamic-group-id <ID> --force
oci network public-ip delete --public-ip-id <ID> --force
```

Dynamic groups and policies are **tenancy-scoped**, not compartment-scoped, so
they do not appear in a compartment listing. A `TERMINATED` database needs no
action; a `TERMINATING` one still holds its Always Free slot until it finishes.
