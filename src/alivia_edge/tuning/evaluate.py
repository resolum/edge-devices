"""Evalúa un intérprete (cualquier objeto con interpret(history, room, catalog)) contra casos con respuesta conocida.

Métricas que importan en un asistente que mueve puertas:
- exacto: mismo estado y mismo conjunto de acciones.
- acciones de más: propuso alguna acción que no está en la respuesta correcta (lo peor: se la leería al usuario).
- JSON inválido: la salida no pasó la validación contra el catálogo ni con reintento.
Todas las órdenes pasan igual por confirmación hablada, así que "acciones de más" cuesta una pregunta absurda, no un daño.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from alivia_edge.commands.domain.errors import LlmError
from alivia_edge.tuning.households import Household


@dataclass
class Case:
    household: Household
    history: list[dict]
    gold: dict
    tag: str = ""


@dataclass
class Report:
    n: int = 0
    invalid: int = 0
    status_ok: int = 0
    exact: int = 0
    extra_actions: int = 0
    missed: int = 0  # la respuesta correcta tenía acciones y el modelo no propuso ninguna
    latencies: list[float] = field(default_factory=list)
    by_tag: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # tag -> [exactos, total]
    failures: list[dict] = field(default_factory=list)

    def rate(self, v: int) -> float:
        return v / self.n if self.n else 0.0

    def p(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        xs = sorted(self.latencies)
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    def summary(self) -> dict:
        return {
            "casos": self.n,
            "exacto": round(self.rate(self.exact), 4),
            "estado": round(self.rate(self.status_ok), 4),
            "json_invalido": round(self.rate(self.invalid), 4),
            "acciones_de_mas": round(self.rate(self.extra_actions), 4),
            "acciones_omitidas": round(self.rate(self.missed), 4),
            "latencia_s": {"media": round(statistics.fmean(self.latencies), 3) if self.latencies else 0, "p50": round(self.p(0.5), 3), "p95": round(self.p(0.95), 3)},
            "por_tipo": {t: f"{a}/{b}" for t, (a, b) in sorted(self.by_tag.items())},
        }


def load_cases(path: str | Path) -> list[Case]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cases.append(Case(Household.from_meta(r["household"]), r["history"], r["gold"], r.get("tag", "")))
    return cases


def from_samples(samples) -> list[Case]:
    return [Case(s.household, s.history, s.gold, s.tag) for s in samples]


def _pairs(actions) -> set[tuple[str, str]]:
    return {(a.device, a.action) for a in actions}


async def evaluate(cases: list[Case], interpret, *, keep_failures: int = 25) -> Report:
    from alivia_edge.commands.infrastructure.llm import parse_interpretation

    rep = Report()
    for c in cases:
        cat = c.household.catalog()
        want = parse_interpretation(json.dumps(c.gold, ensure_ascii=False), cat)  # la respuesta correcta, ya expandida
        t0 = time.monotonic()
        try:
            got = await interpret(c.history, c.household.mic_room, cat)
        except (LlmError, ValueError):
            rep.n += 1
            rep.invalid += 1
            rep.by_tag[c.tag][1] += 1
            continue
        rep.latencies.append(time.monotonic() - t0)
        rep.n += 1
        w, g = _pairs(want.actions), _pairs(got.actions)
        exact = got.status == want.status and g == w
        rep.status_ok += got.status == want.status
        rep.exact += exact
        rep.extra_actions += bool(g - w)
        rep.missed += bool(w and not g)
        rep.by_tag[c.tag][0] += exact
        rep.by_tag[c.tag][1] += 1
        if not exact and len(rep.failures) < keep_failures:
            rep.failures.append({"tag": c.tag, "dijo": c.history[-1]["content"], "cuarto": c.household.mic_room,
                                 "esperado": [want.status, sorted(w)], "obtenido": [got.status, sorted(g), got.question]})
    return rep


def run(cases: list[Case], interpret) -> Report:
    return asyncio.run(evaluate(cases, interpret))
