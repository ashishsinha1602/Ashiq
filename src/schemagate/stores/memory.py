"""In-process Store. Zero dependencies; the reference implementation."""
from __future__ import annotations
from typing import Dict
from ..embedder import cosine_distance


class MemoryStore:
    def __init__(self) -> None:
        self._rows: Dict[str, Dict[str, dict]] = {}

    def upsert(self, ns, key, vec, payload, scope=None):
        self._rows.setdefault(ns, {})[key] = {
            "key": key, "vec": list(vec), "payload": payload, "scope": scope}

    @staticmethod
    def _visible(row, scope):
        return row["scope"] is None or row["scope"] == scope

    def search(self, ns, vec, k=6, max_distance=1.0, scope=None):
        out = []
        for row in self._rows.get(ns, {}).values():
            if not self._visible(row, scope):
                continue
            d = cosine_distance(vec, row["vec"])
            if d <= max_distance:
                out.append({**row["payload"], "_key": row["key"], "_distance": d})
        out.sort(key=lambda r: r["_distance"])
        return out[:k]

    def all(self, ns, scope=None):
        return [{**r["payload"], "_key": r["key"], "_vec": r["vec"]}
                for r in self._rows.get(ns, {}).values() if self._visible(r, scope)]

    def get(self, ns, key, scope=None):
        row = self._rows.get(ns, {}).get(key)
        return row["payload"] if row and self._visible(row, scope) else None

    def count(self, ns, scope=None):
        return sum(1 for r in self._rows.get(ns, {}).values() if self._visible(r, scope))

    def purge(self, ns):
        n = len(self._rows.get(ns, {}))
        self._rows.pop(ns, None)
        return n
