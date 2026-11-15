from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LinkClosed(Exception):
    """El enlace con el nodo se cerró o dejó de responder."""


class Link(ABC):
    kind: str = "?"  # "ws" | "bt"
    peer: str = ""
    # Prioridad cuando el mismo nodo aparece por dos transportes (mayor gana): WiFi antes que Bluetooth.
    priority: int = 0

    @abstractmethod
    async def recv(self) -> tuple[str, Any]:
        """('json', dict) | ('audio', bytes). Lanza LinkClosed al cerrarse."""

    @abstractmethod
    async def send_json(self, obj: dict) -> None: ...

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Un mensaje de audio: tamaño par y <= 2048 bytes."""

    @abstractmethod
    async def close(self, reason: str = "") -> None: ...

    @property
    @abstractmethod
    def closed(self) -> bool: ...
