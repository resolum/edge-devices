"""Bus de comandos hacia el edge de actuadores (Mosquitto / MQTT).

Contrato (ver docs/mqtt-contract.md):
  voz -> actuador   alivia/actuators/<deviceId>/command
                    {"v":2,"correlationId","orderId","deviceId","mac","deviceType","roomKey","action","params","source","ts","ttlMs"}
  actuador -> voz   alivia/actuators/<deviceId>/result    {"v":2,"correlationId","ok":true|false,"detail":""}
<deviceId> es el id del dispositivo en el backend (tabla de acceso). El edge de actuadores lee la misma tabla.
Se usa paho-mqtt con su hilo propio (funciona igual en Windows, Linux y macOS) y se puentea a asyncio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from alivia_edge.commands.domain.models import ActionResult, CommandRequest
from alivia_edge.shared.config import MqttCfg

log = logging.getLogger(__name__)


class SimulatedBus:
    """[mqtt].enabled = false: NO envía nada; responde éxito. Solo para probar el flujo de voz sin actuadores."""

    connected = True

    def __init__(self) -> None:
        log.warning("MODO SIMULACIÓN: los comandos NO se envían a ningún actuador (mqtt.enabled=false)")

    async def execute(self, requests, *, node, timeout_s):
        log.info("[simulado] %s", [(r.target.device_id, r.payload) for r in requests])
        await asyncio.sleep(0.2)
        return [ActionResult(r.action, "ok", "simulado") for r in requests]


class MqttBus:
    def __init__(self, cfg: MqttCfg) -> None:
        self._cfg = cfg
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        import paho.mqtt.client as mqtt

        self._loop = asyncio.get_running_loop()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._cfg.client_id)
        if self._cfg.username:
            c.username_pw_set(self._cfg.username, self._cfg.password)
        if self._cfg.tls:
            c.tls_set()
        c.will_set(self._cfg.status_topic, json.dumps({"online": False}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=15)
        c.connect_async(self._cfg.host, self._cfg.port, keepalive=30)
        c.loop_start()
        self._client = c

    async def stop(self) -> None:
        if self._client is not None:
            self._client.publish(self._cfg.status_topic, json.dumps({"online": False}), qos=1, retain=True)
            self._client.disconnect()
            self._client.loop_stop()

    # --- callbacks (hilo de paho) ---------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.error("MQTT: conexión rechazada (%s)", reason_code)
            return
        client.subscribe(self._cfg.result_topic, qos=self._cfg.qos)
        client.publish(self._cfg.status_topic, json.dumps({"online": True}), qos=1, retain=True)
        log.info("MQTT conectado a %s:%s", self._cfg.host, self._cfg.port)
        self._set_connected(True)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        log.warning("MQTT desconectado (%s)", reason_code)
        self._set_connected(False)

    def _set_connected(self, value: bool) -> None:
        self._connected = value  # asignación atómica; se lee desde el loop

    def _on_message(self, client, userdata, msg) -> None:
        try:
            data = json.loads(msg.payload)
            corr = str(data.get("correlationId", ""))
        except (ValueError, AttributeError):
            log.warning("MQTT: resultado ilegible en %s", msg.topic)
            return
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._deliver, corr, data)

    def _deliver(self, corr: str, data: dict) -> None:
        fut = self._pending.get(corr)
        if fut is not None and not fut.done():
            fut.set_result(data)

    # --- API ------------------------------------------------------------------------------
    async def execute(self, requests: list[CommandRequest], *, node: str, timeout_s: float) -> list[ActionResult]:
        assert self._client is not None and self._loop is not None, "MqttBus.start() no se ejecutó"
        order_id = uuid.uuid4().hex
        pending: list[tuple[CommandRequest, str, asyncio.Future[dict]]] = []
        immediate: dict[str, ActionResult] = {}
        for req in requests:
            corr = uuid.uuid4().hex
            fut: asyncio.Future[dict] = self._loop.create_future()
            self._pending[corr] = fut
            t = req.target
            payload = {
                "v": 2,
                "correlationId": corr,
                "orderId": order_id,
                "deviceId": t.device_id,
                "mac": t.mac,
                "deviceType": t.kind,
                "roomKey": t.room_key,
                "action": req.payload,
                "params": {},
                "source": node,
                "ts": int(time.time() * 1000),
                "ttlMs": self._cfg.ttl_ms,
            }
            topic = self._cfg.command_topic.format(device=t.device_id)
            info = self._client.publish(topic, json.dumps(payload), qos=self._cfg.qos, retain=False)
            if info.rc != 0:
                immediate[corr] = ActionResult(req.action, "fail", f"no se pudo publicar (rc={info.rc})")
                fut.cancel()
            pending.append((req, corr, fut))
            log.info("MQTT -> %s %s (%s)", topic, payload["action"], corr[:8])

        waiting = [f for _, c, f in pending if c not in immediate]
        if waiting:
            await asyncio.wait(waiting, timeout=timeout_s)

        results: list[ActionResult] = []
        for req, corr, fut in pending:
            self._pending.pop(corr, None)
            if corr in immediate:
                results.append(immediate[corr])
            elif fut.done() and not fut.cancelled():
                data = fut.result()
                ok = bool(data.get("ok"))
                results.append(ActionResult(req.action, "ok" if ok else "fail", str(data.get("detail", ""))))
            else:
                fut.cancel()
                results.append(ActionResult(req.action, "timeout", "sin respuesta del actuador"))
        return results
