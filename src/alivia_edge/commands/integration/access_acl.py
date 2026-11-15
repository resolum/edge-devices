"""Capa anticorrupción commands -> access: traduce la tabla de acceso a los conceptos de este contexto."""
from __future__ import annotations

from alivia_edge.access.application.services import AccessService
from alivia_edge.access.domain.errors import AccessDenied
from alivia_edge.commands.domain.models import ActuatorTarget


def _target(node) -> ActuatorTarget:
    return ActuatorTarget(node.device_id, node.mac.value, node.device_type.value, node.room_key, node.function)


class AccessAntiCorruptionLayer:
    def __init__(self, access: AccessService) -> None:
        self._access = access

    def table_key(self) -> tuple[int, int] | None:
        t = self._access.table
        return (t.installation_id, t.version) if t else None

    def authorized_targets(self) -> list[ActuatorTarget]:
        """Actuadores que hoy se pueden comandar (en la tabla y IN_USE)."""
        t = self._access.table
        if t is None:
            return []
        out = []
        for node in t.nodes:
            try:
                out.append(_target(self._access.authorize_actuator(node.device_id)))
            except AccessDenied:
                continue
        return out

    def authorize(self, device_id: int) -> ActuatorTarget:
        """Ahora mismo (no cuando se armó el catálogo). Lanza AccessDenied con el motivo."""
        return _target(self._access.authorize_actuator(device_id))
