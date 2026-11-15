"""Política de acceso: decide qué dispositivo puede enviar datos y a cuál se le puede mandar un comando.

Regla común (decidida con el negocio): solo se permite a un dispositivo que está en la tabla vigente y cuyo estado es
IN_USE. Todo lo demás se deniega: sin tabla, no registrado, en mantenimiento, averiado, retirado o disponible.
"""
from __future__ import annotations

from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.errors import AccessDenied
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress

_STATUS_REASON = {
    AssetStatus.MAINTENANCE: "dispositivo en mantenimiento",
    AssetStatus.BROKEN: "dispositivo averiado",
    AssetStatus.RETIRED: "dispositivo retirado",
    AssetStatus.AVAILABLE: "dispositivo sin asignar a un hogar",
}


class AccessPolicy:
    @staticmethod
    def _require_active(node: NodeRef) -> NodeRef:
        if node.status is not AssetStatus.IN_USE:
            raise AccessDenied(_STATUS_REASON.get(node.status, "dispositivo no activo"))
        return node

    @classmethod
    def authorize_microphone(cls, table: EdgeAccessTable | None, mac: MacAddress) -> NodeRef:
        """El micrófono puede conectarse y enviar audio."""
        if table is None:
            raise AccessDenied("sin tabla de acceso del backend")
        node = table.find_by_mac(mac)
        if node is None:
            raise AccessDenied("dispositivo no registrado en el hogar")
        if node.device_type is not DeviceType.MICROPHONE:
            raise AccessDenied("el dispositivo no es un micrófono")
        return cls._require_active(node)

    @classmethod
    def authorize_actuator(cls, table: EdgeAccessTable | None, device_id: int) -> NodeRef:
        """Se le puede mandar un comando al actuador."""
        if table is None:
            raise AccessDenied("sin tabla de acceso del backend")
        node = table.find_by_id(device_id)
        if node is None:
            raise AccessDenied("el actuador ya no está en el hogar")
        if not node.device_type.is_actuator:
            raise AccessDenied("el dispositivo no es un actuador")
        return cls._require_active(node)
