"""Genera órdenes en español (con la forma en que Whisper las suele escribir) y la salida JSON correcta para cada una.

El prompt de sistema de cada muestra es EXACTAMENTE el que usa el edge en producción (build_system_prompt), y cada salida
correcta se valida con parse_interpretation: lo que se entrena es lo que el sistema luego acepta.

Se parte por casas (no por muestras): las casas de prueba no aparecen en el entrenamiento.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from alivia_edge.commands.domain.catalog import Device, with_de
from alivia_edge.commands.infrastructure.llm import build_system_prompt, parse_interpretation
from alivia_edge.tuning.households import Household, random_household

UNKNOWN_Q = "No le entendí. ¿Qué desea hacer?"
OFF_TOPIC = [
    "¿qué hora es?", "cuéntame un chiste", "pon música", "gracias", "hola", "¿cómo está el clima?", "dime la hora",
    "sube el volumen", "llama a mi mamá", "¿cuánto es dos más dos?", "qué día es hoy", "enciende el ventilador",
    "prende el televisor", "apaga la tele", "enciende el aire acondicionado", "abre el refrigerador", "buenos días",
]
VERBS = {
    "abrir": ["abre {x}", "abre {x} por favor", "ábreme {x}", "puedes abrir {x}", "quiero que abras {x}", "necesito que abras {x}"],
    "cerrar": ["cierra {x}", "cierra {x} por favor", "ciérrame {x}", "puedes cerrar {x}", "quiero que cierres {x}", "necesito que cierres {x}"],
    "encender": ["enciende {x}", "prende {x}", "enciéndeme {x}", "préndeme {x}", "puedes encender {x}", "quiero que enciendas {x}"],
    "apagar": ["apaga {x}", "apágame {x}", "apaga {x} por favor", "puedes apagar {x}", "quiero que apagues {x}"],
}
FOLLOW_ACTION = {
    "abrir": ["ábrela", "abre", "que se abra", "ábrela por favor"],
    "cerrar": ["ciérrala", "cierra", "que se cierre", "ciérrala por favor"],
    "encender": ["enciéndela", "préndela", "enciende", "que se encienda"],
    "apagar": ["apágala", "apaga", "que se apague"],
}
NOUNS = {"DOOR": ["la puerta"], "WINDOW": ["la ventana"], "CURTAIN": ["la cortina"], "LIGHT": ["la luz", "la luz", "el foco", "la lámpara"]}
PLURAL = {"DOOR": "las puertas", "WINDOW": "las ventanas", "CURTAIN": "las cortinas", "LIGHT": "las luces"}
SCENE_SAYS = [
    "ya me voy a dormir", "cierra todo", "me voy a descansar, cierra todo", "buenas noches, ya me voy a dormir",
    "voy a salir, deja todo cerrado", "me voy a acostar", "ya me voy, cierra todo por favor", "ya me voy a descansar, me cierras todo",
]
NOISE = {"ventana": "bentana", "cierra": "sierra", "enciende": "en ciende", "apaga": "a paga"}


@dataclass
class Sample:
    household: Household
    history: list[dict]  # turnos previos y el último del usuario, sin el prompt de sistema
    gold: dict
    tag: str

    def messages(self) -> list[dict]:
        sysmsg = build_system_prompt(self.household.catalog(), self.household.mic_room)
        return [{"role": "system", "content": sysmsg}, *self.history, {"role": "assistant", "content": dumps(self.gold)}]

    def eval_row(self) -> dict:
        return {"household": self.household.to_meta(), "history": self.history, "gold": self.gold, "tag": self.tag}


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def gold(status: str, actions: list[tuple[Device, str]] | None = None, *, scene: str = "", question: str = "") -> dict:
    return {
        "status": status,
        "scene": scene,
        "actions": [{"device": d.id, "action": a} for d, a in (actions or [])],
        "question": question,
    }


# --- estilo de lo que escribe Whisper -------------------------------------------------------
def finish(text: str, rng: random.Random) -> str:
    if rng.random() < 0.06:
        for k, v in NOISE.items():
            if k in text:
                text = text.replace(k, v, 1)
                break
    if rng.random() < 0.2 and not text.startswith(("puedes", "quiero", "necesito")):
        text = rng.choice(["oye ", "por favor ", "a ver ", "mira "]) + text
    if rng.random() < 0.55:
        text = text[0].upper() + text[1:] + rng.choice([".", "."])
    return text


def say(action: str, x: str, rng: random.Random) -> str:
    return rng.choice(VERBS[action]).format(x=x)


def kind_of(d: Device) -> str:
    return d.target.kind if d.target else ""


def by_kind(h: Household, kind: str) -> list[Device]:
    return [d for d in h.catalog().devices.values() if kind_of(d) == kind]


def pick_action(d: Device, rng: random.Random) -> str:
    return rng.choice(list(d.actions))


def full_ref(d: Device, rng: random.Random) -> str:
    return f"{rng.choice(NOUNS[kind_of(d)])} {with_de(d.room)}"


# --- intenciones --------------------------------------------------------------------------------
def _single_full(h, rng):
    d = rng.choice(list(h.catalog().devices.values()))
    a = pick_action(d, rng)
    return [Sample(h, [{"role": "user", "content": finish(say(a, full_ref(d, rng), rng), rng)}], gold("ok", [(d, a)]), "single_full")]


def _bare_candidates(h):
    out = []
    for kind in ("DOOR", "WINDOW", "CURTAIN", "LIGHT"):
        ds = by_kind(h, kind)
        if ds:
            out.append((kind, ds))
    return out


def _resolve_bare(h, kind, ds):
    """Qué dispositivo se entiende con "la luz" a secas: el único, o el de la habitación del usuario; si no, se pregunta."""
    if len(ds) == 1:
        return ds[0]
    here = [d for d in ds if d.room == h.mic_room and h.mic_room]
    return here[0] if len(here) == 1 else None


def _bare(h, rng):
    cands = _bare_candidates(h)
    ambiguous = [(k, ds) for k, ds in cands if _resolve_bare(h, k, ds) is None]
    kind, ds = rng.choice(ambiguous if ambiguous and rng.random() < 0.5 else cands)
    a = rng.choice(list(ds[0].actions))
    x = rng.choice(NOUNS[kind])
    text = finish(say(a, x, rng), rng)
    target = _resolve_bare(h, kind, ds)
    if target:
        return [Sample(h, [{"role": "user", "content": text}], gold("ok", [(target, a)]), "bare_resolved")]
    opts = " o ".join(f"la {with_de(d.room)}" for d in ds)
    g = gold("clarify", question=f"¿Cuál desea {a}: {opts}?")
    out = [Sample(h, [{"role": "user", "content": text}], g, "bare_ambiguous")]
    pick = rng.choice(ds)  # y su aclaración
    reply = rng.choice([f"la {with_de(pick.room)}", f"la {with_de(pick.room)}, por favor", f"{pick.name}"])
    out.append(Sample(h, [{"role": "user", "content": text}, {"role": "assistant", "content": dumps(g)},
                          {"role": "user", "content": finish(reply, rng)}], gold("ok", [(pick, a)]), "followup_device"))
    return out


def _multi(h, rng):
    ds = rng.sample(list(h.catalog().devices.values()), min(2, len(h.catalog().devices)))
    if len(ds) < 2:
        return []
    acts = [(d, pick_action(d, rng)) for d in ds]
    parts = [rng.choice(VERBS[a]).format(x=full_ref(d, rng)).replace(" por favor", "") for d, a in acts]
    text = f"{parts[0]} y {parts[1]}"
    return [Sample(h, [{"role": "user", "content": finish(text, rng)}], gold("ok", acts), "multi")]


def _all_kind(h, rng):
    cands = [(k, ds) for k, ds in _bare_candidates(h) if len(ds) >= 2]
    if not cands:
        return []
    kind, ds = rng.choice(cands)
    a = rng.choice(list(ds[0].actions))
    x = rng.choice([PLURAL[kind], "todas " + PLURAL[kind]])
    return [Sample(h, [{"role": "user", "content": finish(say(a, x, rng), rng)}], gold("ok", [(d, a) for d in ds]), "all_kind")]


def _scene(h, rng):
    if "todo" not in h.catalog().scenes:
        return []
    return [Sample(h, [{"role": "user", "content": finish(rng.choice(SCENE_SAYS), rng)}], gold("ok", scene="todo"), "scene")]


def _missing_action(h, rng):
    d = rng.choice(list(h.catalog().devices.values()))
    text = finish(full_ref(d, rng), rng)
    g = gold("clarify", question=f"¿Qué desea hacer con {d.name}?")
    out = [Sample(h, [{"role": "user", "content": text}], g, "missing_action")]
    a = pick_action(d, rng)
    out.append(Sample(h, [{"role": "user", "content": text}, {"role": "assistant", "content": dumps(g)},
                          {"role": "user", "content": finish(rng.choice(FOLLOW_ACTION[a]), rng)}], gold("ok", [(d, a)]), "followup_action"))
    return out


def _unknown(h, rng):
    return [Sample(h, [{"role": "user", "content": finish(rng.choice(OFF_TOPIC), rng)}], gold("unknown", question=UNKNOWN_Q), "unknown")]


def _absent_kind(h, rng):
    present = {kind_of(d) for d in h.catalog().devices.values()}
    missing = [k for k in ("DOOR", "WINDOW", "CURTAIN", "LIGHT") if k not in present]
    if not missing:
        return []
    kind = rng.choice(missing)
    a = rng.choice(["abrir", "cerrar"] if kind != "LIGHT" else ["encender", "apagar"])
    noun = NOUNS[kind][0]
    return [Sample(h, [{"role": "user", "content": finish(say(a, noun, rng), rng)}],
                   gold("unknown", question=f"No encuentro {noun} en la casa. ¿Qué desea hacer?"), "absent_kind")]


def _correction(h, rng):
    cands = [(k, ds) for k, ds in _bare_candidates(h) if len(ds) >= 2]
    if not cands:
        return []
    kind, ds = rng.choice(cands)
    d1, d2 = rng.sample(ds, 2)
    a = rng.choice(list(d1.actions))
    first = finish(say(a, full_ref(d1, rng), rng), rng)
    g1 = gold("ok", [(d1, a)])
    fix = rng.choice([f"No. La {with_de(d2.room)}", f"No. La {with_de(d2.room)}, por favor", f"No. {d2.name.capitalize()}"])
    return [Sample(h, [{"role": "user", "content": first}, {"role": "assistant", "content": dumps(g1)},
                       {"role": "user", "content": fix}], gold("ok", [(d2, a)]), "correction")]


INTENTS = [
    (_single_full, 22), (_bare, 22), (_multi, 12), (_all_kind, 6), (_scene, 8),
    (_missing_action, 9), (_unknown, 8), (_absent_kind, 4), (_correction, 4),
]


def validate(s: Sample) -> None:
    """La salida correcta debe sobrevivir a parse_interpretation con el mismo estado y las mismas acciones."""
    cat = s.household.catalog()
    interp = parse_interpretation(dumps(s.gold), cat)
    want = s.gold["status"]
    if interp.status != want:
        raise AssertionError(f"{s.tag}: estado {interp.status} != {want} para {s.history[-1]['content']!r}")
    if want == "ok" and not interp.actions:
        raise AssertionError(f"{s.tag}: orden ok sin acciones: {s.gold}")


def generate_household(h: Household, n: int, rng: random.Random) -> list[Sample]:
    fns, weights = zip(*INTENTS)
    seen: set[str] = set()
    out: list[Sample] = []
    for _ in range(n * 6):
        if len(out) >= n:
            break
        for s in rng.choices(fns, weights)[0](h, rng):
            key = dumps({"h": s.history, "g": s.gold})
            if key in seen:
                continue
            validate(s)
            seen.add(key)
            out.append(s)
    return out


def build(n_train: int, n_val: int, n_test: int, per_house: int, seed: int = 7) -> dict[str, list[Sample]]:
    def house_set(prefix: str, count: int) -> list[Sample]:
        samples: list[Sample] = []
        for i in range(count):
            hid = f"{prefix}{i:03d}"
            rng = random.Random(f"{seed}-{hid}")
            samples += generate_household(random_household(hid, rng), per_house, rng)
        return samples

    return {"train": house_set("tr", n_train), "val": house_set("va", n_val), "test": house_set("te", n_test)}


def write(out_dir: str | Path, splits: dict[str, list[Sample]], seed: int = 7) -> dict[str, int]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    counts = {}
    for name, samples in splits.items():
        order = samples[:]
        rng.shuffle(order)
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for s in order:
                fh.write(json.dumps({"messages": s.messages(), "tag": s.tag}, ensure_ascii=False) + "\n")
        with open(out / f"{name}.eval.jsonl", "w", encoding="utf-8") as fh:
            for s in order:
                fh.write(json.dumps(s.eval_row(), ensure_ascii=False) + "\n")
        counts[name] = len(order)
    return counts
