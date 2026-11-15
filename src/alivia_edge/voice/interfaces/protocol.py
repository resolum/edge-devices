"""Protocolo "Alivia" (StreamGatewayClient del ESP32): mensajes JSON y constantes."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

SAMPLE_RATE = 16000
BYTES_PER_MS = 32  # 16 kHz * 2 bytes
MAX_MESSAGE_BYTES = 2048  # límite por mensaje de audio hacia el ESP32
UTTERANCE_ID_MAX = 39  # ID_CAPACITY = 40 en el firmware (con el terminador)


class HandshakeError(Exception):
    """Hello inválido; el texto es el motivo que se envía en {"type":"error"}."""


@dataclass
class Hello:
    device_id: str  # etiqueta legible del sketch (DEVICE_ID); NO identifica al dispositivo
    mac: str = ""  # MAC del ESP32: identifica al dispositivo ante la tabla de acceso del backend
    capabilities: list[str] = field(default_factory=list)
    mic_rate: int = SAMPLE_RATE
    playback_rate: int = SAMPLE_RATE
    bits: int = 16
    channels: int = 1
    frame_samples: int = 320
    token: str = ""


def parse_hello(msg: Any) -> Hello:
    if not isinstance(msg, dict) or msg.get("type") != "hello":
        raise HandshakeError("se esperaba hello")
    device_id = str(msg.get("deviceId", "")).strip()
    if not device_id:
        raise HandshakeError("deviceId vacío")
    mac = str(msg.get("mac", "")).strip()
    if not mac:
        raise HandshakeError("falta la MAC del dispositivo")
    hello = Hello(
        device_id=device_id,
        mac=mac,
        capabilities=[str(c) for c in msg.get("capabilities", [])],
        mic_rate=int(msg.get("micSampleRate", SAMPLE_RATE)),
        playback_rate=int(msg.get("playbackSampleRate", SAMPLE_RATE)),
        bits=int(msg.get("bits", 16)),
        channels=int(msg.get("channels", 1)),
        frame_samples=int(msg.get("frameSamples", 320)),
        token=str(msg.get("token", "")),
    )
    if hello.mic_rate != SAMPLE_RATE or hello.playback_rate != SAMPLE_RATE:
        raise HandshakeError("solo se admite 16000 Hz")
    if hello.bits != 16 or hello.channels != 1:
        raise HandshakeError("solo se admite PCM 16 bits mono")
    return hello


def ready_msg() -> dict:
    return {"type": "ready"}


def error_msg(reason: str) -> dict:
    return {"type": "error", "reason": reason}


def new_utterance_id() -> str:
    # <= 39 caracteres: si no, el ESP32 trunca el id y audio_end nunca coincide.
    return "u" + uuid.uuid4().hex[:16]


def audio_begin(utterance_id: str, priority: str = "normal") -> dict:
    return {"type": "audio_begin", "utteranceId": utterance_id, "sampleRate": SAMPLE_RATE, "priority": priority}


def audio_end(utterance_id: str) -> dict:
    return {"type": "audio_end", "utteranceId": utterance_id}


def audio_cancel(utterance_id: str = "") -> dict:
    return {"type": "audio_cancel", "utteranceId": utterance_id}
