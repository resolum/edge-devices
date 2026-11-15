"""Transcripción con faster-whisper (large-v3-turbo por defecto)."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor

import numpy as np

from alivia_edge.shared.config import SttCfg
from alivia_edge.shared.domain.text_es import normalize
from alivia_edge.voice.infrastructure.audio.pcm import normalize_for_stt, pcm_to_float

log = logging.getLogger(__name__)

# Alucinaciones típicas de Whisper con silencio o ruido.
_HALLUCINATIONS = (
    "gracias por ver", "subtitulos", "amara org", "suscribete", "no olvides suscribirte",
    "hasta la proxima", "musica",
)
MIN_AUDIO_MS = 150


def echoes_prompt(norm_text: str, norm_prompt: str) -> bool:
    """Whisper a veces devuelve el prompt cuando solo hay ruido. Ojo: la pista de vocabulario está hecha de órdenes
    válidas, así que una orden corta que aparezca dentro de ella NO es eco. Solo se descarta si trae el encabezado
    ("órdenes para la casa", que nadie dice) o copia de corrido la mayor parte del prompt.
    """
    if not norm_text or not norm_prompt:
        return False
    if "ordenes para la casa" in norm_text:
        return True
    tw, pw = norm_text.split(), norm_prompt.split()
    return len(tw) >= 8 and len(tw) >= 0.6 * len(pw) and norm_text in norm_prompt


class WhisperStt:
    def __init__(self, cfg: SttCfg, executor: Executor) -> None:
        self._cfg = cfg
        self._executor = executor
        self._model = None
        self.runtime: tuple[str, str] = (cfg.device, cfg.compute_type)

    def load(self) -> None:
        from faster_whisper import WhisperModel

        from alivia_edge.shared.infrastructure import hardware

        hw = hardware.detect()
        device, compute, why = hardware.choose_stt_runtime(self._cfg.device, self._cfg.compute_type, hw, self._cfg.vram_budget_mb)
        log.info("Whisper %s en %s/%s (%s); GPU=%s VRAM=%d MB, %d núcleos", self._cfg.model, device, compute, why, hw.cuda, hw.vram_mb, hw.cpu_cores)
        self.runtime = (device, compute)
        kwargs = {"device": device, "compute_type": compute, "cpu_threads": self._cfg.cpu_threads}
        if self._cfg.model_dir:
            kwargs["download_root"] = self._cfg.model_dir
        if self._cfg.revision:
            kwargs["revision"] = self._cfg.revision
        self._model = WhisperModel(self._cfg.model, **kwargs)

    def _run(self, audio: np.ndarray, prompt: str | None) -> str:
        assert self._model is not None, "WhisperStt.load() no se ejecutó"
        segments, _info = self._model.transcribe(
            audio,
            language=self._cfg.language,
            beam_size=self._cfg.beam_size,
            temperature=0.0,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=prompt or self._cfg.initial_prompt,
        )
        parts = [s.text.strip() for s in segments if not (s.no_speech_prob > 0.7 and s.avg_logprob < -1.0)]
        text = " ".join(p for p in parts if p).strip()
        norm = normalize(text)
        if any(h in norm for h in _HALLUCINATIONS) and len(norm.split()) <= 8:
            log.debug("Whisper: alucinación descartada: %r", text)
            return ""
        if echoes_prompt(norm, normalize(prompt or self._cfg.initial_prompt)):
            log.debug("Whisper: eco del prompt descartado: %r", text)
            return ""
        return text

    async def transcribe(self, pcm: bytes, *, prompt: str | None = None) -> str:
        if len(pcm) < MIN_AUDIO_MS * 32:
            return ""
        audio = normalize_for_stt(pcm_to_float(pcm))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._run, audio, prompt)
