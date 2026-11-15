#!/usr/bin/env python
"""Mide el rendimiento de cada etapa del flujo de voz en TU máquina y estima la espera total.

  python scripts/bench.py                    # todo lo que esté disponible
  python scripts/bench.py --only vad stt     # solo algunas etapas
  python scripts/bench.py --config config.toml --snr-db 15

Etapas: vad (Silero), wake (Vosk), tts (Piper), stt (Whisper: tiempo y error de palabras con y sin pista de
vocabulario), llm (Ollama: tiempo y acierto). El audio de prueba lo genera Piper (lazo cerrado), y se le agrega ruido:
sirve para comparar configuraciones y detectar regresiones, NO reemplaza probar con tu voz y tu micrófono.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("ALIVIA_WS_TOKEN", "bench")
os.environ.setdefault("ALIVIA_EDGE_CREDENTIAL", "0.bench")
from alivia_edge.shared.config import Config, load_config  # noqa: E402
from alivia_edge.shared.domain.text_es import normalize  # noqa: E402
from alivia_edge.tuning.households import fixed_household  # noqa: E402

PHRASES = [
    "cierra la puerta de la entrada", "enciende la luz de la sala", "apaga la luz del dormitorio",
    "abre la ventana de la sala", "cierra la cortina del dormitorio", "apaga la luz de la cocina",
    "ya me voy a dormir cierra todo", "abre la ventana del dormitorio y enciende la luz de la sala",
]
RESULTS: dict[str, str] = {}


def wer(ref: str, hyp: str) -> float:
    r, h = normalize(ref).split(), normalize(hyp).split()
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(1, len(r))


def noisy(pcm: bytes, snr_db: float, rng: np.random.Generator) -> bytes:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if snr_db <= 0 or not len(x):
        return pcm
    p = float(np.mean(x**2)) or 1.0
    n = rng.standard_normal(len(x)).astype(np.float32) * np.sqrt(p / 10 ** (snr_db / 10))
    return np.clip(x + n, -32768, 32767).astype("<i2").tobytes()


def row(name: str, value: str) -> None:
    RESULTS[name] = value
    print(f"  {name:<34}{value}")


def bench_vad(cfg: Config) -> None:
    from alivia_edge.voice.infrastructure.audio.vad import WIN, SileroVad

    p = Path(cfg.resolve(cfg.vad.model_path))
    if not p.is_file():
        return print("  vad: falta el modelo")
    vad = SileroVad(str(p))
    w = (np.random.default_rng(0).standard_normal(WIN) * 0.05).astype(np.float32)
    for _ in range(20):
        vad.prob(w)
    t0 = time.perf_counter()
    for _ in range(500):
        vad.prob(w)
    ms = (time.perf_counter() - t0) / 500 * 1000
    row("VAD por ventana de 32 ms", f"{ms:.2f} ms  ({32 / ms:.0f}x tiempo real)")


async def synth_all(cfg: Config, executor, texts: list[str]):
    from alivia_edge.voice.infrastructure.audio.tts import PiperTts

    tts = PiperTts(cfg.tts, cfg.resolve(cfg.tts.voice_path), executor)
    tts.load()
    await tts.synth("prueba")  # calienta
    out, lat = {}, []
    for t in texts:
        t0 = time.perf_counter()
        out[t] = await tts.synth(t)
        lat.append(time.perf_counter() - t0)
    return out, lat


def have_voice(cfg: Config) -> bool:
    v = Path(cfg.resolve(cfg.tts.voice_path))
    return v.is_file() and Path(str(v) + ".json").is_file()


async def bench_audio(cfg: Config, only: set[str], snr: float) -> None:
    if not have_voice(cfg):
        return print("  tts/stt/wake: falta la voz de Piper (genera el audio de prueba)")
    ex = ThreadPoolExecutor(max_workers=2)
    texts = PHRASES + ["alivia", "alivia cierra la puerta de la entrada"]
    audio, lat = await synth_all(cfg, ex, texts)
    if "tts" in only:
        row("TTS síntesis (frase corta)", f"mediana {statistics.median(lat) * 1000:.0f} ms, máx {max(lat) * 1000:.0f} ms")
    rng = np.random.default_rng(1)
    if "wake" in only and Path(cfg.resolve(cfg.wake.model_path)).is_dir():
        from alivia_edge.voice.infrastructure.audio.vosk_rec import VoskWake, load_vosk_model

        wake = VoskWake(load_vosk_model(cfg.resolve(cfg.wake.model_path)), cfg.wake)
        pcm = noisy(audio["alivia cierra la puerta de la entrada"], snr, rng)
        hit, t0 = False, time.perf_counter()
        for i in range(0, len(pcm), 1280):
            hit = wake.feed(pcm[i : i + 1280]) or hit
        el = time.perf_counter() - t0
        row("Vosk palabra de activación", f"{'detectada' if hit else 'NO detectada'}; {el / (len(pcm) / 32000):.2f}x tiempo real")
    if "stt" in only:
        from alivia_edge.voice.infrastructure.audio.stt import WhisperStt

        stt = WhisperStt(cfg.stt, ex)
        t0 = time.perf_counter()
        stt.load()
        row("Whisper carga del modelo", f"{time.perf_counter() - t0:.1f} s en {stt.runtime[0]}/{stt.runtime[1]}")
        await stt.transcribe(b"\x00\x00" * 16000)
        hint = fixed_household().catalog().speech_hint()
        for label, prompt in (("sin pista (prompt fijo)", None), ("con pista de vocabulario", hint)):
            errs, times, rtf = [], [], []
            for t in PHRASES:
                pcm = noisy(audio[t], snr, rng)
                t0 = time.perf_counter()
                got = await stt.transcribe(pcm, prompt=prompt)
                dt = time.perf_counter() - t0
                errs.append(wer(t, got))
                times.append(dt)
                rtf.append(dt / (len(pcm) / 32000))
            row(f"Whisper {label}", f"WER {statistics.fmean(errs) * 100:.1f}%, mediana {statistics.median(times):.2f} s, {statistics.median(rtf):.2f}x tiempo real")


async def bench_llm(cfg: Config) -> None:
    from alivia_edge.commands.infrastructure.llm import OllamaLlm
    from alivia_edge.tuning import evaluate as ev
    from alivia_edge.tuning import handwritten

    llm = OllamaLlm(cfg.llm)
    await llm.start()
    try:
        await llm.model_digest()
        cases = ev.from_samples(handwritten.cases())
        await llm.interpret(cases[0].history, cases[0].household.mic_room, cases[0].household.catalog())
        rep = await ev.evaluate(cases, llm.interpret)
    except Exception as exc:  # noqa: BLE001
        return print(f"  llm: no disponible ({exc})")
    finally:
        await llm.aclose()
    s = rep.summary()
    row(f"LLM {cfg.llm.model} (casos a mano)", f"exacto {s['exacto'] * 100:.0f}%, de más {s['acciones_de_mas'] * 100:.0f}%, p50 {s['latencia_s']['p50']:.2f} s, p95 {s['latencia_s']['p95']:.2f} s")
    RESULTS["_llm_p50"] = str(s["latencia_s"]["p50"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--only", nargs="+", choices=["vad", "wake", "tts", "stt", "llm"])
    ap.add_argument("--snr-db", type=float, default=20.0)
    a = ap.parse_args()
    cfg = load_config(a.config) if Path(a.config).is_file() else Config()
    only = set(a.only or ["vad", "wake", "tts", "stt", "llm"])
    print(f"Benchmark ({cfg.stt.model}, {cfg.llm.model}, SNR {a.snr_db} dB)")
    if "vad" in only:
        bench_vad(cfg)
    if only & {"wake", "tts", "stt"}:
        asyncio.run(bench_audio(cfg, only, a.snr_db))
    if "llm" in only:
        asyncio.run(bench_llm(cfg))
    print("\nLo que importa para la espera: fin de frase (VAD) + Whisper + LLM + primer audio de Piper.")
    print(f"El fin de frase espera {cfg.vad.end_silence_ms} ms de silencio antes de transcribir (ajustable en [vad].end_silence_ms).")


if __name__ == "__main__":
    main()
