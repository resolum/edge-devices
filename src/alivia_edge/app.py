"""Raíz de composición del edge (equivalente a app.py de los edge de referencia): crea los contextos y los conecta.

  access    tabla de acceso del backend (qué dispositivos existen y cuáles están activos) + política de acceso
  commands  catálogo dinámico, interpretación (Qwen), despacho por MQTT, historial
  voice     enlaces con el ESP32 (WebSocket/Bluetooth), audio, diálogo
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alivia_edge.access.application.services import AccessService
from alivia_edge.access.application.sync import AccessSyncService
from alivia_edge.access.infrastructure.backend_client import HttpBackendGateway
from alivia_edge.access.infrastructure.models import AccessNodeModel, AccessTableModel
from alivia_edge.access.infrastructure.repositories import SqliteAccessTableRepository
from alivia_edge.access.domain.value_objects import MacAddress
from alivia_edge.alerts.application.service import AlertService, EventReporter
from alivia_edge.alerts.domain import BatteryWatch
from alivia_edge.alerts.infrastructure.backend import HttpEventSender
from alivia_edge.commands.application.catalog_service import CatalogService
from alivia_edge.commands.application.dispatcher import CommandDispatcher
from alivia_edge.commands.domain.ports import CommandBus
from alivia_edge.commands.infrastructure.bus import MqttBus, SimulatedBus
from alivia_edge.commands.infrastructure.models import CommandRecordModel
from alivia_edge.commands.infrastructure.repositories import SqliteCommandHistoryRepository
from alivia_edge.commands.integration.access_acl import AccessAntiCorruptionLayer
from alivia_edge.shared.config import Config
from alivia_edge.shared.infrastructure import integrity
from alivia_edge.shared.infrastructure.database import close_db, init_db
from alivia_edge.voice.application.dialog import STATIC_PHRASES
from alivia_edge.voice.domain.ports import Stt, Tts, Vad, WakeDetector, YesNoRecognizer
from alivia_edge.voice.integration.access_acl import MicrophoneAccessGate

log = logging.getLogger(__name__)

DB_MODELS = [AccessTableModel, AccessNodeModel, CommandRecordModel]


@dataclass
class Services:
    cfg: Config
    access: AccessService
    gate: MicrophoneAccessGate
    catalogs: CatalogService
    dispatcher: CommandDispatcher
    llm: Any  # OllamaLlm o doble con interpret(history, room, catalog)
    stt: Stt
    tts: Tts
    new_wake: Callable[[], WakeDetector]
    new_yesno: Callable[[], YesNoRecognizer]
    new_vad: Callable[[], Vad]
    executor: ThreadPoolExecutor
    bus: CommandBus | None = None
    sync: AccessSyncService | None = None
    alerts: AlertService | None = None  # pedido de ayuda y batería baja hacia el backend
    events: EventReporter | None = None


def preflight(cfg: Config) -> list[str]:
    """Problemas de archivos/modelos, todos de una vez y en claro (en vez de un traceback de Vosk o Piper)."""
    problems: list[str] = []
    if not Path(cfg.resolve(cfg.wake.model_path)).is_dir():
        problems.append(f"Falta el modelo de Vosk (carpeta): {cfg.resolve(cfg.wake.model_path)}")
    voice = Path(cfg.resolve(cfg.tts.voice_path))
    if not voice.is_file() or not Path(str(voice) + ".json").is_file():
        problems.append(f"Falta la voz de Piper (.onnx y .onnx.json): {voice}")
    if cfg.tts.backend == "exe" and not (cfg.tts.piper_exe and Path(cfg.tts.piper_exe).is_file()):
        problems.append(f"[tts].piper_exe no existe: {cfg.tts.piper_exe!r}")
    if not Path(cfg.resolve(cfg.vad.model_path)).is_file():
        log.warning("Falta Silero VAD (%s): se usará un VAD por energía, mucho peor. Ejecuta scripts/fetch_models.py", cfg.vad.model_path)
    problems += _integrity_problems(cfg)
    if cfg.tts.backend == "python":
        log.info(
            "Licencia: el paquete piper-tts es GPL-3.0 (lleva espeak-ng). Las voces tienen la suya (es_MX claude: Apache-2.0). "
            "Si vas a distribuir el edge a clientes, revisa docs/modelos.md ([tts] backend = \"exe\" lo separa en otro proceso)."
        )
    for prefix, note in integrity.LICENSE_NOTES.items():
        if cfg.llm.model.startswith(prefix):
            log.warning("Licencia de %s: %s", cfg.llm.model, note)
    return problems


def _integrity_problems(cfg: Config) -> list[str]:
    """Compara los modelos locales con models.lock.json. Un hash distinto es bloqueante (archivo alterado)."""
    if not cfg.security.verify_models:
        return []
    lock = {n: e for n, e in integrity.load_lock(cfg.resolve(cfg.security.lock_path)).items() if e.kind != "ollama"}
    if not lock:
        log.warning("No hay %s: no se puede verificar la integridad de los modelos", cfg.security.lock_path)
        return []
    problems: list[str] = []
    for c in integrity.verify_all(cfg.resolve("models"), lock):
        if c.status == "mismatch":
            problems.append(f"Modelo alterado o distinto al registrado ({c.name}): {c.detail}")
        elif c.status == "unpinned":
            msg = f"Modelo sin hash registrado ({c.name}): {c.detail}"
            if cfg.security.allow_unpinned:
                log.warning(msg)
            else:
                problems.append(msg)
        elif c.status == "ok":
            log.info("integridad ok: %s", c.name)
    return problems


async def _check_ollama_model(svc: "Services") -> None:
    """El modelo de Ollama debe existir y, si hay huella registrada, coincidir con ella."""
    from alivia_edge.commands.domain.errors import LlmError

    if not hasattr(svc.llm, "model_digest"):
        return
    try:
        got = await svc.llm.model_digest()
    except LlmError as exc:
        log.error("%s", exc)
        return
    log.info("Ollama %s digest=%s", svc.cfg.llm.model, got[:16])
    if not svc.cfg.security.verify_models:
        return
    entry = integrity.load_lock(svc.cfg.resolve(svc.cfg.security.lock_path)).get(f"ollama:{svc.cfg.llm.model}")
    if entry and entry.sha256 and entry.sha256 != got:
        raise integrity.IntegrityError(
            f"El modelo {svc.cfg.llm.model} de Ollama cambió (digest {got[:16]}... != {entry.sha256[:16]}...). "
            "Si lo actualizaste a propósito: python scripts/verify_models.py --pin-ollama " + svc.cfg.llm.model
        )


def build_services(cfg: Config) -> Services:
    """Carga los modelos (bloqueante, tarda). Los imports pesados son perezosos para poder probar sin ellos."""
    from alivia_edge.commands.infrastructure.llm import OllamaLlm
    from alivia_edge.voice.infrastructure.audio.stt import WhisperStt
    from alivia_edge.voice.infrastructure.audio.tts import PiperTts
    from alivia_edge.voice.infrastructure.audio.vad import make_vad
    from alivia_edge.voice.infrastructure.audio.vosk_rec import VoskWake, VoskYesNo, load_vosk_model

    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="models")

    # access: la tabla guardada permite funcionar sin internet; el backend la mantiene al día
    init_db(cfg.resolve(cfg.database.path), DB_MODELS)
    access = AccessService(SqliteAccessTableRepository())
    access.load()
    gateway = HttpBackendGateway(
        cfg.backend.base_url, cfg.backend.credential,
        request_timeout_s=cfg.backend.request_timeout_s, max_wait_s=cfg.backend.wait_s,
    )
    sync = AccessSyncService(
        access, gateway, wait_s=cfg.backend.wait_s, retry_min_s=cfg.backend.retry_min_s,
        retry_max_s=cfg.backend.retry_max_s, idle_retry_s=cfg.backend.idle_retry_s,
    )

    # alerts: lo que el nodo avisa al familiar
    events = EventReporter(
        HttpEventSender(cfg.backend.base_url, cfg.backend.credential, request_timeout_s=cfg.backend.request_timeout_s),
        queue_max=cfg.backend.events_queue_max, retry_max_s=cfg.backend.retry_max_s,
    )

    def device_id_of(mac: str) -> int | None:
        table = access.table
        node = table.find_by_mac(MacAddress(mac)) if table else None
        return node.device_id if node else None

    alerts = AlertService(events, BatteryWatch(cfg.backend.low_battery_pct, cfg.backend.battery_rearm_pct), device_id_of)

    # commands
    acl = AccessAntiCorruptionLayer(access)
    catalogs = CatalogService(acl, cfg.room_labels)
    bus: CommandBus = MqttBus(cfg.mqtt) if cfg.mqtt.enabled else SimulatedBus()
    dispatcher = CommandDispatcher(bus, acl, SqliteCommandHistoryRepository())

    log.info("Cargando Vosk (%s)...", cfg.wake.model_path)
    vosk_model = load_vosk_model(cfg.resolve(cfg.wake.model_path))
    log.info("Cargando Whisper (%s)...", cfg.stt.model)
    stt = WhisperStt(cfg.stt, executor)
    stt.load()
    log.info("Cargando Piper (%s)...", cfg.tts.voice_path)
    tts = PiperTts(cfg.tts, cfg.resolve(cfg.tts.voice_path), executor)
    tts.load()

    vad_path = cfg.resolve(cfg.vad.model_path)
    return Services(
        cfg=cfg,
        access=access,
        gate=MicrophoneAccessGate(access),
        catalogs=catalogs,
        dispatcher=dispatcher,
        llm=OllamaLlm(cfg.llm),
        stt=stt,
        tts=tts,
        new_wake=lambda: VoskWake(vosk_model, cfg.wake),
        new_yesno=lambda: VoskYesNo(vosk_model),
        new_vad=lambda: make_vad(cfg.vad, vad_path),
        executor=executor,
        bus=bus,
        sync=sync,
        alerts=alerts,
        events=events,
    )


async def start_services(svc: Services) -> None:
    """Parte asíncrona: conexiones y calentamiento (que la primera orden no pague la carga de modelos)."""
    if hasattr(svc.llm, "start"):
        await svc.llm.start()
        await _check_ollama_model(svc)
        try:
            await svc.llm.warmup(svc.catalogs.current())
            log.info("LLM listo (%s)", svc.cfg.llm.model)
        except Exception as exc:  # noqa: BLE001
            log.error("Ollama no responde (%s). ¿Está corriendo y hiciste `ollama pull %s`?", exc, svc.cfg.llm.model)
    if svc.bus is not None and hasattr(svc.bus, "start"):
        await svc.bus.start()
    if svc.sync is not None:
        svc.sync.start()
    if svc.events is not None:
        svc.events.start()
    if hasattr(svc.tts, "prewarm"):
        await svc.tts.prewarm(STATIC_PHRASES + list(svc.cfg.dialog.greetings))
    await svc.stt.transcribe(b"\x00\x00" * 16000)  # calienta Whisper con 1 s de silencio


async def stop_services(svc: Services) -> None:
    if svc.events is not None:
        await svc.events.stop()
    if svc.sync is not None:
        await svc.sync.stop()
    if svc.bus is not None and hasattr(svc.bus, "stop"):
        await svc.bus.stop()
    if hasattr(svc.llm, "aclose"):
        await svc.llm.aclose()
    svc.executor.shutdown(wait=False, cancel_futures=True)
    close_db()
