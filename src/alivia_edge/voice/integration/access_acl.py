"""Capa anticorrupción voice -> access: traduce la autorización del backend a lo que el contexto de voz entiende."""
from __future__ import annotations

from dataclasses import dataclass

from alivia_edge.access.application.services import AccessService
from alivia_edge.access.domain.errors import AccessDenied, InvalidMacAddress
from alivia_edge.access.domain.value_objects import MacAddress


class MicrophoneDenied(Exception):
    """El micrófono no puede enviar datos. El texto es el motivo que se le informa al nodo."""


@dataclass(frozen=True)
class AuthorizedMicrophone:
    mac: str  # forma canónica
    room_key: str


class MicrophoneAccessGate:
    def __init__(self, access: AccessService) -> None:
        self._access = access

    def authorize(self, raw_mac: str) -> AuthorizedMicrophone:
        try:
            mac = MacAddress(raw_mac)
        except InvalidMacAddress as exc:
            raise MicrophoneDenied("MAC inválida") from exc
        try:
            node = self._access.authorize_microphone(mac)
        except AccessDenied as exc:
            raise MicrophoneDenied(str(exc)) from exc
        return AuthorizedMicrophone(mac.value, node.room_key)

    def still_allowed(self, mac: str) -> bool:
        """¿Sigue permitido con la tabla de ahora? Se consulta cada vez que la tabla cambia."""
        try:
            self.authorize(mac)
            return True
        except MicrophoneDenied:
            return False

    def room_key(self, mac: str) -> str:
        try:
            return self.authorize(mac).room_key
        except MicrophoneDenied:
            return ""

    def subscribe(self, listener) -> None:
        self._access.subscribe(listener)
