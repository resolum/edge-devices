from __future__ import annotations

from typing import Protocol

from alivia_edge.access.domain.entities import EdgeAccessTable


class AccessTableRepository(Protocol):
    """Persistencia de la última tabla recibida (el edge sigue funcionando sin internet)."""

    def load(self) -> EdgeAccessTable | None: ...

    def save(self, table: EdgeAccessTable) -> None: ...

    def clear(self) -> None: ...
