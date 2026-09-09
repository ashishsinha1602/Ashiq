---
description: Read the real boot state off a running stack instance over SSH
argument-hint: "<instance-ip>"
---

Diagnose why the MCP endpoint on a stack instance is not answering.

Reasoning from elapsed time produced four confident, well-argued, entirely
wrong diagnoses across one night. Thirty seconds of SSH produced the right one.
Always do this before proposing a fix.

Give the user this, with `$1` as the instance IP:

```bash
ssh -i ~/.schemagate-verify-key -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null opc@$1 '
  echo "== cloud-init"; sudo cloud-init status --long | head -6
  echo "== marker";     ls -l /opt/schemagate/.ready 2>&1
  echo "== units";      ls /etc/systemd/system/schemagate*.service 2>&1
  echo "== status";     sudo systemctl --no-pager --lines=5 \
                          status schemagate schemagate-resolve-db 2>&1 | head -30
  echo "== resolve";    sudo tail -10 /var/log/schemagate-resolve-db.log 2>&1
  echo "== boot";       sudo tail -20 /var/log/cloud-init-output.log
  echo "== oom";        sudo dmesg 2>/dev/null | grep -iE "out of memory|killed process" | tail -5
  echo "== resources";  free -m; uptime
'
```

The key is at `~/.schemagate-verify-key` in the user's home directory, not in
`/tmp` — Cloud Shell discards `/tmp` on reconnect and that lost the evidence
twice.

How to read it:

- **`cloud-init status: error`** → read the `== boot` tail. The failure is
  there, named. Do not guess past it.
- **units missing but `/etc/schemagate.env` present** → `write_files` aborted
  part-way. It stops at the first failing entry and silently discards every
  file after it. The known cause is an `owner:` naming a user cloud-init has
  not created yet.
- **`.ready` missing after 10+ minutes, cloud-init still running** → the pip
  install is the bottleneck. Retry on `SHAPE=VM.Standard.A1.Flex`.
- **`resolve-db` in `activating (start)` with a `systemctl` child** → a
  blocking `systemctl restart` of a unit ordered `After=` this one. Deadlock;
  it needs `--no-block`.
- **server `inactive (dead)` with resolve-db failed** → check whether
  `schemagate.service` uses `Requires=` (fatal) rather than `Wants=`.

Never report a cause you have not seen in this output.
