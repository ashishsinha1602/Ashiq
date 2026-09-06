# Publishing from a phone (no laptop needed)

## 0. Before anything
Get the employment clearance email sent and answered. Everything below is
irreversible once it hits PyPI — names cannot be reused, and deleted releases
stay deleted.

Also decide the name. Check https://pypi.org/project/ashiq/ first; if it is
taken, pick another and change it in `pyproject.toml` AND the directory
`src/ashiq/`.

## 1. Create the repo (mobile browser)
github.com → **+** → New repository → name it → Public → Create.
Do NOT add a README; the upload in step 2 provides one.

## 2. Get the files in
Open `github.com/<you>/<repo>` and add `.dev` to the URL:
`github.dev/<you>/<repo>` — this is a full editor in the browser, works on
mobile in desktop mode. Create each file and paste its contents, then commit.

Faster alternative: repo → **Code** → **Codespaces** → *Create codespace*.
That gives you a real terminal in the browser. Then:

```bash
# paste the tarball via the Codespace file upload, or recreate files, then:
pip install -e ".[dev]"
pytest -q                  # expect 300+ passed (skips need DB URLs / node)
python tests/bench.py      # expect recall 100% on both schemas, -75.6% tokens
python scripts/certify_dialect.py <your-db-url>   # certify a dialect
git add -A && git commit -m "ashiq 0.1.0" && git push
```

## 2b. Turn on the name guard
`scripts/check_names.py` fails CI if a restricted identifier ever lands in the
tree. The term list is NOT in the repo (publishing it would broadcast the very
names you are keeping out). Two places to put it:

* locally: create `.namecheck`, one term per line — it is gitignored
* in CI: repo Settings → Secrets and variables → Actions → New repository
  secret → name `NAMECHECK_TERMS`, value = the same terms, one per line

With no terms configured the check passes silently, so CI still works for
outside contributors.

## 3. Placeholders — already done
`pyproject.toml` and `LICENSE` are filled in (Ashish Sinha,
github.com/ashishsinha1602/ashiq). Nothing to edit unless you rename the
project or want an author email in the metadata (optional; it becomes public
on PyPI, so leaving it out is reasonable).

## 4. Wire up PyPI trusted publishing (no tokens anywhere)
1. pypi.org → account → **Publishing** → *Add a pending publisher*
2. PyPI project name: `ashiq`
3. Owner: `<your github user>`  ·  Repository: `<repo>`
4. Workflow filename: `publish.yml`  ·  Environment: `pypi`
5. On GitHub: Settings → Environments → New environment → `pypi`

## 4b. Public demo page (GitHub Pages)
Settings → Pages → Source: **GitHub Actions**. The `pages` workflow then
publishes the Studio at https://ashishsinha1602.github.io/ashiq/ on every
push to main. No files to add; it builds from `src/ashiq/studio.html`.

## 5. Ship
GitHub → Releases → *Draft a new release* → tag `v0.1.0` → Publish.
The `publish` workflow builds and uploads. `pip install ashiq` works
within a minute or two.

## 6. Version bumps
Edit `version` in `pyproject.toml`, commit, cut a new release. PyPI refuses
re-uploads of an existing version, so every release needs a new number.
