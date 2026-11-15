"""Puerto hacia el backend (alivia-web-services) y sus errores."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alivia_edge.access.domain.entities import EdgeAccessTable


class BackendUnavailable(Exception):
    """503, 429, red caída o respuesta ilegible: reintentar."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class BackendUnauthorized(Exception):
    """401/403: credencial inválida, desconocida o revocada."""


class EdgeNotAssigned(Exception):
    """404: el edge no está asignado a una instalación activa."""


class TableVersionAhead(Exception):
    """409 table_version_ahead: el backend retrocedió (restauración); pedir la tabla sin ETag."""


@dataclass(frozen=True)
class Fetched:
    """Resultado de pedir la tabla: None en `table` significa 304 (sin cambios)."""

    table: EdgeAccessTable | None


class BackendGateway(Protocol):
    async def fetch_table(self, etag: str | None, wait_s: int) -> Fetched: ...

    async def acknowledge(self, version: int) -> None: ...

    async def aclose(self) -> None: ...
