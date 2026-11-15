"""VAD Silero (ONNX directo con onnxruntime, sin torch), endpointing y grabadora de frases."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from alivia_edge.shared.config import VadCfg
from alivia_edge.voice.domain.ports import MicStalled, Vad
from alivia_edge.voice.interfaces.protocol import BYTES_PER_MS

log = logging.getLogger(__name__)

WIN = 512  # muestras por ventana a 16 kHz (32 ms), lo que exige Silero v5
WIN_MS = 32
CONTEXT = 64


class SileroVad:
    """Silero VAD v5: input = 64 muestras de contexto + 512 de la ventana actual."""

    def __init__(self, model_path: str) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._sess = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(16000, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def prob(self, window: np.ndarray) -> float:
        x = np.concatenate([self._context, window.reshape(1, WIN).astype(np.float32)], axis=1)
        out, self._state = self._sess.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -CONTEXT:]
        return float(out[0, 0])


class EnergyVad:
    """Respaldo muy simple por energía si no está el modelo Silero (solo para no quedar sordo)."""

    def __init__(self, threshold: float = 0.015) -> None:
        self._th = threshold

    def reset(self) -> None:
        pass

    def prob(self, window: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(window**2))) > self._th else 0.0


class Endpointer:
    """Máquina de estados sobre las probabilidades del VAD (histéresis + mínimo de voz + silencio final)."""

    def __init__(self, vad: Vad, cfg: VadCfg, end_silence_ms: int) -> None:
        self._vad = vad
        self._cfg = cfg
        self._end_silence_ms = end_silence_ms
        self._rem = b""
        self._run_voiced_ms = 0
        self.in_speech = False
        self.speech_started = False
        self.speech_ms = 0
        self.silence_ms = 0  # silencio acumulado tras la última voz

    @property
    def ended(self) -> bool:
        return self.speech_started and self.silence_ms >= self._end_silence_ms

    def feed(self, pcm: bytes) -> None:
        data = self._rem + pcm
        step = WIN * 2
        n = len(data) // step
        for k in range(n):
            window = np.frombuffer(data[k * step : (k + 1) * step], dtype="<i2").astype(np.float32) / 32768.0
            p = self._vad.prob(window)
            if p >= self._cfg.threshold_on:
                self.in_speech = True
                self._run_voiced_ms += WIN_MS
                self.speech_ms += WIN_MS
                self.silence_ms = 0
                if self._run_voiced_ms >= self._cfg.min_speech_ms:
                    self.speech_started = True
            elif p < self._cfg.threshold_off:
                self.in_speech = False
                self._run_voiced_ms = 0
                if self.speech_started:
                    self.silence_ms += WIN_MS
            # entre ambos umbrales se conserva el estado (histéresis)
        self._rem = data[n * step :]


@dataclass
class Recording:
    pcm: bytes
    speech_ms: int  # voz total detectada (incluye el prefijo)
    post_prefix_speech_ms: int  # voz detectada después del prefijo
    ended_by: str  # "silence" | "max"


class Recorder:
    """Graba una frase usando el VAD. El audio llega por `get` (cola alimentada por el router del micrófono)."""

    def __init__(self, vad: Vad, cfg: VadCfg, stall_s: float = 3.0) -> None:
        self._vad = vad
        self._cfg = cfg
        self._stall_s = stall_s

    async def record(
        self,
        get: Callable[[], Awaitable[bytes]],
        *,
        prefix: bytes = b"",
        start_timeout_s: float,
        max_s: float,
        end_silence_ms: int,
    ) -> Recording | None:
        """None si no se empezó a hablar dentro de start_timeout_s."""
        self._vad.reset()
        ep = Endpointer(self._vad, self._cfg, end_silence_ms)
        audio = bytearray()
        if prefix:
            audio += prefix
            ep.feed(prefix)
        speech_at_prefix = ep.speech_ms
        listened_ms = 0.0
        while True:
            chunk = await get()
            audio += chunk
            ep.feed(chunk)
            listened_ms += len(chunk) / BYTES_PER_MS
            if ep.ended:
                return Recording(bytes(audio), ep.speech_ms, ep.speech_ms - speech_at_prefix, "silence")
            if not ep.speech_started and listened_ms >= start_timeout_s * 1000:
                return None
            if listened_ms >= max_s * 1000:
                if not ep.speech_started:
                    return None
                return Recording(bytes(audio), ep.speech_ms, ep.speech_ms - speech_at_prefix, "max")


def make_vad(cfg: VadCfg, model_path: str) -> Vad:
    try:
        return SileroVad(model_path)
    except Exception as exc:  # noqa: BLE001
        log.error("No se pudo cargar Silero VAD (%s): %s. Uso VAD por energía (peor). Ejecuta scripts/fetch_models.py", model_path, exc)
        return EnergyVad()


__all__ = ["SileroVad", "EnergyVad", "Endpointer", "Recorder", "Recording", "make_vad", "MicStalled"]
