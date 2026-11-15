#!/usr/bin/env python
"""Descarga los modelos que el edge necesita en ./models y verifica su integridad (SHA-256).

  python scripts/fetch_models.py                 # vosk + piper (es_MX claude) + silero
  python scripts/fetch_models.py --voice ald     # otra voz es_MX
  python scripts/fetch_models.py --only silero
  python scripts/fetch_models.py --lock          # registra el hash de lo que YA tienes descargado

Integridad: models.lock.json guarda el SHA-256 de cada modelo. Silero viene con un hash cotejado contra el paquete
oficial de PyPI. Vosk y Piper se registran en la primera descarga (confianza en el primer uso): cotéjalos con el
SHA-256 que publica Hugging Face para el archivo. Si un hash no coincide, el archivo se rechaza.

Whisper (large-v3-turbo) se descarga solo la primera vez que arranca el edge (unos 1.6 GB) desde
mobiuslabsgmbh/faster-whisper-large-v3-turbo. Qwen se instala con Ollama:  ollama pull qwen2.5:3b
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from alivia_edge.shared.infrastructure import integrity as ig  # noqa: E402
from alivia_edge.shared.infrastructure import modelscan as ms  # noqa: E402

ROOT = REPO / "models"
LOCK = REPO / "models.lock.json"
MAX_DOWNLOAD = 3 * 1024**3

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX"
PIPER_VOICES = {"claude": ("claude/high", "es_MX-claude-high"), "ald": ("ald/medium", "es_MX-ald-medium")}
# Etiqueta fija (no master): el modelo no cambia sin que lo decidas. Mismo archivo que trae el paquete PyPI silero-vad 6.2.3.
SILERO_URL = "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx"


def download(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"solo se descarga por HTTPS: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  descargando {url}")
    tty = sys.stdout.isatty()
    last = [-1]

    def hook(blocks, size, total):
        if total > MAX_DOWNLOAD or blocks * size > MAX_DOWNLOAD:
            raise ValueError("descarga demasiado grande, se aborta")
        if total <= 0:
            return
        done = min(blocks * size, total)
        pct = int(done * 100 / total)
        if tty:
            print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        elif pct // 25 != last[0]:
            last[0] = pct // 25
            print(f"  {pct}%", flush=True)

    try:
        urllib.request.urlretrieve(url, tmp, hook)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if tty:
        print()
    tmp.replace(dest)


def _record_or_check(name: str, lock: dict[str, ig.Entry], default: ig.Entry) -> None:
    """Tras descargar: si el bloqueo ya trae hash, se exige; si no, se registra (primer uso)."""
    e = lock.setdefault(name, default)
    target = ROOT / e.path
    got = ig.digest(target, e.kind)
    if e.sha256 and got != e.sha256:
        shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        raise ig.IntegrityError(f"{name}: SHA-256 {got[:16]}... no coincide con el esperado {e.sha256[:16]}...; se borró el archivo")
    findings = [f for f in ms.scan_path(target) if f.severity != "info"]
    if any(f.severity == "alto" for f in findings):
        shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        raise ig.IntegrityError(f"{name}: el escaneo estático encontró código ejecutable; se borró. " + "; ".join(map(str, findings)))
    for f in findings:
        print(f"  AVISO {f}")
    if not e.sha256:
        e.sha256 = got
        ig.save_lock(LOCK, lock)
        print(f"  {name}: hash registrado ({got[:16]}...). Cotéjalo con el SHA-256 de la página del modelo.")
    else:
        print(f"  {name}: hash verificado")


def fetch_vosk(lock: dict[str, ig.Entry]) -> None:
    target = ROOT / "vosk-model-small-es-0.42"
    if target.is_dir():
        print("Vosk: ya existe")
        return
    print("Vosk (es, pequeño: soporta gramática para la palabra de activación)")
    zpath = ROOT / "vosk.zip"
    download(VOSK_URL, zpath)
    try:
        ig.safe_extract(zpath, ROOT)
    finally:
        zpath.unlink(missing_ok=True)
    _record_or_check("vosk-small-es", lock, ig.Entry("vosk-small-es", "tree", target.name, license="Apache-2.0", source=VOSK_URL))


def fetch_piper(lock: dict[str, ig.Entry], voice: str) -> None:
    sub, name = PIPER_VOICES[voice]
    onnx = ROOT / "piper" / f"{name}.onnx"
    if onnx.is_file() and onnx.with_suffix(".onnx.json").is_file():
        print(f"Piper {name}: ya existe")
        return
    print(f"Piper {name}")
    download(f"{PIPER_BASE}/{sub}/{name}.onnx", onnx)
    download(f"{PIPER_BASE}/{sub}/{name}.onnx.json", Path(str(onnx) + ".json"))
    for suffix in (".onnx", ".onnx.json"):
        key = f"piper-{name}{suffix}"
        _record_or_check(key, lock, ig.Entry(key, "file", f"piper/{name}{suffix}", license="Apache-2.0 / Unlicense", source=f"{PIPER_BASE}/{sub}"))


def fetch_silero(lock: dict[str, ig.Entry]) -> None:
    target = ROOT / "silero_vad.onnx"
    if target.is_file():
        print("Silero VAD: ya existe")
        return
    print("Silero VAD (v6.2.3)")
    download(SILERO_URL, target)
    _record_or_check("silero-vad", lock, ig.Entry("silero-vad", "file", "silero_vad.onnx", license="MIT", source=SILERO_URL))


def lock_existing(lock: dict[str, ig.Entry]) -> None:
    """Para instalaciones previas: registra el hash de lo que ya hay (confianza en el estado actual)."""
    for name, e in lock.items():
        target = ROOT / e.path
        if not (target.is_dir() if e.kind == "tree" else target.is_file()):
            print(f"  {name}: no está descargado")
            continue
        got = ig.digest(target, e.kind)
        if e.sha256 and got != e.sha256:
            print(f"  {name}: ALERTA, el hash actual {got[:16]}... NO coincide con el registrado {e.sha256[:16]}...")
        elif not e.sha256:
            e.sha256 = got
            print(f"  {name}: registrado {got[:16]}...")
        else:
            print(f"  {name}: ok")
    ig.save_lock(LOCK, lock)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["vosk", "piper", "silero"])
    ap.add_argument("--voice", choices=list(PIPER_VOICES), default="claude")
    ap.add_argument("--lock", action="store_true", help="registra/verifica los hashes de lo ya descargado y sale")
    args = ap.parse_args()
    lock = ig.load_lock(LOCK)
    if args.lock:
        lock_existing(lock)
        return 0
    steps = {"vosk": lambda: fetch_vosk(lock), "piper": lambda: fetch_piper(lock, args.voice), "silero": lambda: fetch_silero(lock)}
    failed = False
    for name, fn in steps.items():
        if args.only in (None, name):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                failed = True
                print(f"  ERROR en {name}: {exc}", file=sys.stderr)
    if args.voice != "claude":
        print(f"\nRecuerda poner voice_path = \"models/piper/{PIPER_VOICES[args.voice][1]}.onnx\" en [tts] de config.toml")
    print("\nListo. Falta (si no lo hiciste):  ollama pull qwen2.5:3b   y   python scripts/verify_models.py")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
