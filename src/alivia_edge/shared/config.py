"""Configuración del edge (TOML -> dataclasses).

Todo tiene valor por defecto razonable; el config.toml solo necesita lo que cambies.
Las rutas relativas se resuelven respecto a la carpeta del config.toml.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8765
    path: str = "/ws"
    token: str = ""  # si está vacío se usa la variable de entorno ALIVIA_WS_TOKEN
    hello_timeout_s: float = 5.0  # el ESP32 espera "ready" 5 s como máximo
    idle_timeout_s: float = 15.0  # sin ningún dato del nodo (el mic manda cada 40 ms)


@dataclass
class BtLinkCfg:
    mac: str = ""  # MAC del ESP32 (la misma que ve el backend); sirve para no abrir Bluetooth si ya hay WiFi
    port: str = ""  # COM5 (Windows, puerto saliente) o /dev/rfcomm0 (Linux)
    baudrate: int = 115200  # irrelevante para SPP, pero pyserial lo pide


@dataclass
class BluetoothCfg:
    enabled: bool = False
    links: list[BtLinkCfg] = field(default_factory=list)
    retry_min_s: float = 2.0
    retry_max_s: float = 15.0


@dataclass
class SpeakerCfg:
    lead_ms: int = 300  # ráfaga inicial y adelanto máximo sobre tiempo real (anillo del ESP32 = 500 ms)
    chunk_bytes: int = 1920  # 60 ms; par y <= 2048
    tail_ms: int = 300  # se ignora el micrófono este tiempo tras playback_done
    done_margin_s: float = 2.0  # espera extra por playback_done antes de darlo por hecho


@dataclass
class AudioCfg:
    preroll_ms: int = 1500  # audio previo al disparo que se conserva para Whisper
    mic_stall_s: float = 3.0  # sin audio del mic mientras se graba -> se aborta


@dataclass
class WakeCfg:
    model_path: str = "models/vosk-model-small-es-0.42"  # el modelo pequeño soporta gramática
    phrases: list[str] = field(
        default_factory=lambda: ["alivia", "oye alivia", "ey alivia", "hey alivia", "hola alivia", "ok alivia"]
    )
    keyword: str = "alivia"
    min_conf: float = 0.5  # confianza mínima de Vosk al validar el disparo (0 = no validar)
    strip_names: list[str] = field(default_factory=lambda: ["alivia", "olivia", "alibia", "alivea", "aliva"])
    command_min_speech_ms: int = 450  # voz tras el disparo para suponer "frase completa en un aliento"
    capture_start_timeout_s: float = 1.5
    capture_end_silence_ms: int = 500
    capture_max_s: float = 8.0


@dataclass
class VadCfg:
    model_path: str = "models/silero_vad.onnx"
    threshold_on: float = 0.5
    threshold_off: float = 0.35
    min_speech_ms: int = 200
    end_silence_ms: int = 700  # fin de frase al dictar una orden
    answer_end_silence_ms: int = 500  # fin de frase en respuestas cortas (sí/no)


@dataclass
class SttCfg:
    model: str = "large-v3-turbo"
    model_dir: str = ""  # carpeta de descarga/caché (vacío = caché por defecto)
    device: str = "auto"  # auto | cpu | cuda
    compute_type: str = "auto"  # auto | int8 | float16 | int8_float16 ...
    cpu_threads: int = 0
    beam_size: int = 1
    language: str = "es"
    initial_prompt: str = "Órdenes para la casa: abre la puerta, cierra la ventana, enciende la luz, apaga la luz."
    revision: str = ""  # commit/etiqueta del repositorio en Hugging Face (vacío = el último); fíjalo para no recibir cambios
    speech_hint: bool = True  # sesga a Whisper hacia los nombres reales de tus dispositivos y habitaciones
    vram_budget_mb: int = 0  # 0 = detectar; VRAM que puede usar Whisper (el resto es para el LLM)


@dataclass
class LlmCfg:
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:3b"
    timeout_s: float = 30.0
    keep_alive: str = "30m"
    num_ctx: int = 2048
    num_predict: int = 300  # tope de tokens de salida (el JSON de una orden ocupa menos de 150)
    temperature: float = 0.0
    seed: int = 7  # misma frase, misma interpretación


@dataclass
class TtsCfg:
    backend: str = "python"  # python (paquete piper-tts) | exe (binario piper)
    voice_path: str = "models/piper/es_MX-claude-high.onnx"
    piper_exe: str = ""
    length_scale: float = 1.0
    gain: float = 1.0


@dataclass
class DialogCfg:
    order_window_s: float = 10.0
    order_start_timeout_s: float = 4.0  # si no empieza a hablar en este tiempo, se cierra la ventana
    answer_start_timeout_s: float = 8.0  # sin respuesta a una confirmación -> se cancela
    answer_max_s: float = 6.0
    max_clarifications: int = 2
    exec_timeout_s: float = 5.0
    greetings: list[str] = field(default_factory=lambda: ["Dígame", "Sí, dígame", "¿Qué desea?", "Sí"])


@dataclass
class BackendCfg:
    """alivia-web-services: de ahí llega la tabla de acceso (qué dispositivos existen y cuáles están activos)."""

    base_url: str = "http://127.0.0.1:8080"
    credential: str = ""  # <edgeId>.<secreto>; si está vacío se usa la variable de entorno ALIVIA_EDGE_CREDENTIAL
    wait_s: int = 25  # espera larga (el backend admite hasta 25)
    request_timeout_s: float = 10.0
    retry_min_s: float = 2.0
    retry_max_s: float = 60.0
    idle_retry_s: float = 30.0  # tras 401/404


@dataclass
class SecurityCfg:
    verify_models: bool = True  # al arrancar, comprueba los SHA-256 de models.lock.json
    lock_path: str = "models.lock.json"
    allow_unpinned: bool = True  # False: negarse a arrancar si algún modelo no tiene hash registrado


@dataclass
class DatabaseCfg:
    path: str = "alivia_edge.db"  # tabla de acceso e historial de comandos


@dataclass
class MqttCfg:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    client_id: str = "alivia-voice-edge"
    command_topic: str = "alivia/actuators/{device}/command"
    result_topic: str = "alivia/actuators/+/result"
    status_topic: str = "alivia/voice/status"
    qos: int = 1
    ttl_ms: int = 15000


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    backend: BackendCfg = field(default_factory=BackendCfg)
    database: DatabaseCfg = field(default_factory=DatabaseCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)
    room_labels: dict[str, str] = field(default_factory=dict)  # roomKey -> "la sala" (como se dice en voz alta)
    bluetooth: BluetoothCfg = field(default_factory=BluetoothCfg)
    speaker: SpeakerCfg = field(default_factory=SpeakerCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    wake: WakeCfg = field(default_factory=WakeCfg)
    vad: VadCfg = field(default_factory=VadCfg)
    stt: SttCfg = field(default_factory=SttCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    tts: TtsCfg = field(default_factory=TtsCfg)
    dialog: DialogCfg = field(default_factory=DialogCfg)
    mqtt: MqttCfg = field(default_factory=MqttCfg)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, p: str) -> str:
        """Ruta relativa -> absoluta respecto al config.toml."""
        if not p:
            return p
        path = Path(p)
        return str(path if path.is_absolute() else (self.base_dir / path))


def _build(cls: type, data: dict[str, Any] | None, where: str):
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key not in known:
            log.warning("config: clave desconocida [%s].%s (se ignora)", where, key)
            continue
        kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | os.PathLike[str]) -> Config:
    p = Path(path).resolve()
    with open(p, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config(base_dir=p.parent)
    for name in ("server", "backend", "database", "security", "speaker", "audio", "wake", "vad", "stt", "llm", "tts", "dialog", "mqtt"):
        setattr(cfg, name, _build(type(getattr(cfg, name)), raw.get(name), name))

    bt_raw = dict(raw.get("bluetooth", {}))
    links = [_build(BtLinkCfg, item, "bluetooth.links") for item in bt_raw.pop("links", [])]
    cfg.bluetooth = _build(BluetoothCfg, bt_raw, "bluetooth")
    cfg.bluetooth.links = links

    cfg.room_labels = {str(k): str(v) for k, v in (raw.get("rooms", {}).get("labels", {}) or {}).items()}

    if not cfg.server.token:
        cfg.server.token = os.environ.get("ALIVIA_WS_TOKEN", "")
    if not cfg.server.token:
        raise ValueError("Falta el token: define [server].token o la variable ALIVIA_WS_TOKEN")
    if not cfg.backend.credential:
        cfg.backend.credential = os.environ.get("ALIVIA_EDGE_CREDENTIAL", "")
    if not cfg.backend.credential:
        raise ValueError("Falta la credencial del edge: define [backend].credential o la variable ALIVIA_EDGE_CREDENTIAL")
    if not cfg.backend.base_url.startswith(("http://", "https://")):
        raise ValueError("[backend].base_url debe empezar con http:// o https://")
    if cfg.speaker.chunk_bytes % 2 or cfg.speaker.chunk_bytes > 2048:
        raise ValueError("[speaker].chunk_bytes debe ser par y <= 2048")
    return cfg
