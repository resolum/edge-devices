from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Action:
    device: str  # id del catálogo, p. ej. "door_12"
    action: str  # infinitivo en español: abrir, cerrar, encender, apagar...


@dataclass
class Interpretation:
    status: str  # "ok" | "clarify" | "unknown"
    actions: list[Action] = field(default_factory=list)
    question: str = ""
    raw: str = ""  # JSON original del modelo (para el historial de la conversación)


@dataclass
class ActionResult:
    action: Action
    status: str  # "ok" | "fail" | "timeout" | "denied"
    detail: str = ""


@dataclass(frozen=True)
class ActuatorTarget:
    """Un actuador autorizado, tal como lo necesita este contexto (lo traduce la capa anticorrupción de access)."""

    device_id: int
    mac: str
    kind: str  # DOOR | WINDOW | CURTAIN | LIGHT
    room_key: str
    function: str


@dataclass(frozen=True)
class CommandRequest:
    """Una acción ya validada y autorizada, lista para publicarse."""

    action: Action
    target: ActuatorTarget
    payload: str  # lo que viaja por MQTT: open, close, on, off


@dataclass(frozen=True)
class CommandRecord:
    node: str  # MAC del micrófono que escuchó la orden
    device_id: int | None
    action: str
    status: str  # ok | fail | timeout | denied
    detail: str
    created_at: datetime
