"""Value objects de la tabla de acceso. Reflejan el contrato del backend (contexto assets)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from alivia_edge.access.domain.errors import InvalidMacAddress

_MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$|^(?:[0-9A-F]{2}-){5}[0-9A-F]{2}$")


@dataclass(frozen=True)
class MacAddress:
    """Forma canónica XX:XX:XX:XX:XX:XX (mayúsculas, con dos puntos), igual que en el backend."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidMacAddress("la MAC debe ser texto")
        text = self.value.strip().upper()
        if not _MAC.match(text):
            raise InvalidMacAddress(f"MAC inválida: {self.value!r} (formato XX:XX:XX:XX:XX:XX)")
        object.__setattr__(self, "value", text.replace("-", ":"))

    def __str__(self) -> str:
        return self.value


class DeviceType(str, Enum):
    MICROPHONE = "MICROPHONE"
    DOOR = "DOOR"
    WINDOW = "WINDOW"
    CURTAIN = "CURTAIN"
    LIGHT = "LIGHT"

    @property
    def is_actuator(self) -> bool:
        return self is not DeviceType.MICROPHONE


class AssetStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    MAINTENANCE = "MAINTENANCE"
    BROKEN = "BROKEN"
    RETIRED = "RETIRED"
