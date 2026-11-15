"""Detección de hardware y elección de cómo correr Whisper para que quepa junto al LLM.

En una GPU de 6 GB, Whisper large-v3-turbo en float16 ocupa unos 2,5 GB y Qwen 3B Q4 otros 2,5 GB más contexto: queda
al límite. En int8_float16 Whisper baja a ~1,5 GB con una pérdida de precisión pequeña. Se decide por VRAM disponible.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hardware:
    cuda: bool
    vram_mb: int  # de la GPU 0 (0 si no hay)
    cpu_cores: int


def _nvidia_vram_mb() -> int:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()
        return int(out[0].strip()) if out else 0
    except Exception:  # noqa: BLE001
        return 0


def detect() -> Hardware:
    import os

    cores = os.cpu_count() or 1
    vram = _nvidia_vram_mb()
    cuda = vram > 0
    if not cuda:
        try:  # ctranslate2 sabe si hay CUDA usable aunque nvidia-smi no esté en el PATH
            import ctranslate2

            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            cuda = False
    return Hardware(cuda=cuda, vram_mb=vram, cpu_cores=cores)


def choose_stt_runtime(device: str, compute_type: str, hw: Hardware, vram_budget_mb: int = 0) -> tuple[str, str, str]:
    """Devuelve (device, compute_type, motivo). Lo que el usuario fijó explícitamente no se toca."""
    dev = device
    if device == "auto":
        dev = "cuda" if hw.cuda else "cpu"
    if compute_type != "auto":
        return dev, compute_type, "fijado en la configuración"
    if dev == "cpu":
        return dev, "int8", "CPU: int8 es lo más rápido"
    budget = vram_budget_mb or hw.vram_mb
    if budget and budget < 8000:
        return dev, "int8_float16", f"GPU de {budget} MB: int8_float16 deja ~1 GB libre para el LLM"
    return dev, "float16", "GPU con VRAM de sobra: float16"


def expected_stt_vram_mb(compute_type: str, model: str) -> int:
    """Cifras aproximadas del benchmark de faster-whisper (large-v3-turbo); otras escalas por tamaño."""
    if "turbo" in model or model.startswith("large"):
        return 2600 if compute_type == "float16" else 1600
    return {"small": 900, "medium": 1800}.get(model, 1500)
