#!/usr/bin/env python
"""Simulador del edge de actuadores (contrato en docs/mqtt-contract.md).

Sirve para probar el edge de voz contra un Mosquitto real antes de tener el edge de actuadores de verdad:
escucha alivia/actuators/<deviceId>/command y responde en alivia/actuators/<deviceId>/result
(<deviceId> = id del dispositivo en el backend, contrato v2).

  python scripts/actuator_sim.py --host 127.0.0.1
  python scripts/actuator_sim.py --fail 11 --silent 12     # el dispositivo 11 falla y el 12 no responde
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("actuator_sim")


class ActuatorSim:
    def __init__(self, host="127.0.0.1", port=1883, *, username="", password="", fail=(), silent=(), delay=0.3,
                 command_filter="alivia/actuators/+/command", result_topic="alivia/actuators/{device}/result"):
        self.fail, self.silent, self.delay = set(fail), set(silent), delay
        self.command_filter, self.result_topic = command_filter, result_topic
        self.state: dict[str, str] = {}  # deviceId -> última acción aplicada
        self.received: list[dict] = []
        self._host, self._port = host, port
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="alivia-actuator-sim")
        if username:
            self._c.username_pw_set(username, password)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self.ready = threading.Event()

    def start(self) -> None:
        self._c.connect(self._host, self._port, keepalive=30)
        self._c.loop_start()
        if not self.ready.wait(5):
            raise RuntimeError("el simulador no pudo suscribirse al broker")

    def stop(self) -> None:
        self._c.disconnect()
        self._c.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(self.command_filter, qos=1)
        self.ready.set()

    def _on_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload)
        except ValueError:
            return
        self.received.append(cmd)
        device = str(cmd.get("deviceId", cmd.get("device", "")))
        age_ms = time.time() * 1000 - cmd.get("ts", 0)
        if age_ms > cmd.get("ttlMs", 15000):
            log.warning("comando caducado ignorado: %s", cmd)
            return
        if device in self.silent:
            log.info("%s: sin respuesta (simulado)", device)
            return
        ok = device not in self.fail
        if ok:
            self.state[device] = cmd.get("action", "")
        result = {"v": 2, "correlationId": cmd.get("correlationId"), "ok": ok, "detail": "" if ok else "fallo simulado", "ts": int(time.time() * 1000)}
        log.info("%s -> %s (%s)", device, cmd.get("action"), "ok" if ok else "FALLO")
        threading.Timer(self.delay, lambda: client.publish(self.result_topic.format(device=device), json.dumps(result), qos=1)).start()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--fail", nargs="*", default=[], help="deviceId que responden ok=false")
    ap.add_argument("--silent", nargs="*", default=[], help="deviceId que no responden (timeout)")
    ap.add_argument("--delay", type=float, default=0.3, help="segundos antes de responder")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sim = ActuatorSim(args.host, args.port, username=args.username, password=args.password,
                      fail=args.fail, silent=args.silent, delay=args.delay)
    sim.start()
    log.info("Simulador de actuadores escuchando en %s:%d (Ctrl+C para salir)", args.host, args.port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
