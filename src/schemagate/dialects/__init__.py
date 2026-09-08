"""Per-dialect refinements to reflection.

``schemagate.introspect`` is deliberately free of vendor SQL: the Inspector
does the work and anything SQLAlchemy supports is supported. A handful of
things the Inspector cannot know are worth a dictionary query on specific
engines -- which schemas the vendor itself installs, the real name of a
column type SQLAlchemy has no class for. Those live here, one module per
dialect, and are looked up by ``engine.dialect.name``. No hook, no query.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set

#: dialect name -> callable(engine) -> schema names to leave out
MAINTAINED_SCHEMAS: Dict[str, Callable] = {}
#: dialect name -> callable(engine, schema, table, columns) -> None (in place)
FILL_UNKNOWN_TYPES: Dict[str, Callable] = {}


def vendor_maintained(engine) -> Set[str]:
    hook = MAINTAINED_SCHEMAS.get(engine.dialect.name)
    if hook is None:
        return set()
    try:
        return set(hook(engine))
    except Exception:
        # over-inclusive beats a silently missing table
        return set()


def fill_unknown_types(engine, schema: Optional[str], table: str, columns: List[dict]) -> None:
    hook = FILL_UNKNOWN_TYPES.get(engine.dialect.name)
    if hook is None:
        return
    try:
        hook(engine, schema, table, columns)
    except Exception:
        pass


from . import oracle as _oracle  # noqa: E402,F401  (registers its hooks)
