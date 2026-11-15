"""Pruebas de punta a punta: un ESP32 falso habla el protocolo real contra el servidor real
(modelos sustituidos por dobles guionados; el diálogo, el pacing, el anti-eco y los transportes son los reales)."""
import asyncio
import json
import logging

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from alivia_edge.voice.application.dialog import P_BUS_DOWN, P_CANCELLED, P_NO_SPEECH, P_WHAT_THEN
from alivia_edge.commands.domain.models import Action
from alivia_edge.voice.interfaces.server import EdgeServer
from alivia_edge.voice.interfaces.transports.bluetooth import BtLink
from alivia_edge.voice.interfaces.transports.frames import TYPE_AUDIO, TYPE_PING, TYPE_PONG, TYPE_TEXT

from .fakes import (
    FAKE_MAC, NO_MARK, WAKE_ONLY, YES_MARK, FakeBus, FakeNode, FakeSerial, llm_json, make_config, make_services,
    silence_chunk, speech, speech_chunk, wake_and_command,
)

GREETINGS = {"Dígame", "Sí, dígame", "¿Qué desea?", "Sí"}


@pytest.fixture
async def harness():
    created = []

    async def make(llm_script=(), stt_script=(), bus=None):
        cfg = make_config()
        svc = make_services(cfg, llm_script=list(llm_script), stt_script=list(stt_script), bus=bus)
        server = EdgeServer(svc)
        await server.start()
        node = FakeNode()
        created.append((server, node))
        first = await node.connect(f"ws://127.0.0.1:{server.port}/ws")
        assert first == {"type": "ready"}
        return server, svc, node

    yield make
    for server, node in created:
        await node.close()
        await server.stop()


# ------------------------------------------------------------------ flujos de voz
async def test_happy_path_wake_greeting_order_confirm_execute(harness):
    bus = FakeBus()
    server, svc, node = await harness(
        llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"], bus=bus
    )
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)  # saludo
    assert svc.tts.spoken[0] in GREETINGS
    node.say(speech(1200))  # la orden
    await node.wait_utterances(2)  # confirmación
    assert svc.tts.spoken[1] == "¿Quiere que cierre la puerta de la entrada?"
    assert bus.executed == []  # NADA se ejecuta antes del sí
    node.say(speech(600, marker=YES_MARK))
    await node.wait_utterances(3)  # resultado
    assert bus.executed == [[Action("door_10", "cerrar")]]
    assert svc.tts.spoken[2] == "Listo. La puerta de la entrada quedó cerrada."
    assert all(u["bytes"] == 5120 for u in node.utterances)  # el parlante recibió todo el audio
    assert svc.llm.histories[0][-1] == {"role": "user", "content": "cierra la puerta"}


async def test_one_breath_command_skips_greeting_and_strips_wake_name(harness):
    bus = FakeBus()
    server, svc, node = await harness(
        llm_script=[llm_json("ok", scene="todo")], stt_script=["Oye Olivia, ya me voy a descansar, me cierras todo"], bus=bus
    )
    node.say(wake_and_command())
    await node.wait_utterances(1)
    assert svc.tts.spoken[0] == "¿Quiere que cierre la puerta de la entrada, apague la luz de la sala y cierre la ventana de la sala?"
    assert svc.llm.histories[0][-1]["content"] == "ya me voy a descansar, me cierras todo"
    node.say(speech(600, marker=YES_MARK))
    await node.wait_utterances(2)
    assert [a.device for a in bus.executed[0]] == ["door_10", "light_11", "window_12"]
    assert svc.tts.spoken[1].startswith("Listo. La puerta de la entrada quedó cerrada, la luz de la sala quedó apagada")


async def test_clarification_with_guess_then_no_then_new_order(harness):
    bus = FakeBus()
    server, svc, node = await harness(
        llm_script=[llm_json("clarify", [("door_10", "abrir")]), llm_json("ok", [("window_12", "cerrar")])],
        stt_script=["la abre", "la ventana"],
        bus=bus,
    )
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(800))
    await node.wait_utterances(2)
    assert svc.tts.spoken[1] == "No estoy seguro de haber entendido. ¿Quiere que abra la puerta de la entrada?"
    node.say(speech(400, marker=NO_MARK))  # "no"
    await node.wait_utterances(3)
    assert svc.tts.spoken[2] == P_WHAT_THEN
    node.say(speech(800))  # "la ventana"
    await node.wait_utterances(4)
    assert svc.tts.spoken[3] == "¿Quiere que cierre la ventana de la sala?"
    assert svc.llm.histories[1][-1]["content"] == "No, eso no. la ventana"
    assert bus.executed == []
    node.say(speech(400, marker=YES_MARK))
    await node.wait_utterances(5)
    assert bus.executed == [[Action("window_12", "cerrar")]]


async def test_clarify_guess_confirmed_with_yes_executes_that_action(harness):
    bus = FakeBus()
    server, svc, node = await harness(llm_script=[llm_json("clarify", [("door_10", "abrir")])], stt_script=["la abre"], bus=bus)
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(800))
    await node.wait_utterances(2)
    node.say(speech(400, marker=YES_MARK))
    await node.wait_utterances(3)
    assert bus.executed == [[Action("door_10", "abrir")]]


async def test_open_question_when_info_missing_then_confirmation(harness):
    bus = FakeBus()
    server, svc, node = await harness(
        llm_script=[llm_json("clarify", question="¿Qué desea hacer con la luz?"), llm_json("ok", [("light_11", "apagar")])],
        stt_script=["la luz", "apágala"],
        bus=bus,
    )
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(800))
    await node.wait_utterances(2)
    assert svc.tts.spoken[1] == "¿Qué desea hacer con la luz?"
    node.say(speech(800))
    await node.wait_utterances(3)
    assert svc.tts.spoken[2] == "¿Quiere que apague la luz de la sala?"
    node.say(speech(400, marker=YES_MARK))
    await node.wait_utterances(4)
    assert bus.executed == [[Action("light_11", "apagar")]]


async def test_user_says_no_to_final_confirmation_cancels(harness):
    bus = FakeBus()
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"], bus=bus)
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(1200))
    await node.wait_utterances(2)
    node.say(speech(400, marker=NO_MARK))
    await node.wait_utterances(3)
    assert svc.tts.spoken[2] == P_CANCELLED and bus.executed == []


async def test_no_speech_after_greeting_closes_window(harness):
    server, svc, node = await harness()
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    await node.wait_utterances(2)  # sin hablar: vence order_start_timeout_s
    assert svc.tts.spoken[1] == P_NO_SPEECH


async def test_no_answer_to_confirmation_cancels_without_executing(harness):
    bus = FakeBus()
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"], bus=bus)
    node.say(wake_and_command())
    await node.wait_utterances(1)
    await node.wait_utterances(2, timeout=6)  # silencio: vence answer_start_timeout_s
    assert svc.tts.spoken[1] == "No escuché respuesta, cancelo la orden." and bus.executed == []


async def test_failed_and_timed_out_actuators_are_reported(harness):
    bus = FakeBus(status="fail")
    server, svc, node = await harness(llm_script=[llm_json("ok", [("light_11", "apagar")])], stt_script=["apaga la luz"], bus=bus)
    node.say(wake_and_command())
    await node.wait_utterances(1)
    node.say(speech(400, marker=YES_MARK))
    await node.wait_utterances(2)
    assert svc.tts.spoken[1] == "Lo siento, no pude apagar la luz de la sala."


async def test_bus_down_is_reported_before_asking_confirmation(harness):
    bus = FakeBus(connected=False)
    server, svc, node = await harness(llm_script=[llm_json("ok", [("light_11", "apagar")])], stt_script=["apaga la luz"], bus=bus)
    node.say(wake_and_command())
    await node.wait_utterances(1)
    assert svc.tts.spoken[0] == P_BUS_DOWN and bus.executed == []


async def test_own_speech_is_ignored_while_speaker_plays(harness):
    """El micrófono "oye" la palabra de activación mientras suena el parlante: no debe disparar otra conversación."""
    bus = FakeBus()
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"], bus=bus)
    node.say(wake_and_command())
    # mientras suena la confirmación llega eco con la palabra de activación
    await asyncio.sleep(0.05)
    node.say(speech(120, marker=7777) * 3)
    await node.wait_utterances(1)
    assert len(svc.tts.spoken) == 1 and svc.stt.calls == 1


async def test_false_trigger_without_voice_goes_back_to_idle(harness):
    server, svc, node = await harness()
    node.say([speech_chunk(marker=7777)])  # disparo con ruido de 40 ms: no hay voz suficiente
    await asyncio.sleep(1.5)
    assert svc.tts.spoken == [] and server.sessions[FAKE_MAC].state.value == "idle"
    # y el sistema sigue vivo para un disparo real
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    assert svc.tts.spoken[0] in GREETINGS


# ------------------------------------------------------------------ handshake y enlaces
async def test_handshake_rejections(harness):
    server, svc, node = await harness()
    url = f"ws://127.0.0.1:{server.port}/ws"

    bad_token = FakeNode(token="otro")
    reply = await bad_token.connect(url)
    assert reply["type"] == "error" and "token" in reply["reason"]

    unknown = FakeNode(device_id="intruso", mac="AA:BB:CC:99:99:99")
    reply = await unknown.connect(url)
    assert reply["type"] == "error" and "no autorizado" in reply["reason"]

    with pytest.raises(InvalidStatus) as exc:
        await connect(f"ws://127.0.0.1:{server.port}/otra", compression=None)
    assert exc.value.response.status_code == 404


async def test_tcp_probe_without_hello_does_not_break_the_server(harness):
    server, svc, node = await harness()
    r, w = await asyncio.open_connection("127.0.0.1", server.port)  # sondeo TCP del ESP32
    w.close()
    await asyncio.sleep(0.2)
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)


async def test_same_device_reconnect_replaces_old_link_and_cancels_dialog(harness):
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"])
    node.say(wake_and_command())
    await node.wait_utterances(1)  # pidió confirmación; la conversación sigue abierta
    node2 = FakeNode()
    assert (await node2.connect(f"ws://127.0.0.1:{server.port}/ws")) == {"type": "ready"}
    await asyncio.wait_for(node.closed.wait(), 2)  # el enlace viejo se cerró
    sess = server.sessions[FAKE_MAC]
    assert sess.link.peer != "" and sess._conv is None and sess.state.value == "idle"
    node2.say(WAKE_ONLY)
    await node2.wait_utterances(1)
    await node2.close()


# ------------------------------------------------------------------ Bluetooth
async def test_bluetooth_link_handshake_audio_and_keepalive_through_same_pipeline():
    cfg = make_config()
    svc = make_services(cfg, llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"])
    server = EdgeServer(svc)  # sin arrancar WebSocket: solo el manejador de enlaces
    ser = FakeSerial()
    loop = asyncio.get_running_loop()
    link = BtLink(ser, loop, "FAKE-BT")
    task = asyncio.create_task(server.handle_link(link))

    ser.peer_json({"type": "hello", "deviceId": "alivia-mic-01", "mac": FAKE_MAC, "capabilities": ["microphone", "speaker"],
                   "micSampleRate": 16000, "playbackSampleRate": 16000, "bits": 16, "channels": 1, "frameSamples": 320, "token": "tok"})

    async def wait_frames(pred, timeout=3.0):
        async def _w():
            while True:
                frames = ser.peer_frames()
                hit = [f for f in frames if pred(f)]
                if hit:
                    return hit
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(_w(), timeout)

    await wait_frames(lambda f: f[0] == TYPE_TEXT and json.loads(f[1]) == {"type": "ready"})

    # PING del ESP32 -> el edge responde PONG con el mismo contenido (máx. 64 B)
    ser.peer_write(TYPE_PING, b"abc")
    await wait_frames(lambda f: f[0] == TYPE_PONG and f[1] == b"abc")

    # voz: la palabra de activación y luego silencio -> saludo por tramas de audio de <= 2048 B
    def send_audio(chunks):
        for c in chunks:
            ser.peer_write(TYPE_AUDIO, c)

    async def mic_until(pred, chunks, timeout=6.0):
        send_audio(chunks)

        async def _w():
            while True:
                ser.peer_write(TYPE_AUDIO, silence_chunk())
                if pred():
                    return
                await asyncio.sleep(0.004)
        await asyncio.wait_for(_w(), timeout)

    def utterance_events():
        return [json.loads(f[1]) for f in ser.peer_frames() if f[0] == TYPE_TEXT]

    await mic_until(lambda: any(e.get("type") == "audio_end" for e in utterance_events()), WAKE_ONLY)
    frames = ser.peer_frames()
    audio = [f[1] for f in frames if f[0] == TYPE_AUDIO]
    assert sum(len(a) for a in audio) == 5120 and all(len(a) <= 2048 and len(a) % 2 == 0 for a in audio)
    begin = next(e for e in utterance_events() if e.get("type") == "audio_begin")
    ser.peer_json({"type": "playback_done", "utteranceId": begin["utteranceId"]})
    await asyncio.sleep(0.15)
    assert svc.tts.spoken[0] in GREETINGS

    # fin de enlace: el puerto se cierra y la sesión lo detecta
    ser.close()
    await asyncio.wait_for(task, 3)
    assert server.sessions[FAKE_MAC].link is None


async def test_wifi_has_priority_over_bluetooth_for_same_device(harness):
    server, svc, node = await harness()  # conectado por WiFi
    ser = FakeSerial()
    link = BtLink(ser, asyncio.get_running_loop(), "FAKE-BT")
    task = asyncio.create_task(server.handle_link(link))
    ser.peer_json({"type": "hello", "deviceId": "alivia-mic-01", "mac": FAKE_MAC, "token": "tok",
                   "micSampleRate": 16000, "playbackSampleRate": 16000})

    async def _rejected():
        while True:
            msgs = [json.loads(f[1]) for f in ser.peer_frames() if f[0] == TYPE_TEXT]
            if msgs:
                return msgs[0]
            await asyncio.sleep(0.01)

    msg = await asyncio.wait_for(_rejected(), 2)
    assert msg["type"] == "error" and "preferente" in msg["reason"]
    assert server.wifi_alive(FAKE_MAC)
    await asyncio.wait_for(task, 2)
    node.say(WAKE_ONLY)  # el WiFi sigue funcionando
    await node.wait_utterances(1)


async def test_failover_wifi_down_then_bluetooth_takes_over(harness):
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"])
    await node.close()  # cae el WiFi
    for _ in range(100):
        if not server.wifi_alive(FAKE_MAC):
            break
        await asyncio.sleep(0.02)
    assert not server.wifi_alive(FAKE_MAC)
    ser = FakeSerial()
    link = BtLink(ser, asyncio.get_running_loop(), "FAKE-BT")
    task = asyncio.create_task(server.handle_link(link))
    ser.peer_json({"type": "hello", "deviceId": "alivia-mic-01", "mac": FAKE_MAC, "token": "tok",
                   "micSampleRate": 16000, "playbackSampleRate": 16000})
    for _ in range(200):
        if any(f[0] == TYPE_TEXT and json.loads(f[1]).get("type") == "ready" for f in ser.peer_frames()):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Bluetooth no recibió ready tras caer el WiFi")
    assert server.sessions[FAKE_MAC].link.kind == "bt"
    ser.close()
    await asyncio.wait_for(task, 3)


# ------------------------------------------------------------------ autorización por la tabla del backend
from alivia_edge.access.domain.entities import NodeRef  # noqa: E402
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress  # noqa: E402
from alivia_edge.commands.infrastructure.repositories import SqliteCommandHistoryRepository  # noqa: E402
from alivia_edge.voice.application.dialog import P_NO_DEVICES  # noqa: E402

from .fakes import sample_table  # noqa: E402


def table_with(**status_by_id):
    """sample_table con otro estado para algunos dispositivos, p. ej. table_with(d1=AssetStatus.MAINTENANCE)."""
    base = sample_table(version=2)
    nodes = tuple(
        NodeRef(n.device_id, n.mac, n.device_type, status_by_id.get(f"d{n.device_id}", n.status), n.room_key, n.function)
        for n in base.nodes
    )
    return type(base)(base.installation_id, base.household_id, base.version, nodes)


async def reject_reason(server, **node_kwargs):
    node = FakeNode(**node_kwargs)
    reply = await node.connect(f"ws://127.0.0.1:{server.port}/ws")
    assert reply["type"] == "error"
    return reply["reason"]


async def test_hello_without_mac_is_rejected(harness):
    server, svc, node = await harness()
    url = f"ws://127.0.0.1:{server.port}/ws"
    from websockets.asyncio.client import connect as ws_connect

    ws = await ws_connect(url, compression=None, ping_interval=None)
    await ws.send(json.dumps({"type": "hello", "deviceId": "alivia-mic-01", "token": "tok",
                              "micSampleRate": 16000, "playbackSampleRate": 16000}))
    reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
    assert reply["type"] == "error" and "MAC" in reply["reason"]
    await ws.close()


async def test_device_not_in_table_or_not_a_microphone_is_denied(harness):
    server, svc, node = await harness()
    assert "no registrado" in await reject_reason(server, mac="AA:BB:CC:99:99:99")
    assert "no es un micrófono" in await reject_reason(server, mac="AA:BB:CC:00:00:10")  # una puerta no manda audio


async def test_mic_in_maintenance_is_denied_and_label_does_not_matter(harness):
    server, svc, node = await harness()
    await node.close()
    svc.access.apply(table_with(d1=AssetStatus.MAINTENANCE))
    assert "mantenimiento" in await reject_reason(server)
    svc.access.apply(sample_table(version=3))  # vuelve a IN_USE
    other_label = FakeNode(device_id="cualquier-cosa")  # la identidad es la MAC, no la etiqueta del sketch
    assert (await other_label.connect(f"ws://127.0.0.1:{server.port}/ws")) == {"type": "ready"}
    await other_label.close()


async def test_no_table_denies_everyone_until_the_first_one_arrives():
    from .fakes import make_access

    cfg = make_config()
    svc = make_services(cfg, llm_script=[], stt_script=[], access=make_access(False))
    server = EdgeServer(svc)
    await server.start()
    try:
        assert "sin tabla" in await reject_reason(server)
        svc.access.apply(sample_table())  # llega la primera tabla del backend
        node = FakeNode()
        assert (await node.connect(f"ws://127.0.0.1:{server.port}/ws")) == {"type": "ready"}
        await node.close()
    finally:
        await server.stop()


async def test_table_change_cuts_a_connected_device_that_lost_access(harness):
    server, svc, node = await harness()
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)  # sesión viva y funcionando
    svc.access.apply(table_with(d1=AssetStatus.BROKEN))  # el administrador lo marca averiado
    await asyncio.wait_for(node.closed.wait(), 2)
    assert any(m.get("type") == "error" and m["reason"] == "acceso revocado" for m in node.messages)
    assert "averiado" in await reject_reason(server)  # y no puede volver a entrar
    # un cambio que no le afecta no corta a nadie
    svc.access.apply(sample_table(version=9))
    assert (await FakeNode().connect(f"ws://127.0.0.1:{server.port}/ws")) == {"type": "ready"}


async def test_room_of_the_microphone_reaches_the_llm(harness):
    bus = FakeBus()
    server, svc, node = await harness(llm_script=[llm_json("ok", [("window_12", "cerrar")])], stt_script=["cierra la ventana"], bus=bus)
    node.say(wake_and_command())
    await node.wait_utterances(1)
    assert svc.llm.rooms == ["la sala"]  # el micrófono 1 está en "sala" según la tabla
    assert svc.tts.spoken[0] == "¿Quiere que cierre la ventana de la sala?"


async def test_actuator_that_lost_access_is_denied_at_execution_time(harness):
    """Se confirma la orden y, antes de ejecutar, el backend manda la ventana a mantenimiento: NO se publica nada."""
    bus = FakeBus()
    server, svc, node = await harness(
        llm_script=[llm_json("ok", [("window_12", "cerrar"), ("light_11", "apagar")])],
        stt_script=["cierra la ventana y apaga la luz"], bus=bus,
    )
    node.say(wake_and_command())
    await node.wait_utterances(1)  # pidió confirmación
    svc.access.apply(table_with(d12=AssetStatus.MAINTENANCE))
    node.say(speech(600, marker=YES_MARK))
    await node.wait_utterances(2)
    assert bus.executed == [[Action("light_11", "apagar")]]  # solo la luz llegó al bus
    assert svc.tts.spoken[1] == "La luz de la sala quedó apagada, pero no tengo permiso para cerrar la ventana de la sala."


async def test_no_devices_in_the_table_tells_the_user(harness):
    server, svc, node = await harness()
    svc.access.apply(type(sample_table())(7, 3, 5, tuple(n for n in sample_table().nodes if n.device_type is DeviceType.MICROPHONE)))
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    assert svc.tts.spoken == [P_NO_DEVICES]


async def test_dispatcher_records_history_with_the_microphone_mac(tmp_path):
    from alivia_edge.commands.application.dispatcher import CommandDispatcher
    from alivia_edge.commands.infrastructure.models import CommandRecordModel
    from alivia_edge.commands.integration.access_acl import AccessAntiCorruptionLayer
    from alivia_edge.shared.infrastructure.database import close_db, init_db

    from .fakes import make_access

    init_db(tmp_path / "h.db", [CommandRecordModel])
    try:
        access = make_access()
        dispatcher = CommandDispatcher(FakeBus(), AccessAntiCorruptionLayer(access), SqliteCommandHistoryRepository())
        res = await dispatcher.execute([Action("door_10", "cerrar"), Action("door_999", "abrir")], node=FAKE_MAC, timeout_s=1)
        assert [r.status for r in res] == ["ok", "denied"]
        rows = SqliteCommandHistoryRepository().recent()
        assert {(r.node, r.device_id, r.action, r.status) for r in rows} == {
            (FAKE_MAC, 10, "cerrar", "ok"), (FAKE_MAC, 999, "abrir", "denied")}
    finally:
        close_db()


async def test_whisper_receives_vocabulary_of_the_real_devices(harness):
    server, svc, node = await harness(llm_script=[llm_json("ok", [("door_10", "cerrar")])], stt_script=["cierra la puerta"])
    node.say(WAKE_ONLY)
    await node.wait_utterances(1)
    node.say(speech(1200))
    await node.wait_utterances(2)
    hint = svc.stt.prompts[0]
    assert hint and "la puerta de la entrada" in hint and "la luz de la sala" in hint
    assert len(hint) <= 280
