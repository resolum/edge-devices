"""Escanea los modelos descargados: análisis estático + ClamAV (opcional) + hashes para VirusTotal.

    python scripts/scan_models.py                       # models/ y ~/.cache/huggingface (si existe)
    python scripts/scan_models.py models/piper          # una ruta concreta
    python scripts/scan_models.py --clamav              # además pasa clamscan (brew install clamav)
    python scripts/scan_models.py --hashes              # imprime SHA-256 por archivo para buscarlos en virustotal.com

Salida 0 si todo está LIMPIO, 1 si hay algo PELIGROSO o a REVISAR. Esto no detecta pesos envenenados (ver docs/modelos.md).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alivia_edge.shared.infrastructure import integrity as ig  # noqa: E402
from alivia_edge.shared.infrastructure import modelscan as ms  # noqa: E402


def default_targets() -> list[Path]:
    cands = [ROOT / "models", Path.home() / ".cache" / "huggingface" / "hub", Path.home() / ".ollama" / "models"]
    return [p for p in cands if p.exists()]


def run_clamav(target: Path) -> tuple[bool, str]:
    exe = shutil.which("clamscan")
    if not exe:
        return False, "clamscan no está instalado (macOS: brew install clamav && freshclam)"
    proc = subprocess.run([exe, "-r", "--infected", "--no-summary", str(target)], capture_output=True, text=True, timeout=3600)
    # 0 = limpio, 1 = infectado, 2 = error
    if proc.returncode == 0:
        return True, "ClamAV: sin detecciones"
    if proc.returncode == 1:
        return False, "ClamAV DETECTÓ:\n" + proc.stdout.strip()
    return False, "ClamAV falló:\n" + (proc.stderr or proc.stdout).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--clamav", action="store_true")
    ap.add_argument("--hashes", action="store_true")
    args = ap.parse_args()
    targets = args.paths or default_targets()
    if not targets:
        print("No hay nada que escanear. Ejecuta primero scripts/fetch_models.py", file=sys.stderr)
        return 1
    bad = False
    for t in targets:
        if not t.exists():
            print(f"{t}: no existe", file=sys.stderr)
            bad = True
            continue
        findings = ms.scan_path(t)
        v = ms.verdict(findings)
        print(f"\n== {t}: {v}")
        for f in findings:
            print(f"   {f}")
        bad |= v != "LIMPIO"
        if args.clamav:
            ok, msg = run_clamav(t)
            print(f"   {msg}")
            bad |= not ok
        if args.hashes:
            files = [t] if t.is_file() else [p for p in sorted(t.rglob("*")) if p.is_file()]
            for p in files:
                print(f"   {ig.sha256_file(p)}  {p}")
    print("\nResultado:", "hay hallazgos" if bad else "todo limpio (análisis estático)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
