"""Qué es un pedido de ayuda, qué se informa al backend y cuándo."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field

HELP_REQUESTED = "HELP_REQUESTED"
LOW_BATTERY = "LOW_BATTERY"

# Frases que siempre son un pedido de ayuda.
_ALWAYS = re.compile(
    r"\b(auxilio|socorro|emergencia|me cai|me he caido|me caigo|no me puedo levantar|no puedo levantarme"
    r"|me siento mal|me duele|llama a mi|llamen a|llama a alguien|necesito ayuda|pide ayuda|ayuda por favor)\b"
)
# "ayuda", "ayudame", "ayudenme"... salvo que sea para mover un dispositivo ("ayudame a cerrar la puerta").
_HELP = re.compile(r"\bayud\w*\b")
_HELP_WITH_DEVICE = re.compile(r"\bayud\w*\s+(a\s+)?(abrir|cerrar|prender|encender|apagar|subir|bajar)\b")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def is_help_request(text: str | None) -> bool:
    """True cuando la persona pide ayuda, no una acción sobre la casa."""
    if not text:
        return False
    t = _normalize(text)
    if _ALWAYS.search(t):
        return True
    return bool(_HELP.search(t)) and not _HELP_WITH_DEVICE.search(t)


@dataclass(frozen=True)
class EdgeEvent:
    """Un evento con su id: si se reenvía tras un corte de red, el backend no vuelve a notificar."""

    type: str
    device_id: int | None = None
    battery_pct: int | None = None
    detail: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_json(self) -> dict:
        body: dict = {"eventId": self.event_id, "type": self.type}
        if self.device_id is not None:
            body["deviceId"] = self.device_id
        if self.battery_pct is not None:
            body["batteryPct"] = self.battery_pct
        if self.detail:
            body["detail"] = self.detail[:200]
        return body


class BatteryWatch:
    """Avisa una vez cuando la batería de un nodo baja del umbral, y otra vez solo después de recargarse."""

    def __init__(self, low_pct: int, rearm_pct: int) -> None:
        if not 0 < low_pct < rearm_pct <= 100:
            raise ValueError("se espera 0 < low_battery_pct < battery_rearm_pct <= 100")
        self._low, self._rearm = low_pct, rearm_pct
        self._warned: set[str] = set()

    def check(self, node: str, battery_pct) -> bool:
        try:
            pct = float(battery_pct)
        except (TypeError, ValueError):
            return False
        if pct >= self._rearm:
            self._warned.discard(node)
            return False
        if pct <= self._low and node not in self._warned:
            self._warned.add(node)
            return True
        return False
