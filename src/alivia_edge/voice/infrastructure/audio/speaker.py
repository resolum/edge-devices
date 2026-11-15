"""Reproducción hacia el parlante del ESP32: pacing, espera de playback_done y silencio del micrófono (anti-eco)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from alivia_edge.shared.config import SpeakerCfg
from alivia_edge.voice.interfaces.protocol import BYTES_PER_MS, audio_begin, audio_cancel, audio_end, new_utterance_id
from alivia_edge.voice.interfaces.transports.base import Link, LinkClosed

log = logging.getLogger(__name__)


class SpeakerController:
    """Un enunciado a la vez. Mientras `muted` es True el micrófono se ignora (evita oírse a sí mismo)."""

    def __init__(self, cfg: SpeakerCfg, on_unmute: Callable[[], None]) -> None:
        self._cfg = cfg
        self._on_unmute = on_unmute
        self._link: Link | None = None
        self._lock = asyncio.Lock()
        self._current: str | None = None
        self._done = asyncio.Event()
        self.muted = False

    def attach(self, link: Link | None) -> None:
        self._link = link

    def on_playback_done(self, utterance_id: str) -> None:
        if utterance_id == self._current:
            self._done.set()

    async def play(self, pcm: bytes) -> None:
        """Envía el audio y retorna cuando ya sonó (o se asume que sonó) más la cola anti-eco."""
        if not pcm:
            return
        pcm = pcm[: len(pcm) & ~1]
        async with self._lock:
            link = self._link
            if link is None or link.closed:
                raise LinkClosed("sin enlace para reproducir")
            uid = new_utterance_id()
            self._current = uid
            self._done.clear()
            self.muted = True
            try:
                t0 = time.monotonic()
                await link.send_json(audio_begin(uid))
                await self._stream(link, pcm)
                await link.send_json(audio_end(uid))
                duration = len(pcm) / (BYTES_PER_MS * 1000)
                remaining = max(0.0, duration - (time.monotonic() - t0)) + self._cfg.done_margin_s
                try:
                    await asyncio.wait_for(self._done.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    log.warning("playback_done no llegó para %s; se asume reproducido", uid)
                await asyncio.sleep(self._cfg.tail_ms / 1000)
            finally:
                self._current = None
                self.muted = False
                self._on_unmute()

    async def _stream(self, link: Link, pcm: bytes) -> None:
        """Ráfaga inicial de lead_ms y luego ~tiempo real, sin pasar de lead_ms por delante."""
        lead_s = self._cfg.lead_ms / 1000
        chunk = self._cfg.chunk_bytes
        t0 = time.monotonic()
        sent = 0
        while sent < len(pcm):
            ahead = sent / (BYTES_PER_MS * 1000) - (time.monotonic() - t0)
            if ahead > lead_s:
                await asyncio.sleep(ahead - lead_s)
            piece = pcm[sent : sent + chunk]
            await link.send_audio(piece)
            sent += len(piece)

    async def cancel(self) -> None:
        link = self._link
        if link is not None and not link.closed and self._current:
            try:
                await link.send_json(audio_cancel(self._current))
            except LinkClosed:
                pass
        self._done.set()
