"""Tabla de acceso del hogar: instantánea completa de los dispositivos activos, con su versión."""
from __future__ import annotations

from dataclasses import dataclass

from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress


@dataclass(frozen=True)
class NodeRef:
    """Un dispositivo del hogar (entrada de la tabla)."""

    device_id: int
    mac: MacAddress
    device_type: DeviceType
    status: AssetStatus
    room_key: str
    function: str


@dataclass(frozen=True)
class EdgeAccessTable:
    """Siempre es la tabla completa. El edge conserva la de mayor versión y descarta las más viejas."""

    installation_id: int
    household_id: int
    version: int
    nodes: tuple[NodeRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda n: n.device_id)))

    @property
    def etag(self) -> str:
        """Mismo formato que el backend: "<instalación>.<versión>" entre comillas."""
        return f'"{self.installation_id}.{self.version}"'

    def find_by_mac(self, mac: MacAddress) -> NodeRef | None:
        return next((n for n in self.nodes if n.mac == mac), None)

    def find_by_id(self, device_id: int) -> NodeRef | None:
        return next((n for n in self.nodes if n.device_id == device_id), None)

    def supersedes(self, current: "EdgeAccessTable | None") -> bool:
        """¿Esta tabla debe reemplazar a la que el edge ya tiene?

        Sí si no hay tabla, si es de otra instalación (el edge se movió de hogar) o si su versión es mayor.
        La misma versión o una anterior se ignoran: recibir dos veces la misma tabla no cambia nada.
        """
        if current is None or current.installation_id != self.installation_id:
            return True
        return self.version > current.version
