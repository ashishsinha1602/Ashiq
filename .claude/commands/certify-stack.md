---
description: Run the OCI stack certification end to end and report the result
argument-hint: "[shape] — optional, defaults to VM.Standard.A1.Flex"
---

Certify the OCI Resource Manager stack in the user's tenancy.

**You cannot run this yourself.** This container has no OCI CLI, no
credentials, and no network route to the tenancy. `verify.sh` runs in the
user's OCI Cloud Shell. Your job is to hand them the exact command, then read
what comes back and act on it.

Give them this, as one block, and nothing else until they paste output:

```bash
rm -rf ~/sg && git clone -q https://github.com/ashishsinha1602/schemagate.git ~/sg
nohup env SHAPE=${1:-VM.Standard.A1.Flex} \
  bash ~/sg/oci/stack/verify.sh > ~/verify.log 2>&1 &
sleep 5; tail -f ~/verify.log
```

Notes that matter, learned from nine real applies:

- No `ZIP_URL` means it pulls the **released** stack zip — the same artifact
  the Deploy to Oracle Cloud button hands out. That is the run that counts.
  Only pass `ZIP_URL=file://$HOME/sg-main.zip` when testing an unreleased
  change, and say so explicitly when you do.
- `nohup` is not optional. Cloud Shell drops idle connections and has killed
  two runs mid-flight. `~/verify.log` survives; `tail -f ~/verify.log`
  re-attaches.
- Step 4 (apply) takes ~10 minutes. Step 5 waits up to 15 more. Do not treat
  anything under 900s in step 5 as a failure, and say so if the user gets
  impatient — killing it costs another 25 minutes.
- `6/6 CERTIFIED` is the pass. In step 5, an HTTP **4xx** is a pass, not a
  failure: a bare GET to an MCP endpoint is a malformed MCP request, and the
  server answering at all is the thing being tested.

If step 5 stalls past ~240s, do not theorise from timings. Get onto the box —
that mistake cost a full day once. Run `/diagnose-boot` instead.

When it passes, check whether `oci/stack/README.md` still carries a caveat
that is no longer true, and update it if so.
