"""Interpretación con Qwen en Ollama: salida JSON restringida por esquema y validada contra el catálogo."""
from __future__ import annotations

import json
import logging

from alivia_edge.shared.config import LlmCfg
from alivia_edge.commands.domain.catalog import Catalog
from alivia_edge.commands.domain.errors import LlmError
from alivia_edge.commands.domain.models import Action, Interpretation

log = logging.getLogger(__name__)
MAX_ACTIONS = 8


def build_schema(catalog: Catalog) -> dict:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "clarify", "unknown"]},
            "scene": {"type": "string", "enum": [""] + list(catalog.scenes)},
            "actions": {
                "type": "array",
                "maxItems": MAX_ACTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "device": {"type": "string", "enum": list(catalog.devices)},
                        "action": {"type": "string", "enum": catalog.all_action_names()},
                    },
                    "required": ["device", "action"],
                },
            },
            "question": {"type": "string"},
        },
        "required": ["status", "scene", "actions", "question"],
    }


def build_system_prompt(catalog: Catalog, room: str) -> str:
    lines = [
        "Eres el intérprete de órdenes de Alivia, un asistente de voz de la casa. Recibes la transcripción de "
        "lo que dijo el usuario (puede tener errores de reconocimiento) y respondes ÚNICAMENTE con un JSON.",
        "",
        'Dispositivos (usa exactamente estos "id" y estas acciones):',
    ]
    for d in catalog.devices.values():
        extra = f" | también llamado: {', '.join(d.aliases)}" if d.aliases else ""
        where = f" | habitación: {d.room}" if d.room else ""
        lines.append(f"- id={d.id} | {d.name}{extra}{where} | acciones: {', '.join(d.actions)}")
    if catalog.scenes:
        lines += ["", "Escenas (atajos que ejecutan varias acciones):"]
        for s in catalog.scenes.values():
            steps = ", ".join(f"{a.action} {a.device}" for a in s.actions)
            lines.append(f'- "{s.name}": {s.description} -> {steps}')
    lines += [
        "",
        "Campos del JSON:",
        '- status: "ok" si la orden es clara; "clarify" si dudas entre opciones o falta un dato; "unknown" si no es una orden para estos dispositivos.',
        '- scene: nombre de la escena, o "" si no aplica (con scene puedes dejar actions vacío).',
        "- actions: lista de {device, action}.",
        '- question: solo si status es "clarify" sin acciones, o "unknown": una pregunta corta en español. En otros casos "".',
        "",
        "Reglas:",
        '- Si dudas pero hay una interpretación más probable: status "clarify" y ponla en actions (el sistema se la preguntará al usuario).',
        '- Si no sabes qué dispositivo o qué acción: status "clarify", actions vacío y una pregunta.',
        "- Nunca inventes dispositivos ni acciones.",
        '- Ignora "alivia", "oye alivia" y muletillas.',
        "- Si el usuario corrige o aclara una orden anterior, devuelve la orden final completa ya corregida.",
    ]
    if room:
        lines.append(f"- El usuario está en: {room}. Si no menciona habitación, prefiere los dispositivos de esa habitación.")

    # Ejemplos generados desde el catálogo (siempre coherentes con los ids reales).
    ex: list[tuple[str, dict]] = []
    d1 = next(iter(catalog.devices.values()))
    a1 = next(iter(d1.actions))
    ex.append((f"{a1} {d1.name}", {"status": "ok", "scene": "", "actions": [{"device": d1.id, "action": a1}], "question": ""}))
    multi = next((d for d in catalog.devices.values() if len(d.actions) > 1), None)
    if multi:
        ex.append((multi.name, {"status": "clarify", "scene": "", "actions": [], "question": f"¿Qué desea hacer con {multi.name}?"}))
    for s in catalog.scenes.values():
        if s.examples:
            ex.append((s.examples[0], {"status": "ok", "scene": s.name, "actions": [], "question": ""}))
            break
    ex.append(("¿qué hora es?", {"status": "unknown", "scene": "", "actions": [], "question": "No le entendí. ¿Qué desea hacer?"}))
    lines += ["", "Ejemplos:"]
    for user, out in ex:
        lines.append(f'Usuario: "{user}"')
        lines.append(f"JSON: {json.dumps(out, ensure_ascii=False)}")
    return "\n".join(lines)


def parse_interpretation(raw: str, catalog: Catalog) -> Interpretation:
    """Valida la salida del modelo: nada que no esté en el catálogo llega a ejecutarse."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("el JSON no es un objeto")
    status = data.get("status")
    if status not in ("ok", "clarify", "unknown"):
        status = "unknown"
    question = str(data.get("question") or "").strip()

    actions: list[Action] = []
    scene = catalog.scenes.get(str(data.get("scene") or ""))
    if scene:
        actions.extend(scene.actions)
    for item in data.get("actions") or []:
        if isinstance(item, dict):
            a = Action(str(item.get("device", "")), str(item.get("action", "")))
            if catalog.valid(a):
                actions.append(a)
            else:
                log.warning("LLM propuso acción fuera del catálogo, se descarta: %s", item)
    actions = list(dict.fromkeys(actions))[:MAX_ACTIONS]  # sin duplicados, en orden

    # Mismo dispositivo con acciones contradictorias -> preguntar en vez de adivinar.
    seen: dict[str, str] = {}
    for a in actions:
        if a.device in seen and seen[a.device] != a.action:
            d = catalog.devices[a.device]
            return Interpretation("clarify", [], f"¿Quiere {seen[a.device]} o {a.action} {d.name}?", raw)
        seen[a.device] = a.action

    if status == "unknown":
        actions = []
    if status == "ok" and not actions:
        status = "unknown"
    if status == "clarify" and not actions and not question:
        status = "unknown"
    return Interpretation(status, actions, question, raw)


class OllamaLlm:
    """El catálogo cambia con la tabla de acceso, así que cada llamada recibe el catálogo vigente."""

    def __init__(self, cfg: LlmCfg) -> None:
        self._cfg = cfg
        self._client = None
        self._schemas: dict[object, dict] = {}
        self._prompts: dict[tuple[object, str], str] = {}

    async def start(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(base_url=self._cfg.url, timeout=self._cfg.timeout_s)

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()

    def _system(self, catalog: Catalog, room: str) -> str:
        k = (catalog.key, room)
        if k not in self._prompts:
            if len(self._prompts) > 32:  # las tablas viejas ya no se usan
                self._prompts.clear()
                self._schemas.clear()
            self._prompts[k] = build_system_prompt(catalog, room)
        return self._prompts[k]

    def _schema(self, catalog: Catalog) -> dict:
        if catalog.key not in self._schemas:
            self._schemas[catalog.key] = build_schema(catalog)
        return self._schemas[catalog.key]

    async def _chat(self, messages: list[dict], catalog: Catalog) -> str:
        assert self._client is not None, "OllamaLlm.start() no se ejecutó"
        payload = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": False,
            "format": self._schema(catalog),
            "keep_alive": self._cfg.keep_alive,
            "options": {
                "temperature": self._cfg.temperature,
                "seed": self._cfg.seed,
                "num_ctx": self._cfg.num_ctx,
                "num_predict": self._cfg.num_predict,
            },
        }
        try:
            r = await self._client.post("/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"Ollama: {exc}") from exc

    async def model_digest(self) -> str:
        """Huella (SHA-256) del modelo instalado en Ollama. Falla con un mensaje claro si no está descargado."""
        assert self._client is not None, "OllamaLlm.start() no se ejecutó"
        try:
            r = await self._client.get("/api/tags")
            r.raise_for_status()
            models = r.json().get("models", [])
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"Ollama: {exc}") from exc
        wanted = {self._cfg.model, f"{self._cfg.model}:latest"}
        for m in models:
            if m.get("name") in wanted or m.get("model") in wanted:
                return str(m.get("digest", ""))
        raise LlmError(f"El modelo {self._cfg.model!r} no está en Ollama: ejecuta `ollama pull {self._cfg.model}`")

    async def warmup(self, catalog: Catalog) -> None:
        """Carga el modelo en memoria. Sin actuadores no hay nada que calentar con ese esquema."""
        if not catalog.devices:
            return
        first = next(iter(catalog.devices.values()))
        verb = next(iter(first.actions))
        await self._chat(
            [{"role": "system", "content": self._system(catalog, "")}, {"role": "user", "content": f"{verb} {first.name}"}],
            catalog,
        )

    async def interpret(self, history: list[dict], room: str, catalog: Catalog) -> Interpretation:
        messages = [{"role": "system", "content": self._system(catalog, room)}, *history]
        last_error: Exception | None = None
        for _ in range(2):  # un reintento si el JSON llega roto
            raw = await self._chat(messages, catalog)
            try:
                return parse_interpretation(raw, catalog)
            except (ValueError, TypeError) as exc:
                last_error = exc
                log.warning("Respuesta del LLM no válida (%s): %r", exc, raw[:200])
        raise LlmError(f"respuesta no válida: {last_error}")
