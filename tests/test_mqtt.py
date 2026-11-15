"""MqttBus contra un broker MQTT real en proceso (amqtt) y el simulador de actuadores."""
import asyncio
import importlib.util
import socket
import time
from pathlib import Path

import pytest

amqtt = pytest.importorskip("amqtt")

from amqtt.broker import Broker  # noqa: E402

from alivia_edge.commands.infrastructure.bus import MqttBus  # noqa: E402
from alivia_edge.shared.config import MqttCfg  # noqa: E402
from alivia_edge.commands.domain.models import ActuatorTarget, CommandRequest  # noqa: E402
from alivia_edge.commands.domain.models import Action  # noqa: E402

_spec = importlib.util.spec_from_file_location("actuator_sim", Path(__file__).parent.parent / "scripts" / "actuator_sim.py")
actuator_sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(actuator_sim)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def broker():
    port = free_port()
    b = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
        "sys_interval": 0,
        "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
        "topic-check": {"enabled": False},
    })
    await b.start()
    yield port
    # amqtt a veces no termina shutdown() (los clientes ya se cerraron): no dejar colgada la suite
    try:
        await asyncio.wait_for(b.shutdown(), timeout=5.0)
    except TimeoutError:
        pass


async def start_sim(sim):
    # el broker corre en este mismo loop: el simulador (bloqueante) debe conectarse desde otro hilo
    await asyncio.get_running_loop().run_in_executor(None, sim.start)


async def stop_sim(sim):
    await asyncio.get_running_loop().run_in_executor(None, sim.stop)


async def wait_for(pred, timeout=5.0):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise TimeoutError
        await asyncio.sleep(0.02)


def request(device_id, kind, room, action, payload):
    target = ActuatorTarget(device_id, f"AA:BB:CC:00:00:{device_id:02X}", kind, room, "")
    return CommandRequest(Action(f"{kind.lower()}_{device_id}", action), target, payload)


async def test_execute_reports_ok_fail_and_timeout_per_action(broker):
    sim = actuator_sim.ActuatorSim("127.0.0.1", broker, fail=["11"], silent=["12"], delay=0.05)
    await start_sim(sim)
    bus = MqttBus(MqttCfg(host="127.0.0.1", port=broker, client_id="voz-test"))
    await bus.start()
    try:
        await wait_for(lambda: bus.connected)
        reqs = [request(10, "DOOR", "entrada", "cerrar", "close"), request(11, "LIGHT", "sala", "apagar", "off"),
                request(12, "WINDOW", "sala", "cerrar", "close")]
        t0 = time.monotonic()
        res = await bus.execute(reqs, node="AA:BB:CC:00:00:01", timeout_s=1.0)
        elapsed = time.monotonic() - t0
        assert [(r.action.device, r.status) for r in res] == [("door_10", "ok"), ("light_11", "fail"), ("window_12", "timeout")]
        assert res[1].detail == "fallo simulado"
        assert 1.0 <= elapsed < 1.6  # esperó el timeout solo por el actuador mudo
        assert bus._pending == {}  # no quedan futuros colgados

        # contrato v2 del comando
        by_dev = {str(c["deviceId"]): c for c in sim.received}
        cmd = by_dev["10"]
        assert cmd["v"] == 2 and cmd["action"] == "close" and cmd["source"] == "AA:BB:CC:00:00:01"
        assert cmd["mac"] == "AA:BB:CC:00:00:0A" and cmd["deviceType"] == "DOOR" and cmd["roomKey"] == "entrada"
        assert cmd["ttlMs"] == 15000 and cmd["correlationId"] and cmd["orderId"]
        assert len({c["orderId"] for c in sim.received}) == 1 and len({c["correlationId"] for c in sim.received}) == 3
        assert by_dev["11"]["action"] == "off"
        assert sim.state == {"10": "close"}
    finally:
        await bus.stop()
        await stop_sim(sim)


async def test_all_ok_returns_quickly_and_late_result_is_ignored(broker):
    sim = actuator_sim.ActuatorSim("127.0.0.1", broker, delay=0.05)
    await start_sim(sim)
    bus = MqttBus(MqttCfg(host="127.0.0.1", port=broker, client_id="voz-test2"))
    await bus.start()
    try:
        await wait_for(lambda: bus.connected)
        t0 = time.monotonic()
        res = await bus.execute([request(11, "LIGHT", "sala", "encender", "on")], node="n", timeout_s=3.0)
        assert res[0].status == "ok" and time.monotonic() - t0 < 1.0
        # un resultado desconocido/tardío no rompe nada
        sim._c.publish("alivia/actuators/11/result", '{"correlationId":"zzz","ok":true}', qos=1)
        sim._c.publish("alivia/actuators/11/result", "no-json", qos=1)
        await asyncio.sleep(0.3)
        res = await bus.execute([request(11, "LIGHT", "sala", "apagar", "off")], node="n", timeout_s=3.0)
        assert res[0].status == "ok"
    finally:
        await bus.stop()
        await stop_sim(sim)


async def test_connected_flag_false_when_broker_is_down():
    bus = MqttBus(MqttCfg(host="127.0.0.1", port=free_port(), client_id="voz-test3"))
    await bus.start()
    try:
        await asyncio.sleep(0.5)
        assert not bus.connected
    finally:
        await bus.stop()
