"""Integridad de los modelos descargados: hashes SHA-256 contra un archivo de bloqueo (models.lock.json).

Solo biblioteca estándar, para que scripts/fetch_models.py pueda usarlo antes de instalar nada.

Dos modos por entrada del bloqueo:
- `sha256` conocido: se verifica contra ese valor (viene de una fuente oficial cotejada).
- `sha256` vacío: confianza en el primer uso (TOFU). La primera descarga registra el hash y desde ahí cualquier cambio
  se rechaza. No prueba que la primera descarga fuera legítima: cotéjalo con el SHA-256 que muestra la página del
  modelo (Hugging Face lo publica por archivo) o con otra descarga independiente.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CHUNK = 1 << 20
MAX_UNZIPPED_BYTES = 3 * 1024**3  # un modelo Vosk pequeño pesa ~0,1 GB; el grande ~2 GB. Más de 3 GB es sospechoso.
MAX_ZIP_FILES = 20000

# Avisos de licencia que conviene ver en el arranque (no bloquean).
LICENSE_NOTES = {
    "qwen2.5:3b": (
        "Qwen2.5-3B-Instruct usa la licencia Qwen Research: solo uso no comercial (investigación o evaluación). "
        "Sirve para el proyecto académico; para comercializar Alivia hay que pedir licencia a Alibaba Cloud o cambiar "
        "de modelo (p. ej. uno con Apache 2.0). Ver docs/modelos.md."
    ),
}


class IntegrityError(Exception):
    pass


@dataclass
class Entry:
    name: str
    kind: str  # "file" | "tree"
    path: str  # relativo a la raíz de modelos del proyecto
    sha256: str = ""
    license: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def sha256_tree(root: str | os.PathLike[str]) -> str:
    """Hash estable de una carpeta: rutas relativas ordenadas + hash de cada archivo."""
    base = Path(root)
    h = hashlib.sha256()
    for p in sorted(x for x in base.rglob("*") if x.is_file()):
        h.update(p.relative_to(base).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(p).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def digest(path: str | os.PathLike[str], kind: str) -> str:
    return sha256_tree(path) if kind == "tree" else sha256_file(path)


# --- bloqueo ---------------------------------------------------------------------------------
def load_lock(path: str | os.PathLike[str]) -> dict[str, Entry]:
    p = Path(path)
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, Entry] = {}
    for name, d in raw.get("models", {}).items():
        out[name] = Entry(
            name=name, kind=d.get("kind", "file"), path=d["path"], sha256=d.get("sha256", ""),
            license=d.get("license", ""), source=d.get("source", ""),
            extra={k: v for k, v in d.items() if k not in {"kind", "path", "sha256", "license", "source"}},
        )
    return out


def save_lock(path: str | os.PathLike[str], entries: dict[str, Entry]) -> None:
    models = {}
    for name in sorted(entries):
        e = entries[name]
        models[name] = {"kind": e.kind, "path": e.path, "sha256": e.sha256, "license": e.license, "source": e.source, **e.extra}
    Path(path).write_text(json.dumps({"version": 1, "models": models}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- verificación -----------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str  # ok | mismatch | missing | unpinned
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "mismatch"


def verify_entry(root: str | os.PathLike[str], e: Entry) -> Check:
    target = Path(root) / e.path
    exists = target.is_dir() if e.kind == "tree" else target.is_file()
    if not exists:
        return Check(e.name, "missing", f"no existe: {target}")
    if not e.sha256:
        return Check(e.name, "unpinned", "sin hash registrado: registra el hash con: python scripts/fetch_models.py --lock")
    got = digest(target, e.kind)
    if got != e.sha256:
        return Check(e.name, "mismatch", f"SHA-256 distinto del esperado ({got[:16]}... != {e.sha256[:16]}...): archivo alterado o versión distinta")
    return Check(e.name, "ok")


def verify_all(root: str | os.PathLike[str], lock: dict[str, Entry], only: set[str] | None = None) -> list[Check]:
    return [verify_entry(root, e) for n, e in lock.items() if e.kind != "ollama" and (only is None or n in only)]


# --- extracción segura de zips -------------------------------------------------------------------
def safe_extract(zip_path: str | os.PathLike[str], dest: str | os.PathLike[str]) -> list[str]:
    """Extrae rechazando rutas absolutas, `..`, enlaces simbólicos y zips desproporcionados (zip slip / bomba)."""
    dest_p = Path(dest).resolve()
    names: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise IntegrityError(f"zip con demasiados archivos ({len(infos)})")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
            raise IntegrityError("zip demasiado grande al descomprimir")
        for i in infos:
            pure = PurePosixPath(i.filename)
            if pure.is_absolute() or ".." in pure.parts or "\\" in i.filename or (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise IntegrityError(f"entrada de zip insegura: {i.filename!r}")
            target = (dest_p / pure).resolve()
            if dest_p != target and dest_p not in target.parents:
                raise IntegrityError(f"entrada de zip fuera del destino: {i.filename!r}")
            names.append(i.filename)
        z.extractall(dest_p)
    return names
