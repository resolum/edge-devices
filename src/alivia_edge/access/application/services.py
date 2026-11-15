"""Casos de uso del contexto access: mantener la tabla vigente y autorizar dispositivos."""
from __future__ import annotations

import logging
from collections.abc import Callable

from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.policy import AccessPolicy
from alivia_edge.access.domain.repositories import AccessTableRepository
from alivia_edge.access.domain.value_objects import MacAddress

log = logging.getLogger(__name__)

Listener = Callable[[EdgeAccessTable | None], None]


class AccessService:
    """Guarda en memoria y en disco la tabla vigente, avisa cuando cambia y aplica la política."""

    def __init__(self, repository: AccessTableRepository) -> None:
        self._repo = repository
        self._table: EdgeAccessTable | None = None
        self._listeners: list[Listener] = []

    @property
    def table(self) -> EdgeAccessTable | None:
        return self._table

    def load(self) -> EdgeAccessTable | None:
        """Arranque: recupera la última tabla guardada (puede no haber ninguna)."""
        self._table = self._repo.load()
        if self._table:
            log.info("Tabla de acceso restaurada: instalación %s, versión %s, %d dispositivos",
                     self._table.installation_id, self._table.version, len(self._table.nodes))
        else:
            log.warning("Sin tabla de acceso guardada: se deniega todo hasta recibir la primera del backend")
        return self._table

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def apply(self, incoming: EdgeAccessTable, *, force: bool = False) -> bool:
        """Reemplaza la tabla si la recibida la supera (o siempre con force). True si cambió."""
        if not force and not incoming.supersedes(self._table):
            return False
        self._repo.save(incoming)
        self._table = incoming
        log.info("Tabla de acceso aplicada: instalación %s, versión %s, %d dispositivos",
                 incoming.installation_id, incoming.version, len(incoming.nodes))
        self._notify()
        return True

    def clear(self, reason: str) -> None:
        """El edge dejó de pertenecer a una instalación: se deniega todo."""
        if self._table is None:
            return
        log.warning("Se borra la tabla de acceso: %s", reason)
        self._repo.clear()
        self._table = None
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self._table)
            except Exception:  # noqa: BLE001
                log.exception("un oyente de la tabla de acceso falló")

    # --- autorización (lanzan AccessDenied) ------------------------------------------------
    def authorize_microphone(self, mac: MacAddress) -> NodeRef:
        return AccessPolicy.authorize_microphone(self._table, mac)

    def authorize_actuator(self, device_id: int) -> NodeRef:
        return AccessPolicy.authorize_actuator(self._table, device_id)
