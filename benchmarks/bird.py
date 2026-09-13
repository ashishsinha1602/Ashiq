"""schemagate on BIRD dev: table retrieval, per-database and pooled.

BIRD is the harder of the two: the questions are written the way people ask,
not the way the schema is named, and the databases carry real column naming
rather than tidy benchmark naming. 1,534 questions, 11 databases, 75 tables.

Same two settings as the Spider run. Per-database is the floor -- the largest
BIRD database is 13 tables. Pooled puts all 11 in one catalog with no hint,
which is a fairer test of retrieval and still far smaller than a real
warehouse.

BIRD ships an `evidence` string per question -- a human hint like "eligible
free rate = Free Meal Count / Enrollment". Retrieval is measured without it,
since the point is what the question alone can find, and then with it, since
that is what a real caller would pass through.

Gold tables come from BIRD's reference SQL.
"""
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

from schemagate import Catalog, Column, ForeignKey, ObjectDoc   # noqa: E402

DEV = HERE / "dev_20240627" / "dev.json"
TABLES = HERE / "dev_20240627" / "dev_tables.json"


def build_docs(spec):
    """ObjectDocs for one BIRD database from its tables.json entry."""
    names = spec["table_names_original"]
    cols = spec["column_names_original"]          # [[table_idx, col_name], ...]
    types = spec["column_types"]
    pks = set()
    for pk in spec.get("primary_keys") or []:
        for idx in (pk if isinstance(pk, list) else [pk]):
            pks.add(idx)

    per_table = {i: [] for i in range(len(names))}
    for ci, (ti, cname) in enumerate(cols):
        if ti < 0:
            continue
        per_table[ti].append(Column(cname, types[ci].upper(), True, None, ci in pks))

    fks = {i: [] for i in range(len(names))}
    for a, b in spec.get("foreign_keys") or []:
        ta, ca = cols[a]
        tb, cb = cols[b]
        if ta < 0 or tb < 0:
            continue
        fks[ta].append(ForeignKey([ca], names[tb], [cb]))

    return [ObjectDoc(name=names[i], schema=spec["db_id"], kind="TABLE",
                      columns=per_table[i], foreign_keys=fks[i])
            for i in range(len(names)) if per_table[i]]


GOLD = re.compile(r"\b(?:from|join)\s+[`\"\[]?([A-Za-z_][\w ]*?)[`\"\]]?\s*(?:\bas\b|\bon\b|,|\)|$|\s)", re.I)


def gold_tables(sql, known):
    found = {t.strip().lower() for t in GOLD.findall(sql)}
    return {t for t in found if t in known}


def score(cat, cases, k):
    full = partial = tot = 0
    for q, gold in cases:
        if not gold:
            continue
        tot += 1
        picked = {n.split(".")[-1].lower()
                  for n in cat.select(q, top_k=k).table_names}
        full += (gold <= picked)
        partial += len(gold & picked) / len(gold)
    return full, partial, tot


def main():
    specs = {s["db_id"]: s for s in json.loads(TABLES.read_text("utf-8"))}
    dev = json.loads(DEV.read_text("utf-8"))
    docs = {db: build_docs(s) for db, s in specs.items()}
    known = {db: {d.name.lower() for d in ds} for db, ds in docs.items()}

    print(f"BIRD dev: {len(dev)} questions, {len(specs)} databases, "
          f"{sum(len(v) for v in docs.values())} tables")
    sizes = sorted(len(v) for v in docs.values())
    print(f"per-database size: min {sizes[0]}, median {sizes[len(sizes)//2]}, "
          f"max {sizes[-1]} tables\n")

    plain, with_ev = {}, {}
    for r in dev:
        db = r["db_id"]
        g = gold_tables(r["SQL"], known.get(db, set()))
        plain.setdefault(db, []).append((r["question"], g))
        ev = (r.get("evidence") or "").strip()
        with_ev.setdefault(db, []).append(
            ((r["question"] + " " + ev).strip(), g))

    for label, data in (("question only", plain), ("question + BIRD evidence", with_ev)):
        print(f"PER-DATABASE, {label}")
        for k in (3, 5):
            F = P = T = 0
            for db, cases in data.items():
                cat = Catalog()
                for d in docs[db]:
                    cat.add(d)
                cat.index()
                f, p, t = score(cat, cases, k)
                F += f; P += p; T += t
            print(f"  top_k={k}: all gold tables present {F}/{T} "
                  f"({100*F/T:.1f}%)   per-table recall {100*P/T:.1f}%")

        pooled = Catalog()
        for ds in docs.values():
            for d in ds:
                pooled.add(d)
        pooled.index()
        flat = [c for cs in data.values() for c in cs]
        print(f"POOLED ({len(pooled)} tables, no database hint), {label}")
        for k in (5, 10):
            f, p, t = score(pooled, flat, k)
            print(f"  top_k={k:2}: all gold tables present {f}/{t} "
                  f"({100*f/t:.1f}%)   per-table recall {100*p/t:.1f}%")
        print()


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"({time.time()-t0:.0f}s)")
