"""Assemble the GitHub Pages site into ./site.

    pip install markdown && python scripts/build_site.py

Pages:
  /                      the Studio, with real <head> metadata (title, description,
                         Open Graph, Twitter card, canonical, JSON-LD)
  /vanna-alternative/    docs/migrating-from-vanna.md rendered as a page
  /cost/                 the token-cost table and a small calculator
  /robots.txt, /sitemap.xml, /llms.txt, /social-preview.png

Nothing here is a tracker, a font CDN or a third-party script.
"""
from __future__ import annotations

import datetime as dt
import html
import pathlib
import re
import shutil

import markdown

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
BASE = "https://ashishsinha1602.github.io/schemagate"
REPO = "https://github.com/ashishsinha1602/schemagate"
TITLE = "schemagate — identity-scoped schema selection for text-to-SQL"
DESC = ("Shows the model only the tables this caller may read, before any SQL exists, "
        "and cuts prompt tokens 65–97%. Any SQLAlchemy database. pip install schemagate.")

CSS = """
:root{--ink:#161A22;--muted:#5E6779;--line:#DCE1E9;--accent:#0F7B6C;--bg:#F5F7FA;--panel:#fff}
@media(prefers-color-scheme:dark){:root{--ink:#E6E9EF;--muted:#9AA3B5;--line:#2A303C;--accent:#3FBFA9;--bg:#0F1218;--panel:#171B23}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 20px 64px}nav{font-size:14px;color:var(--muted);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}
a{color:var(--accent)}h1{font-size:30px;line-height:1.2;margin:0 0 12px}h2{font-size:22px;margin:32px 0 8px}
pre{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px;overflow:auto;font-size:13px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}th{color:var(--muted);font-weight:500}
.cta{display:inline-block;background:var(--accent);color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:500;margin:8px 8px 8px 0}
input{font:inherit;padding:4px 8px;width:7em;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--ink)}
.muted{color:var(--muted)}
"""

NAV = ('<nav><a href="/schemagate/">Demo</a><a href="/schemagate/cost/">Cost</a>'
       '<a href="/schemagate/vanna-alternative/">Coming from Vanna</a>'
       f'<a href="{REPO}">GitHub</a><a href="https://pypi.org/project/schemagate/">PyPI</a></nav>')


def head(title: str, desc: str, path: str, extra: str = "") -> str:
    url = f"{BASE}{path}"
    ld = {
        "@context": "https://schema.org", "@type": "SoftwareApplication",
        "name": "schemagate", "applicationCategory": "DeveloperApplication",
        "operatingSystem": "Any", "url": BASE, "downloadUrl": "https://pypi.org/project/schemagate/",
        "codeRepository": REPO, "license": "https://www.apache.org/licenses/LICENSE-2.0",
        "author": {"@type": "Person", "name": "Ashish Sinha"}, "description": DESC,
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
    }
    import json
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<meta name="description" content="{html.escape(desc)}">'
        f'<link rel="canonical" href="{url}">'
        '<meta property="og:type" content="website">'
        f'<meta property="og:title" content="{html.escape(title)}">'
        f'<meta property="og:description" content="{html.escape(desc)}">'
        f'<meta property="og:url" content="{url}">'
        f'<meta property="og:image" content="{BASE}/social-preview.png">'
        '<meta property="og:image:width" content="1280"><meta property="og:image:height" content="640">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{html.escape(title)}">'
        f'<meta name="twitter:description" content="{html.escape(desc)}">'
        f'<meta name="twitter:image" content="{BASE}/social-preview.png">'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f"{extra}</head>"
    )


def page(title: str, desc: str, path: str, body: str) -> str:
    return (head(title, desc, path, f"<style>{CSS}</style>")
            + f"<body><main>{NAV}{body}</main></body></html>")


def build_index() -> None:
    studio = (ROOT / "src" / "schemagate" / "studio.html").read_text("utf-8")
    studio = re.sub(r"<title>.*?</title>\s*", "", studio, count=1)   # head() sets it
    # a crawlable summary above the app, invisible in the app's own layout
    intro = ('<div style="position:absolute;left:-9999px;top:auto;width:1px;height:1px;overflow:hidden">'
             '<h1>schemagate: identity-scoped schema selection for text-to-SQL</h1>'
             f'<p>{html.escape(DESC)}</p></div>')
    html_ = (head(TITLE, DESC, "/", "<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>")
             + "<body>" + intro + studio + "</body></html>")
    (SITE / "index.html").write_text(html_, "utf-8")


def build_vanna() -> None:
    md = (ROOT / "docs" / "migrating-from-vanna.md").read_text("utf-8")
    body = markdown.markdown(md, extensions=["fenced_code", "tables"])
    body = body.replace("<h1>", "<h1>", 1)
    body += (f'<p><a class="cta" href="{REPO}">GitHub</a>'
             '<a class="cta" href="/schemagate/">Try the demo</a></p>')
    (SITE / "vanna-alternative").mkdir(parents=True, exist_ok=True)
    (SITE / "vanna-alternative" / "index.html").write_text(page(
        "Vanna alternative for schema selection with access control — schemagate",
        "Vanna was archived in March 2026 and applied identity at execution, after the model saw "
        "the whole schema. schemagate applies it at schema selection. Migration notes.",
        "/vanna-alternative/", body), "utf-8")


def build_cost() -> None:
    rows = [("Commerce", 42, 2483, 604), ("Clinical claims", 27, 1568, 543),
            ("Claims warehouse (star)", 51, 3312, 880), ("Bank ledger and trading", 39, 2255, 637),
            ("IoT telemetry", 40, 2125, 448), ("Hostile (4 schemas, copies of everything)", 260, 16095, 444)]
    table = "".join(f"<tr><td>{n}</td><td>{o}</td><td>{f:,}</td><td>{s:,}</td><td><b>{(1-s/f)*100:.0f}%</b></td></tr>"
                    for n, o, f, s in rows)
    body = f"""
<h1>What text-to-SQL prompts cost, and what schema selection saves</h1>
<p class="muted">Measured on schemagate's six test schemas with its built-in token estimator,
averaged over each schema's golden questions. Reproduce with <code>python tests/bench.py</code>.</p>
<table><tr><th>schema</th><th>objects</th><th>full schema, every call</th><th>schemagate, average</th><th>reduction</th></tr>{table}</table>
<p>The selection stays at about six tables however large the schema is, so the saving grows with the
database. Real databases look like the last row.</p>
<h2>Your numbers</h2>
<p>Tokens per question: full schema <input id="tf" type="number" value="16095"> → selected <input id="ts" type="number" value="444">.
Questions per day <input id="q" type="number" value="5000">. Input price $<input id="p" type="number" step="0.05" value="3.00"> per million tokens.</p>
<p id="out" style="font-size:20px"></p>
<p class="muted">This multiplies four numbers you typed; it knows nothing about your provider's actual pricing. The
<a href="/schemagate/">demo</a> fills the token counts from a live question.</p>
<h2>Two things that cost nothing</h2>
<p>The selector never calls a model — BM25 plus a hashed embedder, offline, milliseconds. And the optional
one-sentence table descriptions can be written by any chat window you already have instead of an API key:
<code>schemagate describe</code> prints the prompt and takes the JSON reply.</p>
<p><a class="cta" href="https://pypi.org/project/schemagate/">pip install schemagate</a><a class="cta" href="{REPO}">GitHub</a></p>
<script>
function m(x){{return x>=1000?"$"+Math.round(x).toLocaleString():"$"+x.toFixed(2)}}
function r(){{const g=i=>Math.max(0,+document.getElementById(i).value||0);const per=t=>t*g("q")*30/1e6*g("p");
const f=per(g("tf")),s=per(g("ts"));document.getElementById("out").innerHTML=
"<s style='color:var(--muted)'>"+m(f)+"</s> → <b style='color:var(--accent)'>"+m(s)+"</b> per month on schema tokens; <b>"+m(Math.max(0,f-s))+"</b> saved"}}
for(const i of ["tf","ts","q","p"])document.getElementById(i).oninput=r;r();
</script>"""
    (SITE / "cost").mkdir(parents=True, exist_ok=True)
    (SITE / "cost" / "index.html").write_text(page(
        "Text-to-SQL prompt token cost calculator — schemagate",
        "Full-schema prompts vs per-question table selection: measured token counts on six schemas "
        "(65–97% fewer tokens) and a calculator for your own volume and price.",
        "/cost/", body), "utf-8")


def build_misc() -> None:
    shutil.copy(ROOT / "docs" / "media" / "social-preview.png", SITE / "social-preview.png")
    (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n")
    (SITE / "google7c61fe50e3637040.html").write_text(
        "google-site-verification: google7c61fe50e3637040.html\n")
    today = dt.date.today().isoformat()
    urls = "".join(f"<url><loc>{BASE}{p}</loc><lastmod>{today}</lastmod></url>"
                   for p in ["/", "/cost/", "/vanna-alternative/"])
    (SITE / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>')
    (SITE / "llms.txt").write_text(f"""# schemagate

> {DESC}

schemagate is an open-source Python library (Apache-2.0, by Ashish Sinha) for text-to-SQL and NL2SQL
systems. Given a question and a caller identity, it returns the handful of tables and views the model
needs, with every object the caller may not read removed before ranking, plus the DDL fragment for the
prompt. It reflects any SQLAlchemy database (Oracle, PostgreSQL, SQL Server, MySQL, SQLite), needs no
API key, ships an MCP server, a LangChain retriever, a CLI and a browser Studio, and has a native
Oracle 23ai VECTOR store.

- Install: pip install schemagate
- Repository: {REPO}
- PyPI: https://pypi.org/project/schemagate/
- Demo (runs in the browser, no database): {BASE}/
- Token cost table and calculator: {BASE}/cost/
- Migrating from Vanna: {BASE}/vanna-alternative/
- What was tested and what broke: {REPO}/blob/main/TESTING.md
""")


def main() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    build_index(); build_vanna(); build_cost(); build_misc()
    for p in sorted(SITE.rglob("*")):
        if p.is_file():
            print(f"{p.stat().st_size:9,d}  {p.relative_to(SITE)}")


if __name__ == "__main__":
    main()
