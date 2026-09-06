import re, sqlite3, sys, tempfile, os
sys.path.insert(0, os.path.dirname(__file__))
from schema_fixture import DDL, HINTS, GOLDEN, GOLDEN_PARAPHRASE
import schema_fixture_health as health
import schema_fixture_warehouse as warehouse
import schema_fixture_finance as finance
import schema_fixture_telemetry as telemetry
from ashiq import Catalog

# ---- token counting -------------------------------------------------------
# Exact cl100k counts when tiktoken is installed AND its BPE file is
# reachable; otherwise a deterministic offline estimate that splits
# identifiers the way a BPE tokenizer does (snake_case and camelCase
# boundaries, digits and punctuation separately). The estimate is what CI
# reports, so the published numbers are reproducible with zero network.
_SPLIT = re.compile(r"[A-Za-z]+|[0-9]|[^\sA-Za-z0-9]")


def _estimate(text: str) -> int:
    n = 0
    for piece in _SPLIT.findall(text):
        if piece.isalpha():
            # BPE keeps short words whole and splits longer ones ~4 chars
            n += max(1, round(len(piece) / 4))
        else:
            n += 1
    return n


def _make_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda t: len(enc.encode(t))), "tiktoken cl100k_base (exact)"
    except Exception:
        return _estimate, "built-in estimator (offline, deterministic)"


count_tokens, TOKEN_METHOD = _make_counter()

def build(hints=True, ddl=DDL, hint_map=HINTS, name="default"):
    p = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(p); c.executescript(ddl); c.commit(); c.close()
    cat = Catalog(name=name).bootstrap(f"sqlite:///{p}")
    if hints:
        for t, h in hint_map.items():
            cat.hint(t, h)
        cat.index()
    return cat

def recall(cat, vw, lw, k=6, expand=True, qs=None):
    hits = tot = 0
    misses = []
    for q, gold in (qs or GOLDEN):
        sel = cat.select(q, top_k=k, vector_weight=vw, lexical_weight=lw,
                         expand_fks=expand)
        got = {d.name for d in sel.objects}
        found = len(gold & got)
        hits += found; tot += len(gold)
        if found < len(gold):
            misses.append((q, sorted(gold - got)))
    return hits / tot, misses

cat = build()
print(f"{'mode':<12}{'recall@6':>10}")
for label, vw, lw in [("vector only", 1.0, 0.0), ("lexical only", 0.0, 1.0),
                      ("hybrid", 1.0, 1.0)]:
    r, m = recall(cat, vw, lw)
    print(f"{label:<12}{r:>9.1%}")

r, misses = recall(cat, 1.0, 1.0)
print("\nremaining misses:")
for q, m in misses:
    print(f"  {q!r} -> missing {m}")

nh = build(hints=False)
print(f"\nhints off : {recall(nh,1,1)[0]:.1%}   hints on : {r:.1%}")
print(f"fk expansion off: {recall(cat,1,1,expand=False)[0]:.1%}")
for k in (3, 6, 10):
    print(f"top_k={k:<3} recall {recall(cat,1,1,k=k)[0]:.1%}")

print("\n--- paraphrase set (no vocabulary overlap) ---")
for label, vw, lw in [("lexical only",0.0,1.0),("hybrid",1.0,1.0)]:
    r,_ = recall(cat, vw, lw, qs=GOLDEN_PARAPHRASE)
    print(f"{label:<12}{r:>9.1%}")
print("swap in SentenceTransformerEmbedder to see whether this closes.")

# ---- second domain --------------------------------------------------------
# The commerce schema shares vocabulary with its own questions, which
# flatters any lexical retriever. This is an unrelated clinical-claims
# schema, where the questions and identifiers mostly do not overlap.
hcat = build(ddl=health.DDL, hint_map=health.HINTS, name="health")
hrecall, hmisses = recall(hcat, 1.0, 1.0, qs=health.GOLDEN)
print(f"\n--- second domain: clinical claims, {len(hcat._docs)} objects ---")
print(f"recall@6       {hrecall:>9.1%}")
for q, m in hmisses:
    print(f"  {q!r} -> missing {m}")

# ---- star schema: grain, SCD2, role-playing dates, backup copies ----------
wcat = build(ddl=warehouse.DDL, hint_map=warehouse.HINTS, name="warehouse")
wrecall, wmisses = recall(wcat, 1.0, 1.0, qs=warehouse.GOLDEN)
wdec = 0
for q, wanted, decoy in warehouse.DECOYS:
    names = [d.name for d in wcat.select(q, top_k=8, expand_fks=False).objects]
    wdec += wanted in names and (decoy not in names or names.index(wanted) < names.index(decoy))
print(f"\n--- star schema: claims warehouse, {len(wcat._docs)} objects, "
      f"{len(wcat.shadows())} backup/staging copies ---")
print(f"recall@6       {wrecall:>9.1%}")
print(f"real table beats its copy   {wdec}/{len(warehouse.DECOYS)}")
for q, m in wmisses:
    print(f"  {q!r} -> missing {m}")

# ---- more domains: same contract -----------------------------------------
extra = {}
for label, mod in (("finance", finance), ("telemetry", telemetry)):
    c = build(ddl=mod.DDL, hint_map=mod.HINTS, name=label)
    r, _ = recall(c, 1.0, 1.0, qs=mod.GOLDEN)
    dec = 0
    for q, wanted, decoy in mod.DECOYS:
        names = [d.name for d in c.select(q, top_k=8, expand_fks=False).objects]
        dec += wanted in names and (decoy not in names or names.index(wanted) < names.index(decoy))
    extra[label] = (r, dec, len(mod.DECOYS), len(c._docs), len(c.shadows()))
    print(f"\n--- {label}: {len(c._docs)} objects, {len(c.shadows())} backup/staging copies ---")
    print(f"recall@6       {r:>9.1%}")
    print(f"real table beats its copy   {dec}/{len(mod.DECOYS)}")

# ---- prompt size ----------------------------------------------------------
# The other half of the claim: selection is only worth doing if the prompt
# actually gets smaller. Compare sending every object's DDL on every call
# against sending only what select() returned.
print(f"\n--- prompt tokens, measured with {TOKEN_METHOD} ---")
full = count_tokens("\n\n".join(d.render_ddl() for d in cat._docs.values()))
per_q = [count_tokens(cat.select(q, top_k=6).prompt_fragment()) for q, _ in GOLDEN]
avg = sum(per_q) / len(per_q)
print(f"full schema, every call : {full:>6,}   ({len(cat._docs)} objects)")
print(f"ashiq, average       : {avg:>6,.0f}   (min {min(per_q):,}  max {max(per_q):,})")
reduction = 1 - avg / full
print(f"reduction               : {reduction:>6.1%}")

# ---- gate ----------------------------------------------------------------
# CI runs this file, so make it fail rather than merely print when a change
# regresses the two numbers the README advertises.
hybrid_recall, _ = recall(cat, 1.0, 1.0)
failures = []
if hybrid_recall < 1.0:
    failures.append(f"hybrid recall@6 fell to {hybrid_recall:.1%}, expected 100%")
if hrecall < 1.0:
    failures.append(f"second-domain recall@6 fell to {hrecall:.1%}, expected 100%")
if wrecall < 1.0:
    failures.append(f"warehouse recall@6 fell to {wrecall:.1%}, expected 100%")
if wdec < len(warehouse.DECOYS):
    failures.append(f"a backup/staging copy outranked its real table ({wdec}/{len(warehouse.DECOYS)})")
for label, (r, dec, n, _, _) in extra.items():
    if r < 1.0:
        failures.append(f"{label} recall@6 fell to {r:.1%}, expected 100%")
    if dec < n:
        failures.append(f"{label}: a copy outranked its real table ({dec}/{n})")
if reduction < 0.70:
    failures.append(f"token reduction fell to {reduction:.1%}, expected >=70%")
if failures:
    print("\nBENCH FAILED:")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("\nbench OK: README claims hold")
