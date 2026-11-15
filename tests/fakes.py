"""Dobles de prueba: modelos falsos y un "ESP32" falso que habla el protocolo real por WebSocket/Bluetooth."""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from alivia_edge.access.application.services import AccessService
from alivia_edge.access.domain.entities import EdgeAccessTable, NodeRef
from alivia_edge.access.domain.value_objects import AssetStatus, DeviceType, MacAddress
from alivia_edge.app import Services
from alivia_edge.commands.application.catalog_service import CatalogService
from alivia_edge.commands.application.dispatcher import CommandDispatcher
from alivia_edge.commands.infrastructure.llm import parse_interpretation
from alivia_edge.commands.integration.access_acl import AccessAntiCorruptionLayer
from alivia_edge.commands.domain.models import ActionResult
from alivia_edge.shared.config import Config, DialogCfg, ServerCfg, SpeakerCfg, WakeCfg
from alivia_edge.voice.domain.ports import WakeResult
from alivia_edge.voice.integration.access_acl import MicrophoneAccessGate
from alivia_edge.voice.interfaces.transports.frames import (
    TYPE_AUDIO, TYPE_PING, TYPE_PONG, TYPE_TEXT, FrameDecoder, encode,
)

FAKE_MAC = "AA:BB:CC:00:00:01"
ROOM_LABELS = {"sala": "la sala", "entrada": "la entrada"}


class MemoryAccessRepository:
    def __init__(self):
        self.table = None

    def load(self):
        return self.table

    def save(self, table):
        self.table = table

    def clear(self):
        self.table = None


def _node(device_id, mac_tail, kind, room, status=AssetStatus.IN_USE, function="f"):
    return NodeRef(device_id, MacAddress(f"AA:BB:CC:00:00:{mac_tail:02X}"), kind, status, room, function)


def sample_table(version=1, installation=7, extra=()):
    """Hogar de pruebas: micrófono 1 (sala), puerta 10 (entrada), luz 11 y ventana 12 (sala)."""
    nodes = [
        _node(1, 1, DeviceType.MICROPHONE, "sala"),
        _node(10, 0x10, DeviceType.DOOR, "entrada"),
        _node(11, 0x11, DeviceType.LIGHT, "sala"),
        _node(12, 0x12, DeviceType.WINDOW, "sala"),
        *extra,
    ]
    return EdgeAccessTable(installation, 3, version, tuple(nodes))


CHUNK_SAMPLES = 640  # 40 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2
WAKE_MARK = 7777
YES_MARK = 1111
NO_MARK = 2222


def speech_chunk(marker: int | None = None) -> bytes:
    t = np.arange(CHUNK_SAMPLES)
    x = (np.sin(2 * np.pi * 300 * t / 16000) * 8000).astype("<i2")
    if marker is not None:
        x[0] = marker
    return x.tobytes()


def silence_chunk() -> bytes:
    return bytes(CHUNK_BYTES)


def speech(ms: int, marker: int | None = None) -> list[bytes]:
    n = max(1, ms // 40)
    return [speech_chunk(marker if i == 0 else None) for i in range(n)]


# ------------------------------------------------------------------ modelos falsos
class FakeVad:
    def prob(self, window):
        return 1.0 if float(np.max(np.abs(window))) > 0.05 else 0.0

    def reset(self):
        pass


class FakeWake:
    conf = 0.9

    def feed(self, pcm):
        return int(np.frombuffer(pcm[:2], "<i2")[0]) == WAKE_MARK

    def finish(self):
        return WakeResult("alivia", self.conf)

    def reset(self):
        pass


class FakeYesNo:
    def recognize(self, pcm):
        # la grabación incluye silencio previo: se busca la marca en el primer sample de cada cuadro de 40 ms
        for off in range(0, len(pcm), CHUNK_BYTES):
            first = int(np.frombuffer(pcm[off : off + 2], "<i2")[0])
            if first in (YES_MARK, NO_MARK):
                return "yes" if first == YES_MARK else "no"
        return None


class FakeStt:
    def __init__(self, script):
        self.script = deque(script)
        self.calls = 0
        self.prompts: list[str | None] = []

    async def transcribe(self, pcm, *, prompt=None):
        self.calls += 1
        self.prompts.append(prompt)
        return self.script.popleft() if self.script else ""


class FakeTts:
    def __init__(self):
        self.spoken: list[str] = []

    async def synth(self, text):
        self.spoken.append(text)
        return bytes(5120)  # 160 ms (menos que la ráfaga: sin pacing en pruebas)


class FakeLlm:
    """Devuelve JSON guionado, validado con el parser real contra el catálogo real."""

    def __init__(self, script):
        self.script = deque(script)
        self.histories: list[list[dict]] = []
        self.rooms: list[str] = []

    async def interpret(self, history, room, catalog):
        self.histories.append([dict(m) for m in history])
        self.rooms.append(room)
        return parse_interpretation(self.script.popleft(), catalog)


class FakeBus:
    def __init__(self, connected=True, status="ok"):
        self.connected = connected
        self.status = status
        self.executed = []

    async def execute(self, requests, *, node, timeout_s):
        self.executed.append([r.action for r in requests])
        self.requests = list(requests)
        return [ActionResult(r.action, self.status) for r in requests]


def make_config() -> Config:
    cfg = Config()
    cfg.server = ServerCfg(host="127.0.0.1", port=0, token="tok", hello_timeout_s=1.0, idle_timeout_s=5.0)
    cfg.room_labels = dict(ROOM_LABELS)
    cfg.speaker = SpeakerCfg(lead_ms=300, chunk_bytes=1920, tail_ms=30, done_margin_s=0.5)
    cfg.dialog = DialogCfg(order_start_timeout_s=1.0, answer_start_timeout_s=1.5, answer_max_s=3.0, order_window_s=4.0)
    cfg.wake = WakeCfg(capture_start_timeout_s=1.0, capture_end_silence_ms=500, capture_max_s=5.0)
    cfg.audio.mic_stall_s = 2.0
    return cfg


def make_access(table=None):
    access = AccessService(MemoryAccessRepository())
    if table is not False:
        access.apply(table or sample_table())
    return access


def make_services(cfg, *, llm_script, stt_script, bus=None, table=None, access=None):
    access = access or make_access(table)
    acl = AccessAntiCorruptionLayer(access)
    bus = bus or FakeBus()
    return Services(
        cfg=cfg,
        access=access,
        gate=MicrophoneAccessGate(access),
        catalogs=CatalogService(acl, cfg.room_labels),
        dispatcher=CommandDispatcher(bus, acl),
        llm=FakeLlm(llm_script),
        stt=FakeStt(stt_script),
        tts=FakeTts(),
        bus=bus,
        new_wake=FakeWake,
        new_yesno=FakeYesNo,
        new_vad=FakeVad,
        executor=ThreadPoolExecutor(1),
    )


def llm_json(status="ok", actions=(), scene="", question=""):
    return json.dumps(
        {"status": status, "scene": scene, "actions": [{"device": d, "action": a} for d, a in actions], "question": question}
    )


# ------------------------------------------------------------------ ESP32 falso (WebSocket)
class FakeNode:
    """Habla el protocolo real: hello/ready, mic continuo, audio_begin/PCM/audio_end -> playback_done."""

    def __init__(self, device_id="alivia-mic-01", token="tok", mac=FAKE_MAC):
        self.device_id, self.token, self.mac = device_id, token, mac
        self.ws = None
        self.utterances: list[dict] = []  # {"id", "bytes", "done": bool}
        self.messages: list[dict] = []
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._cur = None
        self.closed = asyncio.Event()

    async def connect(self, url):
        from websockets.asyncio.client import connect

        self.ws = await connect(url, compression=None, ping_interval=None)
        await self.ws.send(json.dumps({
            "type": "hello", "deviceId": self.device_id, "mac": self.mac, "capabilities": ["microphone", "speaker"],
            "micSampleRate": 16000, "playbackSampleRate": 16000, "bits": 16, "channels": 1,
            "frameSamples": 320, "token": self.token,
        }))
        first = json.loads(await asyncio.wait_for(self.ws.recv(), 2))
        if first.get("type") != "ready":
            return first
        self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._feeder())]
        return first

    async def _reader(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    assert len(msg) % 2 == 0 and len(msg) <= 2048, "audio inválido hacia el parlante"
                    if self._cur is not None:
                        self._cur["bytes"] += len(msg)
                    continue
                m = json.loads(msg)
                self.messages.append(m)
                if m["type"] == "audio_begin":
                    assert m["sampleRate"] == 16000 and len(m["utteranceId"]) <= 39
                    self._cur = {"id": m["utteranceId"], "bytes": 0, "done": False}
                    self.utterances.append(self._cur)
                elif m["type"] == "audio_end" and self._cur and m["utteranceId"] == self._cur["id"]:
                    cur, self._cur = self._cur, None
                    asyncio.get_running_loop().call_later(0.02, lambda c=cur: asyncio.create_task(self._done(c)))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.closed.set()

    async def _done(self, cur):
        try:
            await self.ws.send(json.dumps({"type": "playback_done", "utteranceId": cur["id"]}))
        except Exception:  # noqa: BLE001
            pass
        cur["done"] = True

    async def _feeder(self):
        try:
            while True:
                try:
                    chunk, delay = self._out.get_nowait(), 0.004  # frases a 10x tiempo real
                except asyncio.QueueEmpty:
                    chunk, delay = silence_chunk(), 0.02  # silencio a 2x: las ventanas de espera del servidor no vencen antes de que el test hable
                await self.ws.send(chunk)
                await asyncio.sleep(delay)
        except Exception:  # noqa: BLE001
            pass

    def say(self, chunks):
        for c in chunks:
            self._out.put_nowait(c)

    async def wait_utterances(self, n, timeout=8.0):
        """Espera n enunciados completos (con playback_done enviado) y deja pasar la cola anti-eco."""
        async def _w():
            while not (len(self.utterances) >= n and all(u["done"] for u in self.utterances[:n])):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_w(), timeout)
        await asyncio.sleep(0.12)

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.ws:
            await self.ws.close()


WAKE_ONLY = speech(280) + speech(40, marker=WAKE_MARK)  # "alivia" y luego silencio


def wake_and_command(command_ms=1200):
    return speech(280) + speech(40, marker=WAKE_MARK) + speech(command_ms)


# ------------------------------------------------------------------ puerto serie falso (Bluetooth)
class FakeSerial:
    """pyserial en memoria: el 'ESP32' escribe con peer_write() y lee lo que el edge escribió con peer_frames()."""

    def __init__(self):
        self._rx = bytearray()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.tx = bytearray()
        self.closed = False

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._rx)

    def read(self, n=1):
        with self._cv:
            if not self._rx and not self.closed:
                self._cv.wait(0.05)
            if self.closed:
                raise OSError("puerto cerrado")
            data = bytes(self._rx[:n])
            del self._rx[:n]
            return data

    def write(self, data):
        with self._lock:
            self.tx += data
        return len(data)

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    # lado "ESP32"
    def peer_write(self, ftype, payload=b""):
        with self._cv:
            self._rx += encode(ftype, payload)
            self._cv.notify_all()

    def peer_json(self, obj):
        self.peer_write(TYPE_TEXT, json.dumps(obj).encode())

    def peer_frames(self):
        with self._lock:
            dec = FrameDecoder()
            return dec.feed(bytes(self.tx))
