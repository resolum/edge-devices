"""Síntesis con Piper (voz es_MX) -> PCM 16 kHz mono, con caché de frases."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections import OrderedDict
from concurrent.futures import Executor
from pathlib import Path

import numpy as np

from alivia_edge.shared.config import TtsCfg
from alivia_edge.voice.infrastructure.audio.pcm import resample_int16_to_16k

log = logging.getLogger(__name__)
CACHE_SIZE = 96


class PiperTts:
    def __init__(self, cfg: TtsCfg, voice_path: str, executor: Executor) -> None:
        self._cfg = cfg
        self._voice_path = voice_path
        self._executor = executor
        self._voice = None
        self._rate = 22050
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    def load(self) -> None:
        if self._cfg.backend == "exe":
            cfg_path = Path(self._voice_path + ".json")
            self._rate = int(json.loads(cfg_path.read_text(encoding="utf-8"))["audio"]["sample_rate"])
            return
        from piper import PiperVoice

        self._voice = PiperVoice.load(self._voice_path)

    # --- bloqueante (se ejecuta en el executor) ---
    def _synth_raw(self, text: str) -> tuple[np.ndarray, int]:
        if self._cfg.backend == "exe":
            proc = subprocess.run(
                [self._cfg.piper_exe, "--model", self._voice_path, "--output_raw", "--length_scale", str(self._cfg.length_scale)],
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
            )
            return np.frombuffer(proc.stdout, dtype="<i2"), self._rate
        voice = self._voice
        assert voice is not None, "PiperTts.load() no se ejecutó"
        if hasattr(voice, "synthesize_stream_raw"):  # piper-tts 1.2.x
            raw = b"".join(voice.synthesize_stream_raw(text, length_scale=self._cfg.length_scale))
            return np.frombuffer(raw, dtype="<i2"), int(voice.config.sample_rate)
        from piper import SynthesisConfig  # piper-tts >= 1.3

        chunks = list(voice.synthesize(text, syn_config=SynthesisConfig(length_scale=self._cfg.length_scale)))
        if not chunks:
            return np.zeros(0, dtype="<i2"), 22050
        raw = b"".join(c.audio_int16_bytes for c in chunks)
        return np.frombuffer(raw, dtype="<i2"), int(chunks[0].sample_rate)

    def _synth_16k(self, text: str) -> bytes:
        samples, rate = self._synth_raw(text)
        return resample_int16_to_16k(samples, rate, self._cfg.gain)

    async def synth(self, text: str) -> bytes:
        hit = self._cache.get(text)
        if hit is not None:
            self._cache.move_to_end(text)
            return hit
        loop = asyncio.get_running_loop()
        pcm = await loop.run_in_executor(self._executor, self._synth_16k, text)
        self._cache[text] = pcm
        if len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return pcm

    async def prewarm(self, phrases: list[str]) -> None:
        for p in phrases:
            await self.synth(p)
