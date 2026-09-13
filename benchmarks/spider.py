"""schemagate on Spider: table retrieval, per-database and pooled.

Two settings, because only one of them is honest about difficulty.

PER-DATABASE is what Spider is built for: you already know which database the
question belongs to, and the average Spider database has about five tables.
Picking six of five is not retrieval, and a high number there means nothing.
It is reported anyway, as a floor.

POOLED merges every Spider database into one catalog and asks the same
questions without telling it which database to look in. That is the problem
schemagate exists for -- hundreds of tables, most of them irrelevant, many
with colliding names (`name`, `id`, `student`) across unrelated domains -- and
it is reproducible by anyone from the same two public files.

Metric is table recall@k: did the selected set contain every table the gold
SQL reads. Gold tables come from the reference query, not from a model.
"""
import json
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

import pandas as pd                                          # noqa: E402
from schemagate import Catalog, Column, ForeignKey, ObjectDoc  # noqa: E402

TYPE_MAP = {"number": "NUMERIC", "text": "TEXT", "time": "TIMESTAMP",
            "boolean": "BOOLEAN", "others": "TEXT"}


def parse_schemas(path):
    """{db_id: [ObjectDoc, ...]} from the published schema dump."""
    out = {}
    for row in json.loads(pathlib.Path(path).read_text("utf-8")):
        db = row["db_id"]
        pks = {}
        for part in (row.get("Primary Keys") or "").split("|"):
            if ":" in part:
                t, c = part.split(":", 1)
                pks.setdefault(t.strip().lower(), set()).add(c.strip().lower())
        fks = {}
        for part in (row.get("Foreign Keys") or "").split("|"):
            m = re.match(r"\s*(\w+)\s*:\s*(\w+)\s+equals\s+(\w+)\s*:\s*(\w+)", part)
            if m:
                fks.setdefault(m.group(1).lower(), []).append(
                    ForeignKey([m.group(2)], m.group(3), [m.group(4)]))
        docs = []
        for chunk in row["Schema (values (type))"].split("|"):
            if ":" not in chunk:
                continue
            tname, cols = chunk.split(":", 1)
            tname = tname.strip()
            columns = []
            for c in cols.split(","):
                c = c.strip()
                m = re.match(r"(.+?)\s*\((\w+)\)$", c)
                if not m:
                    continue
                cname = m.group(1).strip()
                columns.append(Column(
                    cname, TYPE_MAP.get(m.group(2), "TEXT"), True, None,
                    cname.lower() in pks.get(tname.lower(), set())))
            if columns:
                docs.append(ObjectDoc(
                    name=tname, schema=db, kind="TABLE",
                    columns=columns,
                    foreign_keys=fks.get(tname.lower(), [])))
        out[db] = docs
    return out


GOLD = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w]*)", re.I)


def gold_tables(sql):
    aliases = {a.lower() for a in re.findall(r"\bas\s+(\w+)", sql, re.I)}
    return {t.lower() for t in GOLD.findall(sql)} - aliases


def recall_at(cat, questions, k, restrict_schema=None):
    hit = tot = 0
    covered_all = 0
    for q, gold in questions:
        if not gold:
            continue
        tot += 1
        sel = cat.select(q, top_k=k)
        picked = {n.split(".")[-1].lower() for n in sel.table_names}
        got = len(gold & picked)
        hit += got / len(gold)
        covered_all += (gold <= picked)
    return covered_all, hit, tot


def main():
    schemas = parse_schemas(HERE / "spider_schema.json")
    dev = pd.read_parquet(HERE / "spider_dev.parquet")
    print(f"Spider dev: {len(dev)} questions over {dev.db_id.nunique()} databases")
    print(f"schema dump: {len(schemas)} databases, "
          f"{sum(len(v) for v in schemas.values())} tables total\n")

    by_db = {}
    for _, r in dev.iterrows():
        by_db.setdefault(r.db_id, []).append((r.question, gold_tables(r["query"])))

    sizes = [len(schemas[d]) for d in by_db if d in schemas]
    print(f"per-database size: min {min(sizes)}, median "
          f"{sorted(sizes)[len(sizes)//2]}, max {max(sizes)} tables")

    # ---- setting 1: you already know the database -------------------------
    print("\nPER-DATABASE (Spider's own setting -- a floor, not a result)")
    for k in (3, 5):
        allc = h = t = 0
        for db, qs in by_db.items():
            if db not in schemas:
                continue
            cat = Catalog()
            for d in schemas[db]:
                cat.add(d)
            cat.index()
            a, hh, tt = recall_at(cat, qs, k)
            allc += a; h += hh; t += tt
        print(f"  top_k={k}: all gold tables present {allc}/{t} "
              f"({100*allc/t:.1f}%)   per-table recall {100*h/t:.1f}%")

    # ---- setting 2: one pooled schema, no database hint -------------------
    print("\nPOOLED (every Spider database in one catalog, no hint given)")
    t0 = time.time()
    pooled = Catalog()
    for db, docs in schemas.items():
        for d in docs:
            pooled.add(d)
    pooled.index()
    print(f"  {len(pooled)} tables indexed in {time.time()-t0:.1f}s")
    flat = [q for qs in by_db.values() for q in qs]
    for k in (5, 10, 20):
        a, h, t = recall_at(pooled, flat, k)
        print(f"  top_k={k:2}: all gold tables present {a}/{t} "
              f"({100*a/t:.1f}%)   per-table recall {100*h/t:.1f}%")


if __name__ == "__main__":
    main()
