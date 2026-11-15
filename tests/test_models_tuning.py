"""Integridad de modelos, ajustes de hardware/STT, y herramientas de conjunto de datos y evaluación del LLM."""
from __future__ import annotations

import asyncio
import json
import os
import random
import types
import zipfile
from pathlib import Path

import httpx
import pytest

from alivia_edge.app import _check_ollama_model, _integrity_problems
from alivia_edge.commands.domain.catalog import Catalog, with_de
from alivia_edge.commands.domain.errors import LlmError
from alivia_edge.commands.domain.models import ActuatorTarget
from alivia_edge.commands.infrastructure.llm import OllamaLlm, build_system_prompt, parse_interpretation
from alivia_edge.shared.config import Config, LlmCfg, SecurityCfg
from alivia_edge.shared.infrastructure import hardware
from alivia_edge.shared.infrastructure import integrity as ig
from alivia_edge.tuning import dataset, handwritten
from alivia_edge.tuning import evaluate as ev
from alivia_edge.tuning.households import Household, fixed_household, random_household
from alivia_edge.voice.infrastructure.audio.stt import echoes_prompt

REPO = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ integridad
def test_file_hash_verification_detects_tampering(tmp_path):
    (tmp_path / "m.onnx").write_bytes(b"modelo")
    e = ig.Entry("m", "file", "m.onnx", sha256=ig.sha256_file(tmp_path / "m.onnx"))
    assert ig.verify_entry(tmp_path, e).status == "ok"
    (tmp_path / "m.onnx").write_bytes(b"modelo alterado")
    c = ig.verify_entry(tmp_path, e)
    assert c.status == "mismatch" and c.blocking


def test_missing_and_unpinned_are_not_blocking(tmp_path):
    assert ig.verify_entry(tmp_path, ig.Entry("x", "file", "no.onnx", sha256="aa")).status == "missing"
    (tmp_path / "a.bin").write_bytes(b"1")
    c = ig.verify_entry(tmp_path, ig.Entry("a", "file", "a.bin"))
    assert c.status == "unpinned" and not c.blocking


def test_tree_hash_is_stable_and_sees_changes(tmp_path):
    d = tmp_path / "vosk"
    (d / "am").mkdir(parents=True)
    (d / "am" / "final.mdl").write_bytes(b"x")
    (d / "conf.json").write_text("{}")
    h1 = ig.sha256_tree(d)
    assert ig.sha256_tree(d) == h1
    (d / "am" / "final.mdl").write_bytes(b"y")
    assert ig.sha256_tree(d) != h1


def test_safe_extract_blocks_zip_slip_absolute_paths_and_symlinks(tmp_path):
    for name in ("../evil.txt", "/etc/evil.txt", "a/../../evil.txt"):
        z = tmp_path / "bad.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr(name, "x")
        with pytest.raises(ig.IntegrityError):
            ig.safe_extract(z, tmp_path / "out")
    z = tmp_path / "link.zip"
    with zipfile.ZipFile(z, "w") as f:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        f.writestr(info, "/etc/passwd")
    with pytest.raises(ig.IntegrityError):
        ig.safe_extract(z, tmp_path / "out2")
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_accepts_normal_zip(tmp_path):
    z = tmp_path / "ok.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("modelo/am/final.mdl", "x")
    assert ig.safe_extract(z, tmp_path / "out") == ["modelo/am/final.mdl"]
    assert (tmp_path / "out" / "modelo" / "am" / "final.mdl").read_bytes() == b"x"


def test_lock_roundtrip_and_shipped_lock_pins_silero(tmp_path):
    lock = {"a": ig.Entry("a", "file", "a.bin", sha256="ab" * 32, license="MIT", source="s")}
    ig.save_lock(tmp_path / "l.json", lock)
    assert ig.load_lock(tmp_path / "l.json")["a"].sha256 == "ab" * 32
    shipped = ig.load_lock(REPO / "models.lock.json")
    assert len(shipped["silero-vad"].sha256) == 64  # el único que se pudo cotejar con una fuente oficial
    assert shipped["silero-vad"].license == "MIT"


def test_verify_all_skips_ollama_entries(tmp_path):
    lock = {"ollama:x": ig.Entry("ollama:x", "ollama", "", sha256="aa")}
    assert ig.verify_all(tmp_path, lock) == []


def _cfg_with_lock(tmp_path, entries, **sec):
    (tmp_path / "models").mkdir(exist_ok=True)
    ig.save_lock(tmp_path / "models.lock.json", entries)
    return Config(base_dir=tmp_path, security=SecurityCfg(**sec))


def test_preflight_blocks_on_altered_model_but_only_warns_when_unpinned(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "v.onnx").write_bytes(b"bueno")
    good = ig.sha256_file(tmp_path / "models" / "v.onnx")
    cfg = _cfg_with_lock(tmp_path, {"v": ig.Entry("v", "file", "v.onnx", sha256=good)})
    assert _integrity_problems(cfg) == []
    (tmp_path / "models" / "v.onnx").write_bytes(b"malo")
    assert any("alterado" in p for p in _integrity_problems(cfg))
    cfg = _cfg_with_lock(tmp_path, {"v": ig.Entry("v", "file", "v.onnx")})
    assert _integrity_problems(cfg) == []
    cfg.security.allow_unpinned = False
    assert any("sin hash" in p for p in _integrity_problems(cfg))
    cfg.security.verify_models = False
    assert _integrity_problems(cfg) == []


# ------------------------------------------------------------------ Ollama
def _llm_with_transport(handler, **cfg) -> OllamaLlm:
    llm = OllamaLlm(LlmCfg(**cfg))
    llm._client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return llm


def test_ollama_digest_and_missing_model_message():
    def handler(req):
        assert req.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b", "digest": "abc123"}]})

    assert asyncio.run(_llm_with_transport(handler, model="qwen2.5:3b").model_digest()) == "abc123"
    with pytest.raises(LlmError, match="ollama pull otro"):
        asyncio.run(_llm_with_transport(handler, model="otro").model_digest())


def test_pinned_ollama_digest_mismatch_refuses_to_start(tmp_path):
    cfg = _cfg_with_lock(tmp_path, {"ollama:qwen2.5:3b": ig.Entry("ollama:qwen2.5:3b", "ollama", "", sha256="viejo" * 8)})
    cfg.llm = LlmCfg(model="qwen2.5:3b")

    def handler(req):
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b", "digest": "nuevo" * 8}]})

    svc = types.SimpleNamespace(cfg=cfg, llm=_llm_with_transport(handler, model="qwen2.5:3b"))
    with pytest.raises(ig.IntegrityError, match="cambió"):
        asyncio.run(_check_ollama_model(svc))
    cfg.security.verify_models = False
    asyncio.run(_check_ollama_model(svc))  # sin verificación no se bloquea


def test_chat_payload_is_deterministic_and_bounded():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": json.dumps({"status": "unknown", "scene": "", "actions": [], "question": "q"})}})

    h = fixed_household()
    llm = _llm_with_transport(handler, model="m", seed=11, num_predict=123, num_ctx=3000)
    asyncio.run(llm.interpret([{"role": "user", "content": "hola"}], h.mic_room, h.catalog()))
    assert seen["options"] == {"temperature": 0.0, "seed": 11, "num_ctx": 3000, "num_predict": 123}
    assert seen["format"]["properties"]["status"]["enum"] == ["ok", "clarify", "unknown"]
    assert seen["messages"][0]["role"] == "system" and seen["stream"] is False


def test_license_notice_exists_for_qwen_3b():
    assert "no comercial" in ig.LICENSE_NOTES["qwen2.5:3b"]


# ------------------------------------------------------------------ hardware / Whisper
def test_stt_runtime_choice():
    cpu = hardware.Hardware(cuda=False, vram_mb=0, cpu_cores=8)
    g6 = hardware.Hardware(cuda=True, vram_mb=6144, cpu_cores=8)
    g16 = hardware.Hardware(cuda=True, vram_mb=16384, cpu_cores=16)
    assert hardware.choose_stt_runtime("auto", "auto", cpu)[:2] == ("cpu", "int8")
    assert hardware.choose_stt_runtime("auto", "auto", g6)[:2] == ("cuda", "int8_float16")
    assert hardware.choose_stt_runtime("auto", "auto", g16)[:2] == ("cuda", "float16")
    assert hardware.choose_stt_runtime("cuda", "float16", g6)[:2] == ("cuda", "float16")  # lo fijado no se toca
    assert hardware.choose_stt_runtime("auto", "auto", g16, vram_budget_mb=4000)[1] == "int8_float16"
    assert hardware.expected_stt_vram_mb("int8_float16", "large-v3-turbo") < hardware.expected_stt_vram_mb("float16", "large-v3-turbo")


def test_echoes_prompt_never_drops_a_legitimate_order():
    hint = fixed_household().catalog().speech_hint()
    from alivia_edge.shared.domain.text_es import normalize

    ph = normalize(hint)
    # las órdenes reales, aunque aparezcan literalmente en la pista, NO son eco
    for order in ("cierra la puerta de la entrada", "enciende la luz de la sala", "cierra la puerta", "apaga la luz del dormitorio"):
        assert not echoes_prompt(normalize(order), ph), order
    # el eco típico sí
    assert echoes_prompt(normalize("Alivia. Órdenes para la casa: cierra la puerta de la entrada"), ph)
    words = ph.split()
    assert echoes_prompt(" ".join(words[: int(len(words) * 0.8)]), ph)
    assert not echoes_prompt("", ph) and not echoes_prompt("hola", "")


# ------------------------------------------------------------------ catálogo
def test_with_de_contraction_and_names():
    assert with_de("la sala") == "de la sala" and with_de("el dormitorio") == "del dormitorio"
    t = [ActuatorTarget(1, "AA:BB:CC:00:00:01", "LIGHT", "dormitorio", "")]
    cat = Catalog.from_targets(t, {"dormitorio": "el dormitorio"})
    assert cat.devices["light_1"].name == "la luz del dormitorio"


def test_speech_hint_lists_devices_and_is_bounded():
    h = fixed_household()
    hint = h.catalog().speech_hint()
    assert "la puerta de la entrada" in hint and "la ventana del dormitorio" in hint and len(hint) <= 280
    big = random_household("x", random.Random(3))
    assert len(big.catalog().speech_hint(max_chars=60)) <= 61


# ------------------------------------------------------------------ conjunto de datos
def test_dataset_is_deterministic_and_leak_free():
    a = dataset.build(5, 2, 3, 25, seed=3)
    b = dataset.build(5, 2, 3, 25, seed=3)
    assert [s.messages() for s in a["train"]] == [s.messages() for s in b["train"]]
    houses = {k: {s.household.hid for s in v} for k, v in a.items()}
    assert not (houses["train"] & houses["test"]) and not (houses["train"] & houses["val"]) and not (houses["val"] & houses["test"])
    assert dataset.build(5, 2, 3, 25, seed=4)["train"][0].messages() != a["train"][0].messages()


def test_every_sample_uses_production_prompt_and_valid_gold():
    sp = dataset.build(8, 1, 1, 30)
    tags = set()
    for s in sp["train"]:
        msgs = s.messages()
        assert msgs[0] == {"role": "system", "content": build_system_prompt(s.household.catalog(), s.household.mic_room)}
        assert msgs[-1]["role"] == "assistant" and json.loads(msgs[-1]["content"]) == s.gold
        interp = parse_interpretation(msgs[-1]["content"], s.household.catalog())
        assert interp.status == s.gold["status"]
        tags.add(s.tag)
    assert {"single_full", "bare_resolved", "multi", "scene", "unknown", "missing_action", "followup_action"} <= tags
    assert "bare_ambiguous" in tags or "followup_device" in tags


def test_gold_never_references_devices_outside_the_household():
    for s in dataset.build(10, 0, 0, 30)["train"]:
        for a in s.gold["actions"]:
            assert a["device"] in s.household.catalog().devices


def test_handwritten_cases_are_valid_and_independent_of_templates():
    cs = handwritten.cases()
    assert len(cs) >= 30
    for c in cs:
        dataset.validate(c)
    texts = {c.history[-1]["content"] for c in cs}
    assert "ya me voy a dormir" in texts and "¿Qué hora es?" in texts


def test_write_creates_training_and_eval_files(tmp_path):
    sp = dataset.build(2, 1, 1, 10)
    counts = dataset.write(tmp_path, sp)
    for name, n in counts.items():
        assert len((tmp_path / f"{name}.jsonl").read_text().splitlines()) == n
        rows = ev.load_cases(tmp_path / f"{name}.eval.jsonl")
        assert len(rows) == n and isinstance(rows[0].household, Household)
    row = json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])
    assert set(row) == {"messages", "tag"} and row["messages"][-1]["role"] == "assistant"


def test_household_meta_roundtrip():
    h = random_household("r", random.Random(9))
    h2 = Household.from_meta(json.loads(json.dumps(h.to_meta())))
    assert list(h2.catalog().devices) == list(h.catalog().devices) and h2.mic_room == h.mic_room


# ------------------------------------------------------------------ evaluación
def _oracle(cases):
    index = {(id(c.household.catalog()), json.dumps(c.history, ensure_ascii=False)): c for c in cases}

    async def interpret(history, room, catalog):
        c = index[(id(catalog), json.dumps(history, ensure_ascii=False))]
        return parse_interpretation(json.dumps(c.gold, ensure_ascii=False), catalog)

    return interpret


def test_evaluator_scores_perfect_oracle_at_100_percent():
    cases = ev.from_samples(handwritten.cases() + dataset.build(0, 0, 3, 20)["test"])
    rep = asyncio.run(ev.evaluate(cases, _oracle(cases)))
    s = rep.summary()
    assert s["exacto"] == 1.0 and s["acciones_de_mas"] == 0 and s["json_invalido"] == 0 and rep.n == len(cases)


def test_evaluator_flags_extra_actions_missed_actions_and_invalid_output():
    cases = ev.from_samples(handwritten.cases())
    oracle = _oracle(cases)

    async def always_closes_door(history, room, catalog):
        return parse_interpretation(json.dumps({"status": "ok", "scene": "", "actions": [{"device": "door_10", "action": "cerrar"}], "question": ""}), catalog)

    rep = asyncio.run(ev.evaluate(cases, always_closes_door))
    assert rep.extra_actions > 0 and rep.exact < rep.n  # propone cerrar la puerta aunque no se pidió

    async def silent(history, room, catalog):
        return parse_interpretation(json.dumps({"status": "unknown", "scene": "", "actions": [], "question": "x"}), catalog)

    rep = asyncio.run(ev.evaluate(cases, silent))
    assert rep.missed > 0 and rep.extra_actions == 0

    async def broken(history, room, catalog):
        raise LlmError("json roto")

    rep = asyncio.run(ev.evaluate(cases, broken))
    assert rep.invalid == rep.n and rep.exact == 0
    assert asyncio.run(ev.evaluate(cases, oracle)).exact == len(cases)


def test_evaluator_reports_failures_with_context():
    cases = ev.from_samples(handwritten.cases()[:5])

    async def wrong(history, room, catalog):
        return parse_interpretation(json.dumps({"status": "unknown", "scene": "", "actions": [], "question": "q"}), catalog)

    rep = asyncio.run(ev.evaluate(cases, wrong))
    assert rep.failures and {"dijo", "esperado", "obtenido", "cuarto"} <= set(rep.failures[0])
