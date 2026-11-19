"""Avisos al familiar: pedido de ayuda (voz y botón) y batería baja, hacia POST /api/v1/edge/events."""
import asyncio
import json

import httpx
import pytest

from alivia_edge.alerts.application.service import AlertService, EventReporter
from alivia_edge.alerts.domain import HELP_REQUESTED, LOW_BATTERY, BatteryWatch, EdgeEvent, is_help_request
from alivia_edge.alerts.infrastructure.backend import HttpEventSender, RejectedError, RetryableError
from alivia_edge.voice.application.dialog import P_HELP_SENT, P_NO_DEVICES, Dialog, Outcome
from alivia_edge.voice.interfaces.server import EdgeServer

from .fakes import FAKE_MAC, WAKE_ONLY, FakeBus, FakeNode, make_config, make_services, speech, wake_and_command


class FakeSender:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent = []

    async def send(self, event):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(event)
        return 1


async def wait_until(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        if loop.time() - t0 > timeout:
            raise AssertionError("no se cumplio la condicion a tiempo")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("text", [
    "ayuda", "Ayúdame por favor", "auxilio", "¡Socorro!", "me caí en el baño", "Me he caído", "es una emergencia",
    "no puedo levantarme", "me siento mal", "llama a mi hija", "necesito ayuda", "ayúdenme",
])
def test_help_requests(text):
    assert is_help_request(text)


@pytest.mark.parametrize("text", [
    "", None, "cierra la puerta", "ayúdame a cerrar la puerta", "ayudame a apagar la luz", "abre la ventana",
])
def test_not_help_requests(text):
    assert not is_help_request(text)


def test_event_json_and_battery_watch():
    assert EdgeEvent(LOW_BATTERY, device_id=1, battery_pct=9, event_id="e").to_json() == {
        "eventId": "e", "type": "LOW_BATTERY", "deviceId": 1, "batteryPct": 9}
    assert EdgeEvent(HELP_REQUESTED, detail="x" * 300, event_id="h").to_json() == {
        "eventId": "h", "type": "HELP_REQUESTED", "detail": "x" * 200}
    watch = BatteryWatch(20, 30)
    assert [watch.check("n", p) for p in (50, 20, 10, 25, 40, 15, "?")] == [False, True, False, False, False, True, False]
    with pytest.raises(ValueError):
        BatteryWatch(20, 20)


async def test_reporter_retries_drops_rejected_and_keeps_order():
    sender = FakeSender([RetryableError("red"), RetryableError("HTTP 503", 0.01), RejectedError("HTTP 404")])
    events = EventReporter(sender, first_retry_s=0.01, queue_max=3)
    events.start()
    for i in "abc":
        events.submit(EdgeEvent(LOW_BATTERY, event_id=i))
    await wait_until(lambda: events.pending == 0)
    assert [e.event_id for e in sender.sent] == ["b", "c"]  # "a" lo rechazó el backend (404) tras dos reintentos
    assert events.delivered == 2
    await events.stop()


def test_alert_service_names_the_microphone_and_survives_a_bad_lookup():
    events = EventReporter(FakeSender())
    alerts = AlertService(events, BatteryWatch(20, 30), lambda mac: 1 if mac == FAKE_MAC else 1 // 0)
    alerts.request_help(FAKE_MAC, " me caí ")
    alerts.on_battery(FAKE_MAC, 50)
    alerts.on_battery(FAKE_MAC, 12.4)
    alerts.on_battery(FAKE_MAC, 11)
    alerts.request_help("otro")
    got = [(e.type, e.device_id, e.battery_pct, e.detail) for e in events._pending]
    assert got == [(HELP_REQUESTED, 1, None, "me caí"), (LOW_BATTERY, 1, 12, ""), (HELP_REQUESTED, None, None, "")]


async def test_http_sender_talks_to_the_events_endpoint():
    seen = []
    replies = [httpx.Response(202, json={"notified": 2}), httpx.Response(429, headers={"Retry-After": "3"}),
               httpx.Response(401), httpx.Response(202, text="?")]

    def handler(request):
        seen.append((request.url.path, request.headers["Authorization"], json.loads(request.content)))
        return replies.pop(0)

    sender = HttpEventSender("http://backend/", "1.s", transport=httpx.MockTransport(handler))
    event = EdgeEvent(HELP_REQUESTED, device_id=1, event_id="h1")
    assert await sender.send(event) == 2
    assert seen[0] == ("/api/v1/edge/events", "Bearer 1.s", {"eventId": "h1", "type": "HELP_REQUESTED", "deviceId": 1})
    with pytest.raises(RetryableError) as busy:
        await sender.send(event)
    assert busy.value.retry_after == 3
    with pytest.raises(RejectedError):
        await sender.send(event)
    assert await sender.send(event) == 0
    await sender.aclose()

    def down(request):
        raise httpx.ConnectError("sin red")

    offline = HttpEventSender("http://backend", "1.s", transport=httpx.MockTransport(down))
    with pytest.raises(RetryableError):
        await offline.send(event)
    with pytest.raises(ValueError):
        HttpEventSender("http://backend", "")


# ------------------------------------------------------------------ por voz y con el botón, de punta a punta
@pytest.fixture
async def harness():
    created = []

    async def make(stt_script=(), table=None):
        cfg = make_config()
        svc = make_services(cfg, llm_script=[], stt_script=list(stt_script), table=table)
        events = EventReporter(FakeSender())
        svc.alerts = AlertService(events, BatteryWatch(20, 30), lambda mac: 1)
        server = EdgeServer(svc)
        await server.start()
        node = FakeNode()
        created.append((server, node))
        assert await node.connect(f"ws://127.0.0.1:{server.port}/ws") == {"type": "ready"}
        return svc, node, events

    yield make
    for server, node in created:
        await node.close()
        await server.stop()


async def test_alivia_ayuda_warns_the_family_without_the_llm_or_a_confirmation(harness):
    svc, node, events = await harness(stt_script=["Alivia, ayúdame, me caí"])
    node.say(wake_and_command())
    await node.wait_utterances(1)
    assert svc.tts.spoken == [P_HELP_SENT]
    assert svc.llm.histories == []
    assert [(e.type, e.device_id, e.detail) for e in events._pending] == [(HELP_REQUESTED, 1, "ayúdame, me caí")]


async def test_help_after_the_greeting(harness):
    svc, node, events = await harness(stt_script=["auxilio"])
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(1200))
    await node.wait_utterances(2)
    assert svc.tts.spoken[1] == P_HELP_SENT
    assert events.pending == 1


async def test_the_help_button_and_the_battery_of_the_node(harness):
    svc, node, events = await harness()
    await node.ws.send(json.dumps({"type": "telemetry", "batteryPct": 50}))
    await node.ws.send(json.dumps({"type": "telemetry", "batteryPct": 14}))
    await node.ws.send(json.dumps({"type": "help_request"}))
    await node.wait_utterances(1)
    assert svc.tts.spoken == [P_HELP_SENT]
    assert [(e.type, e.battery_pct, e.detail) for e in events._pending] == [
        (LOW_BATTERY, 14, ""), (HELP_REQUESTED, None, "presionó el botón de ayuda")]


async def test_no_devices_still_lets_the_person_ask_for_help():
    said = []

    class IO:
        async def say(self, text):
            said.append(text)

        async def record(self, **kw):
            return b"x"

        async def transcribe(self, pcm):
            return "me siento mal"

        async def quick_yesno(self, pcm):
            return None

    class EmptyCatalog:
        devices = {}

    helped = []
    cfg = make_config().dialog
    dialog = Dialog(cfg=cfg, catalog=EmptyCatalog(), llm=None, bus=FakeBus(), io=IO(), node_id="n", room="sala",
                    strip_names=[], on_help=helped.append)
    assert await dialog.run(None) == Outcome.HELP_REQUESTED
    assert helped == ["me siento mal"]

    class NoHelpIO(IO):
        async def transcribe(self, pcm):
            return "abre la puerta"

    dialog.io = NoHelpIO()
    assert await dialog.run(None) == Outcome.ERROR
    assert said[-1] == P_NO_DEVICES
