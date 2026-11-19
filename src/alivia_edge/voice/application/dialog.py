"""Diálogo de una orden: dictado -> interpretación -> (aclaración) -> confirmación -> ejecución -> resultado.

Reglas:
  * Nada se envía al bus sin un "sí" a una pregunta construida EN CÓDIGO a partir de las acciones ya
    validadas contra el catálogo (no se confía en el texto que escriba el LLM para ejecutar).
  * Si el LLM duda y propone una acción, se pregunta "¿Quiere que ...?"; el "sí" cuenta como confirmación.
  * Si falta información, se hace la pregunta aclaratoria del LLM y se vuelve a interpretar (máx. N rondas).
"""
from __future__ import annotations

import logging
import random
import time
from enum import Enum
from typing import Callable, Protocol

from alivia_edge.alerts.domain import is_help_request
from alivia_edge.shared.config import DialogCfg
from alivia_edge.shared.domain.text_es import Answer, classify_answer, is_meaningful, strip_wake
from alivia_edge.commands.domain.catalog import Catalog
from alivia_edge.commands.domain.errors import LlmError
from alivia_edge.commands.domain.models import Action

log = logging.getLogger(__name__)

P_NO_SPEECH = "No escuché nada. Llámeme cuando quiera."
P_NOT_UNDERSTOOD = "No logré entenderle. Inténtelo de nuevo cuando quiera."
P_ASK_AGAIN = "No le entendí bien. ¿Qué desea hacer?"
P_CANCELLED = "De acuerdo, no hago nada."
P_NO_ANSWER = "No escuché respuesta, cancelo la orden."
P_REPEAT_YN = "Perdón, no le entendí. ¿Lo hago? Diga sí o no."
P_WHAT_THEN = "¿Qué era lo que quería?"
P_BUS_DOWN = "En este momento no puedo comunicarme con los actuadores."
P_NO_DEVICES = "No tengo dispositivos disponibles para controlar en este momento."
P_INTERNAL = "Tuve un problema para interpretar la orden. Inténtelo de nuevo."
P_HELP_SENT = "Ya estoy avisando a su familia. Quédese tranquilo, la ayuda va en camino."

STATIC_PHRASES = [P_NO_SPEECH, P_NOT_UNDERSTOOD, P_ASK_AGAIN, P_CANCELLED, P_NO_ANSWER, P_REPEAT_YN, P_WHAT_THEN, P_BUS_DOWN, P_NO_DEVICES, P_INTERNAL, P_HELP_SENT]


class Outcome(Enum):
    EXECUTED = "executed"
    PARTIAL = "partial"  # alguna acción falló o no respondió
    CANCELLED = "cancelled"
    SILENCE = "silence"
    NOT_UNDERSTOOD = "not_understood"
    ERROR = "error"
    HELP_REQUESTED = "help_requested"  # pidió ayuda: se avisó al familiar


class DialogIO(Protocol):
    async def say(self, text: str) -> None: ...

    async def record(self, *, start_timeout_s: float, max_s: float, answer: bool) -> bytes | None:
        """Graba una frase con VAD; None si no hubo voz a tiempo."""

    async def transcribe(self, pcm: bytes) -> str: ...

    async def quick_yesno(self, pcm: bytes) -> str | None: ...


class Dialog:
    def __init__(
        self,
        *,
        cfg: DialogCfg,
        catalog: Catalog,
        llm,
        bus,  # CommandDispatcher: connected + execute(); vuelve a autorizar cada acción
        io: DialogIO,
        node_id: str,
        room: str,
        strip_names: list[str],
        on_help: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg, self.catalog, self.llm, self.bus, self.io = cfg, catalog, llm, bus, io
        self.node_id, self.room, self.strip_names = node_id, room, strip_names
        self.on_help = on_help  # avisa al familiar (cola hacia el backend); None si no hay backend

    # --- utilidades -------------------------------------------------------------------------
    async def _listen_text(self) -> str | None:
        pcm = await self.io.record(
            start_timeout_s=self.cfg.order_start_timeout_s, max_s=self.cfg.order_window_s, answer=False
        )
        if pcm is None:
            return None
        text = strip_wake(await self.io.transcribe(pcm), self.strip_names)
        return text if is_meaningful(text) else None

    async def _ask(self, question: str) -> Answer:
        """Pregunta y devuelve la respuesta clasificada. Vosk (rápido) primero; Whisper si no es concluyente."""
        await self.io.say(question)
        for attempt in range(2):
            pcm = await self.io.record(
                start_timeout_s=self.cfg.answer_start_timeout_s, max_s=self.cfg.answer_max_s, answer=True
            )
            if pcm is None:
                return Answer("none")
            quick = await self.io.quick_yesno(pcm)
            if quick == "yes":
                return Answer("yes", "sí")
            if quick == "no":
                return Answer("no", "no")
            ans = classify_answer(await self.io.transcribe(pcm))
            if ans.kind != "none":
                return ans
            if attempt == 0:
                await self.io.say(P_REPEAT_YN)
        return Answer("none")

    async def _execute(self, actions: list[Action]) -> Outcome:
        log.info("[%s] ejecutando: %s", self.node_id, [(a.device, a.action) for a in actions])
        t0 = time.monotonic()
        results = await self.bus.execute(actions, node=self.node_id, timeout_s=self.cfg.exec_timeout_s)
        log.info("[%s] resultado en %.2fs: %s", self.node_id, time.monotonic() - t0, [(r.action.device, r.status) for r in results])
        await self.io.say(self.catalog.summarize(results))
        return Outcome.EXECUTED if all(r.status == "ok" for r in results) else Outcome.PARTIAL

    # --- flujo principal -----------------------------------------------------------------------
    async def _help(self, text: str) -> Outcome:
        """Un pedido de ayuda no pasa por el LLM ni pide confirmación: se avisa al familiar de inmediato."""
        if self.on_help is not None:
            self.on_help(text)
        await self.io.say(P_HELP_SENT)
        return Outcome.HELP_REQUESTED

    async def run(self, initial_text: str | None) -> Outcome:
        say = self.io.say
        if initial_text and self.on_help is not None and is_help_request(initial_text):  # "Alivia, ayuda"
            return await self._help(initial_text)
        if not self.catalog.devices and self.on_help is None:  # sin tabla, o sin actuadores activos: nada que ofrecer
            await say(P_NO_DEVICES)
            return Outcome.ERROR
        if initial_text:  # frase completa dicha de corrido ("Alivia, cierra la puerta")
            text: str | None = initial_text
        else:
            await say(random.choice(self.cfg.greetings))
            text = await self._listen_text()
            if text is None:
                await say(P_NO_SPEECH)
                return Outcome.SILENCE
            if self.on_help is not None and is_help_request(text):
                return await self._help(text)
        if not self.catalog.devices:  # pudo pedir ayuda igual; para mover algo hace falta la tabla
            await say(P_NO_DEVICES)
            return Outcome.ERROR

        history: list[dict] = []
        rounds = 0
        while True:
            history.append({"role": "user", "content": text})
            try:
                interp = await self.llm.interpret(history, self.room, self.catalog)
            except LlmError as exc:
                log.error("[%s] LLM: %s", self.node_id, exc)
                await say(P_INTERNAL)
                return Outcome.ERROR
            history.append({"role": "assistant", "content": interp.raw})
            log.info("[%s] interpretación: %s %s %r", self.node_id, interp.status, [(a.device, a.action) for a in interp.actions], interp.question)

            if interp.status in ("ok", "clarify") and interp.actions:
                if not self.bus.connected:
                    await say(P_BUS_DOWN)
                    return Outcome.ERROR
                what = self.catalog.describe(interp.actions)
                if interp.status == "ok":
                    ans = await self._ask(f"¿Quiere que {what}?")
                else:
                    ans = await self._ask(f"No estoy seguro de haber entendido. ¿Quiere que {what}?")
                if ans.kind == "yes":
                    return await self._execute(interp.actions)
                if ans.kind == "none":
                    await say(P_NO_ANSWER)
                    return Outcome.SILENCE
                if ans.kind == "no" and not ans.has_content:
                    if interp.status == "ok":  # rechazó lo que se le leyó: cancelar
                        await say(P_CANCELLED)
                        return Outcome.CANCELLED
                    await say(P_WHAT_THEN)  # rechazó la suposición: ¿qué quería entonces?
                    rounds += 1
                    if rounds > self.cfg.max_clarifications:
                        await say(P_NOT_UNDERSTOOD)
                        return Outcome.NOT_UNDERSTOOD
                    heard = await self._listen_text()
                    if heard is None:
                        await say(P_NO_SPEECH)
                        return Outcome.SILENCE
                    text = f"No, eso no. {heard}"
                    continue
                # "no, la ventana" / otra frase: se reinterpreta con el contexto previo
                rounds += 1
                if rounds > self.cfg.max_clarifications:
                    await say(P_NOT_UNDERSTOOD)
                    return Outcome.NOT_UNDERSTOOD
                text = ("No. " if ans.kind == "no" else "") + ans.text
                continue

            # clarify sin acción o unknown: pregunta abierta
            rounds += 1
            if rounds > self.cfg.max_clarifications:
                await say(P_NOT_UNDERSTOOD)
                return Outcome.NOT_UNDERSTOOD
            await say(interp.question or P_ASK_AGAIN)
            heard = await self._listen_text()
            if heard is None:
                await say(P_NO_SPEECH)
                return Outcome.SILENCE
            text = heard
