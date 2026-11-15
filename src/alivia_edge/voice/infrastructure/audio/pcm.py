from __future__ import annotations

from math import gcd

import numpy as np

from alivia_edge.voice.interfaces.protocol import BYTES_PER_MS, SAMPLE_RATE


def ms_to_bytes(ms: float) -> int:
    return int(ms * BYTES_PER_MS) & ~1


def bytes_to_ms(n: int) -> float:
    return n / BYTES_PER_MS


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """int16 little-endian -> float32 en [-1, 1]."""
    return np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def resample_int16_to_16k(samples: np.ndarray, src_rate: int, gain: float = 1.0) -> bytes:
    """Piper suele dar 22050 Hz; el parlante solo acepta 16000 Hz."""
    x = samples.astype(np.float32) / 32768.0
    if src_rate != SAMPLE_RATE:
        from scipy.signal import resample_poly

        g = gcd(SAMPLE_RATE, src_rate)
        x = resample_poly(x, SAMPLE_RATE // g, src_rate // g).astype(np.float32)
    if gain != 1.0:
        x = x * gain
    return float_to_pcm(x)


def normalize_for_stt(audio: np.ndarray, target_peak: float = 0.5, max_gain: float = 8.0) -> np.ndarray:
    """Sube el volumen de voces bajas (sin amplificar el ruido de fondo más de max_gain)."""
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-4:
        return audio
    gain = min(target_peak / peak, max_gain)
    return audio * gain if gain > 1.0 else audio
