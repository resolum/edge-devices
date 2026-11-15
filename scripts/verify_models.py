#!/usr/bin/env python
"""Verifica los modelos locales contra models.lock.json (SHA-256) sin descargar nada.

  python scripts/verify_models.py                          # código de salida 1 si algún hash no coincide
  python scripts/verify_models.py --pin-ollama qwen2.5:3b  # registra la huella del modelo instalado en Ollama
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from alivia_edge.shared.infrastructure import integrity as ig  # noqa: E402

LOCK = REPO / "models.lock.json"


def pin_ollama(model: str, url: str) -> int:
    with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=10) as r:  # noqa: S310 (Ollama local)
        models = json.load(r).get("models", [])
    for m in models:
        if m.get("name") in (model, f"{model}:latest"):
            lock = ig.load_lock(LOCK)
            lock[f"ollama:{model}"] = ig.Entry(
                f"ollama:{model}", "ollama", "", sha256=str(m["digest"]), source="Ollama /api/tags",
                license="ver docs/modelos.md",
            )
            ig.save_lock(LOCK, lock)
            print(f"Registrado ollama:{model} digest={m['digest'][:16]}...")
            return 0
    print(f"{model} no está en Ollama: ollama pull {model}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pin-ollama", metavar="MODELO")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = ap.parse_args()
    if args.pin_ollama:
        return pin_ollama(args.pin_ollama, args.ollama_url)
    lock = ig.load_lock(LOCK)
    if not lock:
        print("No hay models.lock.json")
        return 1
    bad = 0
    for c in ig.verify_all(REPO / "models", lock):
        mark = {"ok": "OK       ", "unpinned": "SIN HASH ", "missing": "FALTA    ", "mismatch": "ALTERADO "}[c.status]
        print(f"{mark}{c.name}" + (f"  ({c.detail})" if c.detail else ""))
        bad += c.blocking
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
