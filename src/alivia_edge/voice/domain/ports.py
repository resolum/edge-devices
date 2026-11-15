"""Contratos (Protocol) entre el pipeline y los modelos. Permiten sustituir cada pieza por un doble en pruebas."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


class MicStalled(Exception):
    """El nodo dejó de enviar audio mientras se esperaba al usuario."""


@dataclass
class WakeResult:
    text: str
    conf: float | None  # confianza de la palabra clave; None si no se pudo medir


class WakeDetector(Protocol):
    def feed(self, pcm: bytes) -> bool:
        """Alimenta audio 16 kHz mono int16; True en cuanto detecta la palabra de activación."""

    def finish(self) -> WakeResult:
        """Cierra el reconocimiento del disparo (y reinicia el detector)."""

    def reset(self) -> None: ...


class YesNoRecognizer(Protocol):
    def recognize(self, pcm: bytes) -> str | None:
        """'yes' | 'no' | None (no concluyente) con gramática cerrada."""


class Vad(Protocol):
    def prob(self, window: np.ndarray) -> float:
        """Probabilidad de voz para una ventana de 512 muestras float32 en [-1, 1]."""

    def reset(self) -> None: ...


class Stt(Protocol):
    async def transcribe(self, pcm: bytes, *, prompt: str | None = None) -> str: ...


class Tts(Protocol):
    async def synth(self, text: str) -> bytes:
        """PCM int16 mono 16 kHz listo para enviar al parlante."""
