/* schemagate selector, JavaScript port.
 *
 * A faithful port of schemagate.catalog / schemagate.embedder / schemagate.models so the
 * Studio can run selection in the browser with no server. Same tokeniser,
 * same BM25 constants, same blake2b-bucketed hashing embedder, same RRF
 * fusion, same shadow demotion, same FK expansion, same scoping.
 *
 * tests/test_js_parity.py runs this file under Node against the same
 * schemas and questions as the Python library and asserts the rankings
 * are identical. If you change the Python ranking, that test tells you
 * to change this too.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    let b;
    try { b = require("blakejs"); }            // npm install, if present
    catch (e) { b = require("./blake2b.browser.js"); }  // bundled copy: no install needed
    module.exports = factory(b);
  }
  else root.schemagate = factory(root.blakejs);
})(typeof self !== "undefined" ? self : this, function (blakejs) {
  "use strict";

  const RRF_K = 60;
  const SHADOW_PENALTY = 0.5;
  const STANDALONE_SHADOW = /_(?:bak|bkp|backup)(?:_?\d{4}_?\d{2}_?\d{2}|_\d{6,8})?$/i;
  const PARTITION = /_(?:p\d+|\d{4}(?:_?\d{2}){0,2}|y\d{4}(?:m\d{2})?(?:d\d{2})?)$/i;
  const SHADOW_SUFFIXES = ["_bkp", "_backup", "_bak", "_old", "_tmp", "_temp", "_new", "_copy",
    "_archive", "_arch", "_hist", "_stg", "_staging", "_v1", "_v2", "_v3",
    "_prev", "_orig"];
  const SHADOW_PREFIXES = ["stg_", "staging_", "tmp_", "temp_", "bkp_", "backup_", "old_",
    "copy_", "scratch_", "wip_"];
  const LAYER_PREFIXES = ["dim_", "fact_", "fct_", "f_", "d_", "v_", "vw_", "view_",
    "bridge_", "br_", "tbl_", "t_", "agg_", "mv_"];

  // ---------------- tokenize (schemagate.embedder.tokenize) ----------------
  const CJK = [[0x3040, 0x30FF], [0x3400, 0x4DBF], [0x4E00, 0x9FFF], [0xAC00, 0xD7AF], [0xF900, 0xFAFF]];
  const WORD = /[\p{L}\p{N}]+/gu;
  const isCjk = (ch) => { const p = ch.codePointAt(0); return CJK.some(([a, b]) => p >= a && p <= b); };
  const isAscii = (s) => /^[\x00-\x7f]*$/.test(s);
  const FOLD = [["ß", "ss"], ["ø", "o"], ["æ", "ae"], ["đ", "d"], ["ł", "l"]];
  function fold(tok) {
    if (isAscii(tok)) return tok;
    for (const [a, b] of FOLD) tok = tok.split(a).join(b);
    return tok.normalize("NFKD").replace(/\p{M}/gu, "");
  }
  // Ports of embedder.expand_joins and catalog._stem / _BOOST_STOP. The
  // Python side is the source of truth; tests/test_js_parity.py asserts the
  // two rank identically on the same fixtures, which is what caught this
  // file being left behind when the scoring changed.
  const BOOST_STOP = new Set("of by as at in on to for and or per the a an is are was".split(" "));
  function stemToken(t) {
    if (t.length > 4 && t.endsWith("ies")) return t.slice(0, -3) + "y";
    if (t.length > 4 && t.endsWith("es") && !t.endsWith("ses")) return t.slice(0, -2);
    if (t.length > 3 && t.endsWith("s") && !t.endsWith("ss")) return t.slice(0, -1);
    return t;
  }
  function expandJoins(tokens, vocab, maxLen = 14) {
    const out = tokens.slice();
    for (let i = 0; i + 1 < tokens.length; i++) {
      const a = tokens[i], b = tokens[i + 1];
      if (!/^[a-z]+$/.test(a) || !/^[a-z]+$/.test(b)) continue;
      if (a.length + b.length > maxLen) continue;
      const joined = a + b;
      if (!vocab || vocab.has(joined)) out.push(joined);
    }
    return out;
  }

  function tokenize(text) {
    text = text.replace(/(?<=[a-z0-9])(?=[A-Z])/g, " ");
    const out = [];
    for (const tok of (text.toLowerCase().match(WORD) || [])) {
      const chars = Array.from(tok);
      if (chars.some(isCjk)) {
        out.push(...chars);
        for (let i = 0; i < chars.length - 1; i++) out.push(chars[i] + chars[i + 1]);
        continue;
      }
      const f = fold(tok);
      if (f) out.push(f);
    }
    return out;
  }

  // ---------------- hashing embedder ----------------
  class HashingEmbedder {
    constructor(dim = 512, ngram = [3, 5], wordWeight = 2.0) {
      this.dim = dim; this.ngram = ngram; this.wordWeight = wordWeight;
      this.name = `hashing-${dim}-${ngram[0]}${ngram[1]}`;
    }
    _bucketAndSign(feature) {
      const h = blakejs.blake2b(new TextEncoder().encode(feature), null, 8);
      let n = 0n;
      for (let i = 0; i < 8; i++) n = (n << 8n) | BigInt(h[i]);
      const bucket = Number(n % BigInt(this.dim));
      const sign = ((n >> 63n) & 1n) === 1n ? 1.0 : -1.0;
      return [bucket, sign];
    }
    *_features(text) {
      const toks = tokenize(text);
      for (const t of toks) yield [`w:${t}`, this.wordWeight];
      const [lo, hi] = this.ngram;
      for (const t of toks) {
        const padded = Array.from(`^${t}$`);
        for (let n = lo; n <= hi; n++)
          for (let i = 0; i + n <= padded.length; i++) yield [`c:${padded.slice(i, i + n).join("")}`, 1.0];
      }
    }
    embed(texts) {
      return texts.map((text) => {
        const vec = new Array(this.dim).fill(0.0);
        const counts = new Map();
        for (const [feat, w] of this._features(text || "")) counts.set(feat, (counts.get(feat) || 0.0) + w);
        for (const [feat, c] of counts) {
          const [b, s] = this._bucketAndSign(feat);
          vec[b] += (1.0 + Math.log(c)) * s;
        }
        let n = 0.0; for (const v of vec) n += v * v; n = Math.sqrt(n);
        return n ? vec.map((v) => v / n) : vec;
      });
    }
  }
  function cosineDistance(a, b) { let s = 0.0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return 1.0 - s; }

  // ---------------- models ----------------
  const KW = new Set(["select", "from", "where", "join", "left", "right", "inner", "outer",
    "on", "group", "by", "order", "as", "and", "or", "not", "null", "case",
    "when", "then", "else", "end", "sum", "count", "avg", "min", "max",
    "distinct", "union", "all", "having", "with", "create", "view", "is",
    "substr", "cast", "coalesce", "asc", "desc", "limit", "cross", "full",
    "exists", "between", "like", "over", "partition", "rows", "range",
    "fetch", "first", "only", "top", "offset", "in", "any", "some",
    "nvl", "nvl2", "decode", "trunc", "sysdate", "systimestamp", "rownum",
    "rowid", "dual", "to_char", "to_date", "to_number", "add_months",
    "months_between", "listagg", "instr", "lpad", "rpad", "regexp_like",
    "regexp_substr", "nullif", "greatest", "least", "round", "floor",
    "ceil", "abs", "mod", "power", "sqrt", "upper", "lower", "initcap",
    "length", "replace", "concat", "date_trunc", "extract", "now",
    "current_date", "current_timestamp", "getdate", "dateadd", "datediff",
    "datepart", "isnull", "ifnull", "convert", "len", "charindex",
    "julianday", "strftime", "lag", "lead", "row_number", "rank",
    "dense_rank", "ntile", "first_value", "last_value", "string_agg",
    "group_concat", "array_agg", "unnest", "json_value", "json_query"]);
  function identifiers(sql, limit = 1200) {
    const seen = new Set(), out = [];
    for (const tok of (sql.match(/[A-Za-z_][A-Za-z0-9_]*/g) || [])) {
      const t = tok.toLowerCase();
      if (KW.has(t) || t.length < 3 || seen.has(t)) continue;
      seen.add(t); out.push(tok.split("_").join(" ")); out.push(tok);
    }
    return out.join(" ").slice(0, limit);
  }
  function qname(d) { return d.schema ? `${d.schema}.${d.name}` : d.name; }
  function embedText(d) {
    const parts = [d.name.split("_").join(" "), d.name];
    if (d.hint) parts.push(d.hint);
    if (d.description) parts.push(d.description);
    for (const c of d.columns) parts.push(c.name.split("_").join(" "));
    for (const c of d.columns) if (c.comment) parts.push(c.comment);
    if (d.definition) parts.push(identifiers(d.definition));
    return parts.filter(Boolean).join(" \n");
  }
  const oneLine = (t) => (t ? String(t).split(/\s+/).filter(Boolean).join(" ") : t);
  function renderColumn(c) {
    const bits = [c.name, c.type];
    if (c.pk) bits.push("PK");
    if (c.nullable === false) bits.push("NOT NULL");
    const s = bits.join(" ");
    const comment = oneLine(c.comment);
    return comment ? `${s}  -- ${comment}` : s;
  }
  function renderDdl(d, maxColumns = 40) {
    const note = oneLine(d.hint || d.description);
    const lines = [note ? `-- ${note}` : "", `${d.kind} ${qname(d)} (`];
    const cols = d.columns.slice(0, maxColumns);
    for (const c of cols) lines.push(`  ${renderColumn(c)},`);
    if (d.columns.length > maxColumns) lines.push(`  -- ...${d.columns.length - maxColumns} more columns`);
    if (lines[lines.length - 1].endsWith(",")) lines[lines.length - 1] = lines[lines.length - 1].slice(0, -1);
    lines.push(")");
    for (const fk of (d.foreign_keys || [])) lines.push(`-- FK ${d.name}(${fk.columns.join(",")}) -> ${fk.ref_table}`);
    return lines.filter(Boolean).join("\n");
  }

  // ---------------- BM25 ----------------
  class BM25 {
    constructor(docs, k1 = 1.4, b = 0.72) {
      this.k1 = k1; this.b = b;
      this.docs = docs.map(tokenize);
      this.len = this.docs.map((d) => d.length);
      this.avg = this.len.length ? this.len.reduce((a, x) => a + x, 0) / this.len.length : 0.0;
      this.tf = this.docs.map((d) => { const m = new Map(); for (const t of d) m.set(t, (m.get(t) || 0) + 1); return m; });
      const df = new Map();
      for (const d of this.docs) for (const t of new Set(d)) df.set(t, (df.get(t) || 0) + 1);
      const n = this.docs.length;
      this.idf = new Map(); for (const [t, c] of df) this.idf.set(t, Math.log(1 + (n - c + 0.5) / (c + 0.5)));
    }
    scores(query) {
      const q = expandJoins(tokenize(query), this.idf);
      return this.tf.map((tf, i) => {
        let s = 0.0;
        for (const t of q) {
          const f = tf.get(t) || 0; if (!f) continue;
          const denom = f + this.k1 * (1 - this.b + this.b * this.len[i] / (this.avg || 1));
          s += (this.idf.get(t) || 0.0) * f * (this.k1 + 1) / denom;
        }
        return s;
      });
    }
  }

  // ---------------- principal ----------------
  function principal(subject, roles) {
    if (!subject) return null;
    if (!/^[a-z0-9_-]+:[^\s]+$/i.test(subject)) throw new Error(`subject ${JSON.stringify(subject)} must be namespaced as '<source>:<id>'`);
    return { subject, roles: new Set(roles || []) };
  }

  // ---------------- catalog ----------------
  class Catalog {
    constructor(docs, opts = {}) {
      this.embedder = opts.embedder || new HashingEmbedder();
      this.docs = new Map();
      for (const d of docs) this.docs.set(qname(d), Object.assign({ columns: [], foreign_keys: [], roles: null }, d));
      this.shadowSuffixes = opts.shadowSuffixes || SHADOW_SUFFIXES;
      this.shadowPrefixes = opts.shadowPrefixes || SHADOW_PREFIXES;
      this.stale = true;
    }
    hint(table, text) { for (const [q, d] of this.docs) if (q === table || d.name === table) { d.hint = text; this.stale = true; return; } throw new Error(`${table} not in catalog`); }
    restrict(table, roles) { for (const [q, d] of this.docs) if (q === table || d.name === table) { d.roles = [...roles]; return; } throw new Error(`${table} not in catalog`); }
    describe(map) { for (const [q, d] of this.docs) if (map[q] || map[d.name]) { d.description = map[q] || map[d.name]; this.stale = true; } }
    index() {
      this.order = [...this.docs.keys()];
      const texts = this.order.map((q) => embedText(this.docs.get(q)));
      this.bm25 = new BM25(texts);
      this.shadows = this._findShadows();
      this.vecs = new Map(); const vs = this.embedder.embed(texts);
      this.order.forEach((q, i) => this.vecs.set(q, vs[i]));
      this.stale = false; return this;
    }
    _findShadows() {
      const stemsOf = (n) => { const out = [n]; for (const p of LAYER_PREFIXES) if (n.startsWith(p) && n.length > p.length) { out.push(n.slice(p.length)); break; } const i = n.indexOf("_"); if (i > 0) { const tail = n.slice(i + 1); if (tail.length >= 4 && !out.includes(tail)) out.push(tail); } return out; };
      const bySchema = new Map(), stems = new Map();
      for (const [q, d] of this.docs) {
        const name = d.name.toLowerCase(), sch = d.schema || null;
        if (!bySchema.has(sch)) bySchema.set(sch, new Map()); bySchema.get(sch).set(name, q);
        if (!this.shadowPrefixes.some((p) => name.startsWith(p))) {
          if (!stems.has(sch)) stems.set(sch, new Map());
          for (const st of stemsOf(name)) if (!stems.get(sch).has(st)) stems.get(sch).set(st, q);
        }
      }
      const shadows = new Map();
      for (const [q, d] of this.docs) {
        const name = d.name.toLowerCase(), sch = d.schema || null;
        for (const suf of this.shadowSuffixes) if (name.endsWith(suf) && name.length > suf.length) {
          const base = bySchema.get(sch).get(name.slice(0, -suf.length));
          if (base && base !== q) { shadows.set(q, base); break; }
        }
        if (shadows.has(q)) continue;
        for (const pre of this.shadowPrefixes) if (name.startsWith(pre) && name.length > pre.length) {
          const base = (stems.get(sch) || new Map()).get(name.slice(pre.length));
          if (base && base !== q) { shadows.set(q, base); break; }
        }
      }
      // A backup is a backup with or without its original still present, and
      // a partition is a copy of its parent. Same rules as catalog.py.
      if (this.shadowSuffixes.length) {
        for (const [q, d] of this.docs) {
          if (shadows.has(q)) continue;
          const name = (d.name || "").toLowerCase(), sch = d.schema || null;
          const m = STANDALONE_SHADOW.exec(name);
          if (m) { shadows.set(q, name.slice(0, m.index) || name); continue; }
          const p = PARTITION.exec(name);
          if (p) {
            const parent = (bySchema.get(sch) || new Map()).get(name.slice(0, p.index));
            if (parent && parent !== q && !shadows.has(parent)) shadows.set(q, parent);
          }
        }
      }
      return shadows;
    }
    _visible(d, who) { if (!d.roles || !d.roles.length) return true; if (!who) return false; return d.roles.some((r) => who.roles.has(r)); }
    select(question, opts = {}) {
      const topK = opts.topK ?? 6, who = opts.principal ?? null, expandFks = opts.expandFks ?? true;
      const vw = opts.vectorWeight ?? 1.0, lw = opts.lexicalWeight ?? 1.0, pin = opts.pin || [];
      if (this.stale || !this.order) this.index();
      const allowed = this.order.filter((q) => this._visible(this.docs.get(q), who));
      const allowedSet = new Set(allowed);
      const vocab = this.bm25 ? this.bm25.idf : null;
      const baseToks = tokenize(question);
      const joins = expandJoins(baseToks, vocab).filter((t) => !baseToks.includes(t));
      const qvec = this.embedder.embed([question + (joins.length ? " " + joins.join(" ") : "")])[0];
      const hits = this.order.map((q) => ({ q, d: cosineDistance(qvec, this.vecs.get(q)) })).filter((h) => h.d <= 2.0);
      hits.sort((a, b) => a.d - b.d);   // stable, like Python's sort
      const vecRank = new Map(); hits.filter((h) => allowedSet.has(h.q)).forEach((h, i) => vecRank.set(h.q, i));
      const bm = this.bm25.scores(question);
      const lexPairs = this.order.map((q, i) => [q, bm[i]]).filter(([q, s]) => allowedSet.has(q) && s > 0);
      lexPairs.sort((a, b) => b[1] - a[1]);
      const lexRank = new Map(); lexPairs.forEach(([q], i) => lexRank.set(q, i));
      const qTokens = expandJoins(tokenize(question), vocab);
      const qStems = new Set(qTokens.filter((t) => t.length > 2 && !BOOST_STOP.has(t)).map(stemToken));
      let maxIdf = 0.0;
      if (this.bm25) for (const v of this.bm25.idf.values()) if (v > maxIdf) maxIdf = v;
      const namedIn = (d) => { const nm = tokenize(d.name); if (!nm.length || nm.length > qTokens.length) return false; for (let i = 0; i + nm.length <= qTokens.length; i++) { let ok = true; for (let j = 0; j < nm.length; j++) if (qTokens[i + j] !== nm[j]) { ok = false; break; } if (ok) return true; } return false; };
      const fused = [];
      for (const q of allowed) {
        let s = 0.0;
        if (vecRank.has(q)) s += vw / (RRF_K + vecRank.get(q) + 1);
        if (lexRank.has(q)) s += lw / (RRF_K + lexRank.get(q) + 1);
        if (s && this.shadows.has(q)
            && (allowedSet.has(this.shadows.get(q)) || !this.docs.has(this.shadows.get(q)))
            && !namedIn(this.docs.get(q))) s *= SHADOW_PENALTY;
        if (s && this.bm25 && maxIdf > 0) {
          const nameToks = new Set(tokenize(this.docs.get(q).name || "")
            .filter((t) => t.length > 2 && !BOOST_STOP.has(t)).map(stemToken));
          let hit = 0.0;
          for (const t of qStems) if (nameToks.has(t)) hit += this.bm25.idf.get(t) || 0.0;
          if (hit) {
            s *= 1.0 + 0.5 * Math.min(1.0, hit / maxIdf);
            if (qStems.has(stemToken((this.docs.get(q).name || "").toLowerCase()))) s *= 1.2;
          }
        }
        if (s) fused.push([q, s]);
      }
      fused.sort((a, b) => b[1] - a[1]);
      const chosen = [], taken = new Set();
      for (const name of pin) for (const [q, d] of this.docs) if ((q === name || d.name === name) && allowedSet.has(q) && !taken.has(q)) { chosen.push({ doc: d, score: 1.0, reason: "pinned" }); taken.add(q); }
      for (const [q, s] of fused) { if (chosen.length >= topK) break; if (!taken.has(q)) { const reason = vecRank.has(q) && lexRank.has(q) ? "hybrid" : vecRank.has(q) ? "vector" : "lexical"; chosen.push({ doc: this.docs.get(q), score: s, reason }); taken.add(q); } }
      if (expandFks) for (const sc of [...chosen]) for (const fk of (sc.doc.foreign_keys || [])) for (const q of allowed) { const d = this.docs.get(q); if (d.name.toLowerCase() === fk.ref_table.toLowerCase() && !taken.has(q)) { chosen.push({ doc: d, score: 0.0, reason: "fk" }); taken.add(q); } }
      return {
        question, hits: chosen, totalObjects: this.order.length,
        tableNames: chosen.map((h) => qname(h.doc)),
        promptFragment: (maxColumns = 40) => chosen.map((h) => renderDdl(h.doc, maxColumns)).join("\n\n"),
        explain: () => chosen.map((h) => `${h.score.toFixed(3).padStart(6)}  ${h.reason.padEnd(8)}  ${qname(h.doc)}`).join("\n"),
      };
    }
    fullSchema(maxColumns = 40) { return [...this.docs.values()].map((d) => renderDdl(d, maxColumns)).join("\n\n"); }
  }

  // ---------------- token estimate (tests/bench.py) ----------------
  // Python's round() is round-half-to-even; Math.round is half-up. Match Python.
  function roundHalfEven(x) { const f = Math.floor(x), d = x - f; if (d < 0.5) return f; if (d > 0.5) return f + 1; return f % 2 === 0 ? f : f + 1; }
  function estimateTokens(text) {
    let n = 0;
    for (const piece of (text.match(/[A-Za-z]+|[0-9]|[^\sA-Za-z0-9]/g) || [])) n += /^[A-Za-z]+$/.test(piece) ? Math.max(1, roundHalfEven(piece.length / 4)) : 1;
    return n;
  }

  return { Catalog, HashingEmbedder, BM25, tokenize, principal, renderDdl, embedText, estimateTokens, qname, cosineDistance };
});
