---
description: Cut a new release — bump, verify clean, push, and hand off the GitHub release
argument-hint: "<version> e.g. 0.1.9"
---

Cut release $1.

Do these in order and stop at the first failure.

**1. Bump the version in both places.** They must match or the wheel and the
package disagree:

- `src/schemagate/__init__.py` → `__version__`
- `pyproject.toml` → `version`

**2. Promote the CHANGELOG.** Change the `## Unreleased` heading to `## $1`.
If there is no Unreleased section, ask what this release contains rather than
inventing an entry.

**3. Run the suite in a clean venv, not this container.** This container is
missing extras and produces false failures that have wasted time before:

```bash
python3 -m venv /tmp/rel && /tmp/rel/bin/pip install -q -e ".[dev,mcp]" \
  && /tmp/rel/bin/python -m pytest -q
```

Everything must pass. Do not proceed on "those failures are unrelated" without
proving it in the clean venv.

**4. Commit and push to `main`.** Write the message about what changed and why,
in the style of the existing history — no attribution trailers of any kind;
this repo has had them stripped deliberately.

**5. Hand the release to the user.** Tag pushes are blocked in this
environment and have failed on every attempt across many sessions — do not
spend retries on it. Tell them:

> github.com/ashishsinha1602/schemagate/releases/new
> Choose a tag → type `v$1` → "Create new tag: v$1 on publish"
> Target `main`. Title and description. Publish release.

The release form creates the tag itself, so no separate tag push is needed.
`publish.yml` fires on `release: published`, not on a tag, so a bare tag would
publish nothing anyway.

**6. Verify after they publish**, and report each explicitly:

- both workflow jobs (`pypi`, `oci-stack`) green
- PyPI shows the new version (its JSON API is CDN-cached; re-check if stale)
- the release has a `schemagate-oci-stack.zip` asset with `main.tf` at the
  **zip root** — Resource Manager will not read it otherwise
