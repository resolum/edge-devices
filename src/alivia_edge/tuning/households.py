"""Casas sintéticas (tabla de actuadores + habitación del micrófono) y la casa fija de las pruebas escritas a mano."""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from alivia_edge.commands.domain.catalog import Catalog
from alivia_edge.commands.domain.models import ActuatorTarget

ROOM_POOL = {
    "sala": "la sala",
    "cocina": "la cocina",
    "dormitorio": "el dormitorio",
    "dormitorio_2": "el segundo dormitorio",
    "bano": "el baño",
    "entrada": "la entrada",
    "patio": "el patio",
    "cochera": "la cochera",
    "comedor": "el comedor",
    "estudio": "el estudio",
}
# probabilidad de que una habitación tenga cada tipo de actuador
_P = {"LIGHT": 0.95, "WINDOW": 0.55, "DOOR": 0.30, "CURTAIN": 0.35}
_NO_CURTAIN = {"bano", "cochera", "patio"}


@dataclass
class Household:
    hid: str
    targets: list[ActuatorTarget]
    labels: dict[str, str]
    mic_room: str = ""  # etiqueta hablada ("la sala"), como la recibe el LLM; vacío si no se conoce
    _catalog: Catalog | None = field(default=None, repr=False, compare=False)

    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = Catalog.from_targets(self.targets, self.labels, key=self.hid)
        return self._catalog

    def to_meta(self) -> dict:
        return {
            "hid": self.hid,
            "mic_room": self.mic_room,
            "labels": self.labels,
            "targets": [[t.device_id, t.kind, t.room_key] for t in self.targets],
        }

    @classmethod
    def from_meta(cls, m: dict) -> "Household":
        return cls(m["hid"], [_target(i, k, r) for i, k, r in m["targets"]], dict(m["labels"]), m.get("mic_room", ""))


def _target(device_id: int, kind: str, room_key: str) -> ActuatorTarget:
    mac = f"AA:BB:CC:00:{device_id // 256:02X}:{device_id % 256:02X}"
    return ActuatorTarget(device_id=device_id, mac=mac, kind=kind, room_key=room_key, function="")


def random_household(hid: str, rng: random.Random) -> Household:
    rooms = rng.sample(list(ROOM_POOL), rng.randint(2, 5))
    next_id = rng.randint(10, 40)
    targets: list[ActuatorTarget] = []
    for room in rooms:
        kinds = []
        for kind, p in _P.items():
            if kind == "CURTAIN" and room in _NO_CURTAIN:
                continue
            q = 0.95 if (kind == "DOOR" and room in ("entrada", "cochera")) else p
            if rng.random() < q:
                kinds.append(kind)
        if not kinds:
            kinds = ["LIGHT"]
        for kind in kinds:
            targets.append(_target(next_id, kind, room))
            next_id += rng.randint(1, 3)
    labels = {r: ROOM_POOL[r] for r in rooms}
    mic = labels[rng.choice(rooms)] if rng.random() < 0.85 else ""
    return Household(hid, targets, labels, mic)


def fixed_household(mic_room: str = "la sala") -> Household:
    """La casa de las pruebas escritas a mano (handwritten.py)."""
    spec = [
        (10, "DOOR", "entrada"), (11, "LIGHT", "sala"), (12, "WINDOW", "sala"), (13, "LIGHT", "cocina"),
        (14, "WINDOW", "dormitorio"), (15, "CURTAIN", "dormitorio"), (16, "LIGHT", "dormitorio"),
    ]
    labels = {"sala": "la sala", "cocina": "la cocina", "dormitorio": "el dormitorio", "entrada": "la entrada"}
    return Household("fija", [_target(*s) for s in spec], labels, mic_room)
