"""Catálogo de dispositivos y escenas. Ya no es un archivo: se arma con los actuadores autorizados de la tabla del hogar.

Es la única fuente de verdad de lo que el LLM puede elegir: ids y acciones que no estén aquí se descartan.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from alivia_edge.commands.domain.models import Action, ActionResult, ActuatorTarget
from alivia_edge.shared.domain.text_es import capfirst, join_es


@dataclass
class ActionSpec:
    name: str  # infinitivo: "cerrar"
    say: str  # subjuntivo para "¿Quiere que ... la puerta?": "cierre"
    done: str  # participio concordado: "cerrada"
    payload: str  # lo que viaja por MQTT: "close"


def _open_close() -> dict[str, ActionSpec]:
    return {
        "abrir": ActionSpec("abrir", "abra", "abierta", "open"),
        "cerrar": ActionSpec("cerrar", "cierre", "cerrada", "close"),
    }


def _on_off() -> dict[str, ActionSpec]:
    return {
        "encender": ActionSpec("encender", "encienda", "encendida", "on"),
        "apagar": ActionSpec("apagar", "apague", "apagada", "off"),
    }


# Qué se puede hacer con cada tipo de actuador (todos son sustantivos femeninos: "quedó cerrada").
KIND_NOUN = {"DOOR": "la puerta", "WINDOW": "la ventana", "CURTAIN": "la cortina", "LIGHT": "la luz"}
KIND_ALIASES = {
    "DOOR": ["puerta", "entrada"],
    "WINDOW": ["ventana"],
    "CURTAIN": ["cortina"],
    "LIGHT": ["luz", "foco", "lámpara", "las luces"],
}
KIND_SPECS = {"DOOR": _open_close, "WINDOW": _open_close, "CURTAIN": _open_close, "LIGHT": _on_off}
# "Cierra todo": qué acción se aplica a cada tipo (las cortinas se dejan como están).
SCENE_ALL = {"DOOR": "cerrar", "WINDOW": "cerrar", "LIGHT": "apagar"}


def payload_for(kind: str, action_name: str) -> str | None:
    specs = KIND_SPECS.get(kind)
    spec = specs().get(action_name) if specs else None
    return spec.payload if spec else None


def device_key(kind: str, device_id: int) -> str:
    """Id del catálogo: tipo y número del backend, p. ej. "door_12"."""
    return f"{kind.lower()}_{device_id}"


def parse_device_key(key: str) -> int | None:
    try:
        return int(key.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return None


@dataclass
class Device:
    id: str
    name: str  # como se dice en voz alta: "la puerta de la sala"
    room: str = ""
    aliases: list[str] = field(default_factory=list)
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    target: ActuatorTarget | None = None


@dataclass
class Scene:
    name: str
    description: str
    actions: list[Action]
    examples: list[str] = field(default_factory=list)


def with_de(room: str) -> str:
    """"de" + habitación con contracción: "la sala" -> "de la sala", "el dormitorio" -> "del dormitorio"."""
    return "del " + room[3:] if room.startswith("el ") else f"de {room}"


def humanize_room(room_key: str, labels: dict[str, str] | None = None) -> str:
    """Cómo se dice una habitación en voz alta: etiqueta configurada o el roomKey con espacios."""
    if labels and room_key in labels:
        return labels[room_key]
    return room_key.replace("_", " ").replace("-", " ").strip()


class Catalog:
    def __init__(self, devices: list[Device], scenes: list[Scene] | None = None, key: object = None) -> None:
        self.devices = {d.id: d for d in devices}
        self.scenes = {s.name: s for s in (scenes or [])}
        self.key = key  # identifica la versión de la tabla de la que salió (para cachear prompts)
        for s in self.scenes.values():
            for a in s.actions:
                if not self.valid(a):
                    raise ValueError(f"escena '{s.name}': acción inválida {a}")

    @classmethod
    def from_targets(
        cls, targets: list[ActuatorTarget], labels: dict[str, str] | None = None, key: object = None
    ) -> "Catalog":
        devices: list[Device] = []
        for t in sorted(targets, key=lambda t: t.device_id):
            if t.kind not in KIND_SPECS:
                continue
            room = humanize_room(t.room_key, labels)
            noun = KIND_NOUN[t.kind]
            name = f"{noun} {with_de(room)}" if room else noun
            aliases = list(KIND_ALIASES[t.kind])
            if t.function and t.function.lower() not in (a.lower() for a in aliases):
                aliases.append(t.function)
            devices.append(Device(device_key(t.kind, t.device_id), name, room, aliases, KIND_SPECS[t.kind](), t))
        scenes: list[Scene] = []
        steps = [Action(d.id, SCENE_ALL[d.target.kind]) for d in devices if d.target and d.target.kind in SCENE_ALL]
        if steps:
            scenes.append(Scene(
                "todo",
                "El usuario se va a dormir, a descansar o a salir, o dice 'cierra todo'",
                steps,
                ["ya me voy a descansar, me cierras todo"],
            ))
        return cls(devices, scenes, key)

    # --- validación --------------------------------------------------------------------
    def valid(self, a: Action) -> bool:
        d = self.devices.get(a.device)
        return d is not None and a.action in d.actions

    def all_action_names(self) -> list[str]:
        return sorted({name for d in self.devices.values() for name in d.actions})

    def payload(self, a: Action) -> str:
        return self.devices[a.device].actions[a.action].payload

    def speech_hint(self, max_chars: int = 280) -> str:
        """Texto corto con el vocabulario real (dispositivos y habitaciones) para sesgar a Whisper (initial_prompt).

        Whisper tiene un límite de ~224 tokens para el prompt; se corta antes de llegar ahí.
        """
        parts: list[str] = []
        for d in self.devices.values():
            verb = next(iter(d.actions.values())).name
            parts.append(f"{verb} {d.name}")
        text = "Alivia. Órdenes para la casa: " + ", ".join(parts) + "."
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars].rsplit(",", 1)[0]
        return cut + "."

    # --- frases en español ---------------------------------------------------------------
    def phrase(self, a: Action) -> str:
        d = self.devices[a.device]
        return f"{d.actions[a.action].say} {d.name}"  # "cierre la puerta de la sala"

    def describe(self, actions: list[Action]) -> str:
        return join_es([self.phrase(a) for a in actions])

    def done_phrase(self, a: Action) -> str:
        d = self.devices[a.device]
        return f"{d.name} quedó {d.actions[a.action].done}"  # "la puerta de la sala quedó cerrada"

    def fail_phrase(self, a: Action) -> str:
        return f"no pude {a.action} {self.devices[a.device].name}"

    def timeout_phrase(self, a: Action) -> str:
        return f"no recibí confirmación de {a.action} {self.devices[a.device].name}"

    def denied_phrase(self, a: Action) -> str:
        return f"no tengo permiso para {a.action} {self.devices[a.device].name}"

    def summarize(self, results: list[ActionResult]) -> str:
        def bad_phrase(r: ActionResult) -> str:
            if r.status == "timeout":
                return self.timeout_phrase(r.action)
            if r.status == "denied":
                return self.denied_phrase(r.action)
            return self.fail_phrase(r.action)

        ok = [self.done_phrase(r.action) for r in results if r.status == "ok"]
        bad = [bad_phrase(r) for r in results if r.status != "ok"]
        if ok and not bad:
            return f"Listo. {capfirst(join_es(ok))}."
        if ok:
            return f"{capfirst(join_es(ok))}, pero {join_es(bad)}."
        return f"Lo siento, {join_es(bad)}."
