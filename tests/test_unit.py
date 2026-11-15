import asyncio
import json
import os
import time

import numpy as np
import pytest

from alivia_edge.voice.infrastructure.audio.pcm import resample_int16_to_16k
from alivia_edge.voice.infrastructure.audio.speaker import SpeakerController
from alivia_edge.voice.infrastructure.audio.vad import Endpointer, Recorder, SileroVad
from alivia_edge.shared.config import SpeakerCfg, VadCfg, load_config
from alivia_edge.shared.domain.text_es import classify_answer, is_meaningful, join_es, normalize, strip_wake
from alivia_edge.commands.domain.catalog import Catalog
from alivia_edge.commands.domain.models import ActuatorTarget
from alivia_edge.commands.infrastructure.llm import build_schema, build_system_prompt, parse_interpretation
from alivia_edge.commands.domain.models import Action, ActionResult
from alivia_edge.voice.interfaces.protocol import HandshakeError, parse_hello
from alivia_edge.voice.interfaces.transports.base import Link
from alivia_edge.voice.interfaces.transports.frames import MAX_PAYLOAD, TYPE_AUDIO, TYPE_PING, TYPE_TEXT, FrameDecoder, crc16, encode

from .fakes import FakeVad, llm_json, silence_chunk, speech_chunk


# ------------------------------------------------------------------ tramas Bluetooth
def test_crc16_ccitt_false_check_value():
    assert crc16(b"123456789") == 0x29B1


def test_frame_roundtrip_and_layout():
    f = encode(TYPE_TEXT, b'{"a":1}')
    assert f[0] == 0xA7 and f[1] == TYPE_TEXT and f[2] == 7 and f[3] == 0
    assert f[4] == 0xA7 ^ TYPE_TEXT ^ 7 ^ 0
    assert FrameDecoder().feed(f) == [(TYPE_TEXT, b'{"a":1}')]


def test_decoder_resyncs_after_garbage_and_bad_crc_and_split_reads():
    good1, good2 = encode(TYPE_AUDIO, bytes(range(200))), encode(TYPE_PING, b"x")
    corrupt = bytearray(encode(TYPE_TEXT, b"hola"))
    corrupt[-1] ^= 0xFF
    stream = b"\x00\xa7\xa7\x01junk" + bytes(corrupt) + good1 + b"\xa7" + good2
    dec = FrameDecoder()
    out = []
    for i in range(0, len(stream), 7):  # lecturas fragmentadas
        out += dec.feed(stream[i : i + 7])
    assert out == [(TYPE_AUDIO, bytes(range(200))), (TYPE_PING, b"x")]
    assert dec.bad_crc >= 1


def test_encode_rejects_oversize():
    with pytest.raises(ValueError):
        encode(TYPE_AUDIO, bytes(MAX_PAYLOAD + 1))
    assert len(FrameDecoder().feed(encode(TYPE_AUDIO, bytes(MAX_PAYLOAD)))[0][1]) == MAX_PAYLOAD


# ------------------------------------------------------------------ hello
def test_parse_hello():
    h = parse_hello({"type": "hello", "deviceId": "x", "mac": "aa:bb:cc:00:00:01", "token": "t", "micSampleRate": 16000, "playbackSampleRate": 16000})
    assert h.device_id == "x" and h.token == "t" and h.mac == "aa:bb:cc:00:00:01"
    for bad in ({"type": "nope"}, {"type": "hello", "deviceId": ""}, {"type": "hello", "deviceId": "x"},  # sin MAC
                {"type": "hello", "deviceId": "x", "mac": "AA:BB:CC:00:00:01", "micSampleRate": 8000}):
        with pytest.raises(HandshakeError):
            parse_hello(bad)


# ------------------------------------------------------------------ texto
@pytest.mark.parametrize(
    "text,kind",
    [("Sí", "yes"), ("sí, claro", "yes"), ("Por favor, sí", "yes"), ("de acuerdo", "yes"), ("No", "no"),
     ("no gracias", "no"), ("cancela", "no"), ("", "none"), ("la ventana", "other"), ("sí, y también la luz", "other")],
)
def test_classify_answer(text, kind):
    assert classify_answer(text).kind == kind


def test_classify_no_with_correction():
    assert classify_answer("no, la ventana").has_content
    assert not classify_answer("no, gracias").has_content


@pytest.mark.parametrize(
    "text,expected",
    [("Oye Alivia, cierra la puerta", "cierra la puerta"), ("Olivia apaga la luz", "apaga la luz"),
     ("Alivía, ciérrame todo", "ciérrame todo"), ("hey alivia", ""), ("cierra la puerta", "cierra la puerta"),
     ("Ey Alibia: abre la ventana", "abre la ventana")],
)
def test_strip_wake(text, expected):
    assert strip_wake(text, ["alivia", "olivia", "alibia"]) == expected


def test_helpers():
    assert normalize("¡Ciérrame TODO, por favor!") == "cierrame todo por favor"
    assert is_meaningful("cierra") and not is_meaningful("eh")
    assert join_es(["a", "b", "c"]) == "a, b y c" and join_es(["a"]) == "a"


# ------------------------------------------------------------------ catálogo e interpretación
@pytest.fixture
def catalog():
    targets = [
        ActuatorTarget(10, "AA:BB:CC:00:00:10", "DOOR", "entrada", "puerta principal"),
        ActuatorTarget(11, "AA:BB:CC:00:00:11", "LIGHT", "sala", "luz del techo"),
        ActuatorTarget(12, "AA:BB:CC:00:00:12", "WINDOW", "sala", ""),
        ActuatorTarget(13, "AA:BB:CC:00:00:13", "CURTAIN", "sala", ""),
    ]
    return Catalog.from_targets(targets, {"entrada": "la entrada", "sala": "la sala"}, key=(7, 1))


def test_catalog_phrases(catalog):
    acts = [Action("door_10", "cerrar"), Action("light_11", "apagar"), Action("window_12", "cerrar")]
    assert catalog.describe(acts) == "cierre la puerta de la entrada, apague la luz de la sala y cierre la ventana de la sala"
    ok = [ActionResult(a, "ok") for a in acts]
    assert catalog.summarize(ok) == "Listo. La puerta de la entrada quedó cerrada, la luz de la sala quedó apagada y la ventana de la sala quedó cerrada."
    mixed = [ActionResult(acts[0], "ok"), ActionResult(acts[1], "fail"), ActionResult(acts[2], "timeout")]
    assert catalog.summarize(mixed) == (
        "La puerta de la entrada quedó cerrada, pero no pude apagar la luz de la sala y no recibí confirmación de cerrar la ventana de la sala."
    )
    assert catalog.summarize([ActionResult(acts[1], "fail")]) == "Lo siento, no pude apagar la luz de la sala."


def test_parse_scene_expands_and_dedupes(catalog):
    i = parse_interpretation(llm_json("ok", [("door_10", "cerrar")], scene="todo"), catalog)
    assert i.status == "ok"
    assert i.actions == [Action("door_10", "cerrar"), Action("light_11", "apagar"), Action("window_12", "cerrar")]


def test_parse_drops_actions_outside_catalog(catalog):
    i = parse_interpretation(llm_json("ok", [("garaje", "abrir"), ("light_11", "abrir")]), catalog)  # luz no tiene "abrir"
    assert i.status == "unknown" and i.actions == []
    i = parse_interpretation(llm_json("ok", [("garaje", "abrir"), ("light_11", "encender")]), catalog)
    assert i.actions == [Action("light_11", "encender")]


def test_parse_conflicting_actions_ask_instead_of_guess(catalog):
    i = parse_interpretation(llm_json("ok", [("door_10", "abrir"), ("door_10", "cerrar")]), catalog)
    assert i.status == "clarify" and i.actions == [] and "abrir o cerrar la puerta" in i.question


def test_parse_status_rules(catalog):
    assert parse_interpretation(llm_json("unknown", [("light_11", "apagar")]), catalog).actions == []
    assert parse_interpretation(llm_json("clarify"), catalog).status == "unknown"
    i = parse_interpretation(llm_json("clarify", question="¿Qué desea hacer con la luz?"), catalog)
    assert i.status == "clarify" and i.question
    i = parse_interpretation(llm_json("clarify", [("door_10", "abrir")]), catalog)
    assert i.status == "clarify" and i.actions == [Action("door_10", "abrir")]
    with pytest.raises(ValueError):
        parse_interpretation("no es json", catalog)
    with pytest.raises(ValueError):
        parse_interpretation("[1,2]", catalog)


def test_schema_and_prompt_follow_catalog(catalog):
    schema = build_schema(catalog)
    props = schema["properties"]
    assert props["actions"]["items"]["properties"]["device"]["enum"] == ["door_10", "light_11", "window_12", "curtain_13"]
    assert props["actions"]["items"]["properties"]["action"]["enum"] == ["abrir", "apagar", "cerrar", "encender"]
    assert props["scene"]["enum"] == ["", "todo"]
    prompt = build_system_prompt(catalog, "la sala")
    assert "id=door_10" in prompt and '"todo"' in prompt and "la sala" in prompt
    assert "ya me voy a descansar" in prompt


# ------------------------------------------------------------------ config
def test_load_config(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[server]\nport = 9000\n[wake]\nbogus = 1\n[rooms.labels]\nsala = "la sala"\n', encoding="utf-8")
    monkeypatch.delenv("ALIVIA_WS_TOKEN", raising=False)
    monkeypatch.delenv("ALIVIA_EDGE_CREDENTIAL", raising=False)
    with pytest.raises(ValueError, match="token"):
        load_config(p)
    monkeypatch.setenv("ALIVIA_WS_TOKEN", "secreto")
    with pytest.raises(ValueError, match="credencial"):
        load_config(p)
    monkeypatch.setenv("ALIVIA_EDGE_CREDENTIAL", "5.abc")
    cfg = load_config(p)
    assert cfg.server.port == 9000 and cfg.server.token == "secreto" and cfg.backend.credential == "5.abc"
    assert cfg.room_labels == {"sala": "la sala"}
    assert cfg.resolve("models/x") == str(tmp_path / "models/x")


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("ALIVIA_WS_TOKEN", "t")
    monkeypatch.setenv("ALIVIA_EDGE_CREDENTIAL", "1.secreto")
    cfg = load_config("config.example.toml")
    assert cfg.bluetooth.links[0].mac == "AA:BB:CC:00:00:01" and cfg.bluetooth.links[0].port == "COM5"
    assert cfg.backend.wait_s == 25 and cfg.room_labels["sala"] == "la sala"


# ------------------------------------------------------------------ audio
def test_resample_22050_to_16000_length_and_tone():
    sr = 22050
    t = np.arange(sr) / sr
    tone = (np.sin(2 * np.pi * 440 * t) * 16000).astype("<i2")
    out = np.frombuffer(resample_int16_to_16k(tone, sr), "<i2")
    assert abs(len(out) - 16000) <= 2
    spectrum = np.abs(np.fft.rfft(out.astype(np.float32)))
    assert abs(np.argmax(spectrum) - 440) <= 2  # 1 s de audio -> 1 Hz por bin


def feed_endpointer(ep, chunks):
    for c in chunks:
        ep.feed(c)


def test_endpointer_detects_end_of_phrase():
    ep = Endpointer(FakeVad(), VadCfg(), end_silence_ms=700)
    feed_endpointer(ep, [silence_chunk()] * 10)
    assert not ep.speech_started and not ep.ended
    feed_endpointer(ep, [speech_chunk()] * 25)
    assert ep.speech_started and not ep.ended
    feed_endpointer(ep, [silence_chunk()] * 10)  # 400 ms
    assert not ep.ended
    feed_endpointer(ep, [silence_chunk()] * 12)  # >700 ms
    assert ep.ended


def test_endpointer_ignores_clicks_shorter_than_min_speech():
    ep = Endpointer(FakeVad(), VadCfg(min_speech_ms=200), end_silence_ms=700)
    feed_endpointer(ep, [speech_chunk()])  # 40 ms
    feed_endpointer(ep, [silence_chunk()] * 40)
    assert not ep.speech_started


async def test_recorder_timeouts_and_max():
    def source(chunks):
        it = iter(chunks)

        async def get():
            return next(it)

        return get

    rec = Recorder(FakeVad(), VadCfg())
    # sin voz: vence start_timeout
    r = await rec.record(source([silence_chunk()] * 200), start_timeout_s=1.0, max_s=5.0, end_silence_ms=700)
    assert r is None
    # voz continua: vence max_s
    r = await rec.record(source([speech_chunk()] * 400), start_timeout_s=1.0, max_s=2.0, end_silence_ms=700)
    assert r is not None and r.ended_by == "max" and 1.9 < len(r.pcm) / 32000 < 2.2
    # voz y silencio: termina por silencio, sin cortar la voz
    r = await rec.record(source([speech_chunk()] * 20 + [silence_chunk()] * 40), start_timeout_s=1.0, max_s=5.0, end_silence_ms=700)
    assert r is not None and r.ended_by == "silence" and r.speech_ms >= 700
    # prefijo con voz: cuenta como voz previa y post_prefix_speech_ms solo lo posterior
    prefix = speech_chunk() * 10
    r = await rec.record(source([silence_chunk()] * 40), prefix=prefix, start_timeout_s=1.0, max_s=5.0, end_silence_ms=500)
    # (<= 32 ms: la media ventana de voz que sobra del prefijo se completa con el primer cuadro nuevo)
    assert r is not None and r.post_prefix_speech_ms <= 32 and r.speech_ms >= 300


@pytest.mark.skipif(not os.path.exists("models/silero_vad.onnx"), reason="falta models/silero_vad.onnx")
def test_silero_vad_model_runs_and_rejects_silence():
    vad = SileroVad("models/silero_vad.onnx")
    probs = [vad.prob(np.zeros(512, np.float32)) for _ in range(20)]
    assert max(probs) < 0.1
    rng = np.random.default_rng(0)
    p = vad.prob((rng.standard_normal(512) * 0.2).astype(np.float32))
    assert 0.0 <= p <= 1.0
    vad.reset()
    ep = Endpointer(vad, VadCfg(), 700)
    ep.feed(bytes(32000))  # 1 s de silencio real por el modelo real
    assert not ep.speech_started


# ------------------------------------------------------------------ pacing del parlante
class RecordingLink(Link):
    kind = "ws"
    peer = "test"

    def __init__(self):
        self.sent: list[tuple[float, str, object]] = []
        self.t0 = time.monotonic()
        self._closed = False

    closed = property(lambda self: self._closed)

    async def recv(self):
        await asyncio.sleep(3600)

    async def send_json(self, obj):
        self.sent.append((time.monotonic() - self.t0, "json", obj))

    async def send_audio(self, pcm):
        self.sent.append((time.monotonic() - self.t0, "audio", len(pcm)))

    async def close(self, reason=""):
        self._closed = True


async def test_speaker_paces_burst_then_realtime_and_mutes_mic():
    unmuted = []
    sp = SpeakerController(SpeakerCfg(lead_ms=300, tail_ms=20, done_margin_s=0.5), lambda: unmuted.append(1))
    link = RecordingLink()
    sp.attach(link)
    pcm = bytes(32000 * 1)  # 1 s de audio
    task = asyncio.create_task(sp.play(pcm))
    await asyncio.sleep(0.05)
    assert sp.muted
    begin = link.sent[0][2]
    assert begin["type"] == "audio_begin" and begin["sampleRate"] == 16000 and len(begin["utteranceId"]) <= 39
    await asyncio.sleep(1.2)  # el audio ya se envió en ~0.7 s; ahora llega playback_done
    sp.on_playback_done("otro-id")  # id ajeno: se ignora
    assert not task.done()
    sp.on_playback_done(begin["utteranceId"])
    await asyncio.wait_for(task, 1.0)
    assert not sp.muted and unmuted == [1]

    audio = [(t, n) for t, kind, n in link.sent if kind == "audio"]
    assert sum(n for _, n in audio) == len(pcm) and all(n % 2 == 0 and n <= 2048 for _, n in audio)
    # ráfaga: los primeros ~300 ms de audio salen casi de golpe...
    assert sum(n for t, n in audio if t < 0.03) >= 9600
    # ...y luego nunca se va más de lead_ms (+ un cuadro de 60 ms) por delante del tiempo real
    cum = 0
    for t, n in audio:
        cum += n
        assert cum / 32000 - t <= 0.3 + 0.06 + 0.03, (t, cum)
    # el envío completo dura ~ (1 s - 0.3 s de ráfaga) ≈ 0.7 s
    assert 0.55 <= audio[-1][0] <= 0.9
    end = [o for _, kind, o in link.sent if kind == "json"][-1]
    assert end["type"] == "audio_end" and end["utteranceId"] == begin["utteranceId"]


async def test_speaker_assumes_done_when_playback_done_is_lost():
    sp = SpeakerController(SpeakerCfg(tail_ms=10, done_margin_s=0.3), lambda: None)
    sp.attach(RecordingLink())
    t0 = time.monotonic()
    await sp.play(bytes(3200))  # 100 ms
    assert 0.3 <= time.monotonic() - t0 <= 0.7 and not sp.muted
