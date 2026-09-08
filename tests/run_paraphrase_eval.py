"""Measure business-language recall on every schema. Prints a table + misses."""
from __future__ import annotations
import os
import sqlite3
import sys
import tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from schemagate import Catalog
import paraphrase_eval as EV

import schema_fixture_health as health
import schema_fixture_warehouse as warehouse
import schema_fixture_finance as finance
import schema_fixture_telemetry as telemetry
import schema_fixture_complex as complex_
from schemagate import demo_schema as commerce

MODS = {"commerce": commerce, "health": health, "warehouse": warehouse,
        "finance": finance, "telemetry": telemetry, "complex": complex_}


def build(name, use_hints=True, use_desc=False):
    m = MODS[name]
    ddl = m.DDL
    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path); con.executescript(ddl); con.commit(); con.close()
    cat = Catalog(name=name).bootstrap(f"sqlite:///{path}")
    if use_hints:
        for t, h in getattr(m, "HINTS", {}).items():
            try: cat.hint(t, h)
            except Exception: pass
    cat.index()
    return cat


def score(cat, questions, top_k=6, show=False):
    hits, misses = 0, []
    for q, gold in questions:
        got = {n.split(".")[-1] for n in cat.select(q, top_k=top_k).table_names}
        if gold & got:
            hits += 1
        else:
            misses.append((q, sorted(gold), sorted(got)[:6]))
    if show:
        for q, gold, got in misses:
            print(f"    MISS  {q!r}\n          want {gold}\n          got  {got}")
    return hits, len(questions), misses


def main(show_misses=("commerce", "health")):
    print(f"{'schema':<12}{'set':<9}{'recall@6':>10}   {'hit/total':>10}")
    print("-" * 46)
    tot_h = tot_n = 0
    per = {}
    for name, qs in EV.ALL.items():
        cat = build(name)
        kind = "TUNE" if name in EV.TUNE else "HELDOUT"
        h, n, misses = score(cat, qs, show=(name in show_misses))
        per[name] = (h, n, misses)
        tot_h += h; tot_n += n
        print(f"{name:<12}{kind:<9}{h/n*100:9.1f}%   {h:>4}/{n:<5}")
    print("-" * 46)
    th = sum(per[k][0] for k in EV.TUNE); tn = sum(per[k][1] for k in EV.TUNE)
    hh = sum(per[k][0] for k in EV.HELDOUT); hn = sum(per[k][1] for k in EV.HELDOUT)
    print(f"{'TUNE':<21}{th/tn*100:9.1f}%   {th:>4}/{tn:<5}")
    print(f"{'HELD OUT':<21}{hh/hn*100:9.1f}%   {hh:>4}/{hn:<5}")
    print(f"{'OVERALL':<21}{tot_h/tot_n*100:9.1f}%   {tot_h:>4}/{tot_n:<5}")
    return per


if __name__ == "__main__":
    main()
