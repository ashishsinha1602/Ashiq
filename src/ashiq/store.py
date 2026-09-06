"""Store protocol. Implement these five methods for any new backend."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Store(Protocol):
    def upsert(self, ns: str, key: str, vec: Sequence[float],
               payload: Dict[str, Any], scope: Optional[str] = None) -> None: ...

    def search(self, ns: str, vec: Sequence[float], k: int = 6,
               max_distance: float = 1.0, scope: Optional[str] = None
               ) -> List[Dict[str, Any]]: ...

    def get(self, ns: str, key: str,
            scope: Optional[str] = None) -> Optional[Dict[str, Any]]: ...

    def count(self, ns: str, scope: Optional[str] = None) -> int: ...

    def purge(self, ns: str) -> int: ...
