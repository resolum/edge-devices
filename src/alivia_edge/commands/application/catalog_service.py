from __future__ import annotations

from alivia_edge.commands.domain.catalog import Catalog
from alivia_edge.commands.integration.access_acl import AccessAntiCorruptionLayer


class CatalogService:
    """Catálogo vigente: se reconstruye solo cuando cambia la versión de la tabla de acceso."""

    def __init__(self, acl: AccessAntiCorruptionLayer, room_labels: dict[str, str] | None = None) -> None:
        self._acl = acl
        self._labels = dict(room_labels or {})
        self._cached: Catalog | None = None
        self._cached_key: object = "sin-construir"

    def current(self) -> Catalog:
        key = self._acl.table_key()
        if self._cached is None or key != self._cached_key:
            self._cached = Catalog.from_targets(self._acl.authorized_targets(), self._labels, key)
            self._cached_key = key
        return self._cached

    def room_label(self, room_key: str) -> str:
        from alivia_edge.commands.domain.catalog import humanize_room

        return humanize_room(room_key, self._labels) if room_key else ""
