"""Sesión de un nodo (ESP32 con micrófono + parlante): enruta el audio, mantiene el estado y lanza el diálogo."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from enum import Enum

from alivia_edge.voice.infrastructure.audio.speaker import SpeakerController
from alivia_edge.voice.infrastructure.audio.vad import Recorder
from alivia_edge.voice.application.dialog import Dialog
from alivia_edge.voice.domain.ports import MicStalled
from alivia_edge.shared.domain.text_es import is_meaningful, strip_wake
from alivia_edge.voice.interfaces.protocol import BYTES_PER_MS
from alivia_edge.app import Services
from alivia_edge.voice.interfaces.transports.base import Link, LinkClosed

log = logging.getLogger(__name__)

BAD_MIC_STATES = {"SIN_DATOS", "SOLO_CEROS", "VALOR_FIJO", "SATURADO"}


class State(Enum):
    IDLE = "idle"  # escuchando la palabra de activación
    WAKE_CAPTURE = "wake_capture"  # capturando lo que se dijo junto a la palabra
    THINKING = "thinking"  # transcribiendo / interpretando / ejecutando
    SPEAKING = "speaking"
    LISTENING = "listening"  # grabando una respuesta u orden


class DeviceSession:
    def __init__(self, mac: str, label: str, svc: Services) -> None:
        self.mac = mac  # identidad (la de la tabla de acceso del backend)
        self.device_id = label  # etiqueta del sketch, solo para los logs
        self.svc = svc
        self.cfg = svc.cfg
        self.link: Link | None = None
        self.state = State.IDLE

        self._wake = svc.new_wake()
        self._yesno = svc.new_yesno()
        self._recorder = Recorder(svc.new_vad(), self.cfg.vad, self.cfg.audio.mic_stall_s)
        self.speaker = SpeakerController(self.cfg.speaker, self._on_unmute)

        self._preroll: deque[bytes] = deque()
        self._preroll_len = 0
        self._preroll_max = int(self.cfg.audio.preroll_ms * BYTES_PER_MS)
        self._mic_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._accepting = False
        self._conv: asyncio.Task | None = None
        self._last_mic_status = ""
        self.mic_frames = 0

    # ------------------------------------------------------------------ enlace
    def accept(self, link: Link) -> bool:
        """Registra el enlace. False si ya hay uno vivo con mayor prioridad (WiFi gana a Bluetooth)."""
        cur = self.link
        if cur is not None and not cur.closed:
            if link.priority < cur.priority:
                return False
            log.info("[%s] enlace %s reemplazado por %s", self.device_id, cur.kind, link.kind)
            asyncio.get_running_loop().create_task(cur.close("reemplazado"))
            self._cancel_conversation()
        self.link = link
        self.speaker.attach(link)
        self._reset_idle()
        return True

    async def run_link(self, link: Link) -> None:
        try:
            while True:
                kind, value = await link.recv()
                if kind == "audio":
                    self.on_mic_audio(value)
                else:
                    self._on_json(value)
        except LinkClosed as exc:
            log.info("[%s] enlace %s cerrado: %s", self.device_id, link.kind, exc)
        finally:
            if self.link is link:
                self.link = None
                self.speaker.attach(None)
                self._cancel_conversation()
                self._reset_idle()

    def _on_json(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "playback_done":
            self.speaker.on_playback_done(str(msg.get("utteranceId", "")))
        elif mtype == "audio_level":
            status = str(msg.get("status", ""))
            if status != self._last_mic_status:
                (log.warning if status in BAD_MIC_STATES else log.info)(
                    "[%s] micrófono: %s (rms %.1f dBFS)", self.device_id, status, float(msg.get("rmsDbfs", 0) or 0)
                )
                self._last_mic_status = status
        else:
            log.debug("[%s] mensaje %s", self.device_id, mtype)

    # ------------------------------------------------------------------ micrófono
    def on_mic_audio(self, chunk: bytes) -> None:
        chunk = chunk[: len(chunk) & ~1]
        if not chunk:
            return
        self.mic_frames += 1
        if self.speaker.muted:  # anti-eco: no oírse a sí mismo
            return
        if self._conv is not None:
            if self._accepting:
                self._mic_q.put_nowait(chunk)
            return
        self._preroll.append(chunk)
        self._preroll_len += len(chunk)
        while self._preroll_len > self._preroll_max and self._preroll:
            self._preroll_len -= len(self._preroll.popleft())
        if self._wake.feed(chunk):
            preroll = b"".join(self._preroll)
            log.info("[%s] palabra de activación detectada", self.device_id)
            self._accepting = True  # desde ya: ningún cuadro entre el disparo y el inicio de la tarea se pierde
            self._conv = asyncio.get_running_loop().create_task(self._conversation(preroll))

    def _flush_mic(self) -> None:
        while not self._mic_q.empty():
            self._mic_q.get_nowait()

    def _reset_idle(self) -> None:
        self.state = State.IDLE
        self._accepting = False
        self._flush_mic()
        self._preroll.clear()
        self._preroll_len = 0
        self._wake.reset()

    def _on_unmute(self) -> None:
        self._flush_mic()
        self._preroll.clear()
        self._preroll_len = 0
        if self._conv is None:
            self._wake.reset()

    def _cancel_conversation(self) -> None:
        if self._conv is not None and not self._conv.done():
            self._conv.cancel()

    async def _mic_get(self) -> bytes:
        try:
            return await asyncio.wait_for(self._mic_q.get(), timeout=self.cfg.audio.mic_stall_s)
        except asyncio.TimeoutError:
            raise MicStalled("el nodo dejó de enviar audio") from None

    # ------------------------------------------------------------------ DialogIO
    async def say(self, text: str) -> None:
        self.state = State.SPEAKING
        log.info("[%s] dice: %s", self.device_id, text)
        pcm = await self.svc.tts.synth(text)
        await self.speaker.play(pcm)
        self.state = State.THINKING

    async def record(self, *, start_timeout_s: float, max_s: float, answer: bool) -> bytes | None:
        self.state = State.LISTENING
        self._accepting = True
        try:
            rec = await self._recorder.record(
                self._mic_get,
                start_timeout_s=start_timeout_s,
                max_s=max_s,
                end_silence_ms=self.cfg.vad.answer_end_silence_ms if answer else self.cfg.vad.end_silence_ms,
            )
        finally:
            self._accepting = False
            self.state = State.THINKING
        return rec.pcm if rec else None

    async def transcribe(self, pcm: bytes) -> str:
        t0 = time.monotonic()
        prompt = self.svc.catalogs.current().speech_hint() if self.svc.cfg.stt.speech_hint else None
        text = await self.svc.stt.transcribe(pcm, prompt=prompt or None)
        log.info("[%s] Whisper (%.2fs): %r", self.device_id, time.monotonic() - t0, text)
        return text

    async def quick_yesno(self, pcm: bytes) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.svc.executor, self._yesno.recognize, pcm)

    # ------------------------------------------------------------------ conversación
    async def _conversation(self, preroll: bytes) -> None:
        cfg = self.cfg
        t0 = time.monotonic()
        try:
            # 1) Capturar lo que se dijo con la palabra de activación (hasta que termine la frase).
            self.state = State.WAKE_CAPTURE
            self._accepting = True
            try:
                rec = await self._recorder.record(
                    self._mic_get,
                    prefix=preroll,
                    start_timeout_s=cfg.wake.capture_start_timeout_s,
                    max_s=cfg.wake.capture_max_s,
                    end_silence_ms=cfg.wake.capture_end_silence_ms,
                )
            finally:
                self._accepting = False
            wr = self._wake.finish()
            if rec is None:
                log.info("[%s] falso disparo (sin voz)", self.device_id)
                return
            if wr.conf is not None and cfg.wake.min_conf > 0:
                log.info("[%s] confianza de la palabra de activación: %.2f", self.device_id, wr.conf)
                if wr.conf < cfg.wake.min_conf:
                    log.info("[%s] falso disparo (confianza baja)", self.device_id)
                    return

            # 2) ¿Dijo la orden de corrido ("Alivia, cierra la puerta") o solo la palabra?
            self.state = State.THINKING
            initial: str | None = None
            if rec.post_prefix_speech_ms >= cfg.wake.command_min_speech_ms:
                text = strip_wake(await self.transcribe(rec.pcm), cfg.wake.strip_names)
                if is_meaningful(text):
                    initial = text

            # Habitación del micrófono según la tabla de HOY: "abre la ventana" se entiende en ese cuarto.
            room = self.svc.catalogs.room_label(self.svc.gate.room_key(self.mac))
            dialog = Dialog(
                cfg=cfg.dialog,
                catalog=self.svc.catalogs.current(),
                llm=self.svc.llm,
                bus=self.svc.dispatcher,
                io=self,
                node_id=self.mac,
                room=room,
                strip_names=cfg.wake.strip_names,
            )
            outcome = await dialog.run(initial)
            log.info("[%s] conversación terminada: %s (%.1fs)", self.device_id, outcome.value, time.monotonic() - t0)
        except asyncio.CancelledError:
            raise
        except (LinkClosed, MicStalled) as exc:
            log.warning("[%s] conversación interrumpida: %s", self.device_id, exc)
        except Exception:  # noqa: BLE001
            log.exception("[%s] error inesperado en la conversación", self.device_id)
        finally:
            self._accepting = False
            self.state = State.IDLE
            self._flush_mic()
            self._preroll.clear()
            self._preroll_len = 0
            self._wake.reset()
            self._conv = None
