// Runs the JS port over a set of cases and prints JSON for Python to compare.
// Usage: node parity_runner.js schemas.json cases.json
const fs = require("fs");
const ashiq = require("./ashiq.js");
const [schemasPath, casesPath] = process.argv.slice(2);
const schemas = JSON.parse(fs.readFileSync(schemasPath, "utf8"));
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));

const results = [];
const cats = {};
for (const c of cases) {
  const key = `${c.schema}|${c.hints ? 1 : 0}|${JSON.stringify(c.describe || {})}`;
  if (!cats[key]) {
    const spec = schemas[c.schema];
    const cat = new ashiq.Catalog(spec.docs);
    if (c.hints) for (const [t, h] of Object.entries(spec.hints)) cat.hint(t, h);
    for (const [t, r] of Object.entries(spec.restrict)) cat.restrict(t, r);
    if (c.describe) cat.describe(c.describe);
    cats[key] = cat;
  }
  const cat = cats[key];
  const who = c.principal ? ashiq.principal(c.principal, c.roles) : null;
  const sel = cat.select(c.question, { topK: c.top_k, principal: who, expandFks: c.expand_fks });
  results.push({
    id: c.id,
    names: sel.tableNames,
    reasons: sel.hits.map((h) => h.reason),
    scores: sel.hits.map((h) => Number(h.score.toFixed(10))),
    tokens: ashiq.estimateTokens(sel.promptFragment()),
    ddl_len: sel.promptFragment().length,
  });
}
// tokenizer and embedder spot checks
const tok = {};
for (const t of ["facturación_mensual", "año", "Müller_Straße", "売上明細", "한국어_테이블", "CustOrderLine", "FCT_TXN_LN_2024"]) tok[t] = ashiq.tokenize(t);
const emb = new ashiq.HashingEmbedder(64).embed(["customer order line items", "売上明細 金額"]).map((v) => v.map((x) => Number(x.toFixed(12))));
process.stdout.write(JSON.stringify({ results, tokenize: tok, embed: emb }));
