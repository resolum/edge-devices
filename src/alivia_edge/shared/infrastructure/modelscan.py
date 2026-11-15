"""Escáner estático de archivos de modelo (solo biblioteca estándar).

Qué busca (lo que de verdad puede dañar tu máquina al cargar un modelo):
- Pickle: .pt/.pth/.pkl/.ckpt (zip con data.pkl) o pickle suelto. Un pickle puede ejecutar código al cargarse. Se
  recorren los opcodes con pickletools SIN deserializar y se marcan los GLOBAL/STACK_GLOBAL a módulos peligrosos
  (os, subprocess, builtins.eval/exec, socket, ...).
- Ejecutables dentro de la carpeta del modelo: ELF, Mach-O, PE, scripts con shebang, .py/.sh/.so/.dylib/.dll/.exe.
- Archivos comprimidos anidados (zip/tar/gz) donde no se espera ninguno.
- Cabeceras corruptas en formatos que se pueden validar (safetensors, GGUF, ONNX no vacío).

Qué NO hace: no es un antivirus (usa ClamAV, ver scripts/scan_models.py --clamav) y no detecta modelos envenenados o
con puertas traseras en los pesos. Un resultado "limpio" significa "no hay código ejecutable en el archivo".
"""
from __future__ import annotations

import json
import pickletools
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

DANGEROUS_MODULES = {
    "os", "posix", "nt", "subprocess", "sys", "socket", "shutil", "pty", "ctypes", "importlib", "runpy",
    "webbrowser", "urllib", "http", "requests", "ftplib", "telnetlib", "pickle", "marshal", "code", "codeop",
}
DANGEROUS_BUILTINS = {"eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "input"}
PICKLE_EXT = {".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".joblib"}
EXEC_EXT = {".py", ".sh", ".bat", ".cmd", ".ps1", ".so", ".dylib", ".dll", ".exe", ".jar", ".js", ".command", ".app"}
ARCHIVE_MAGIC = (b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!")
EXEC_MAGIC = {
    b"\x7fELF": "ELF (ejecutable Linux)",
    b"MZ": "PE (ejecutable Windows)",
    b"\xfe\xed\xfa\xce": "Mach-O",
    b"\xfe\xed\xfa\xcf": "Mach-O",
    b"\xce\xfa\xed\xfe": "Mach-O",
    b"\xcf\xfa\xed\xfe": "Mach-O",
    b"\xca\xfe\xba\xbe": "Mach-O universal / clase Java",
    b"#!": "script con shebang",
}
MAX_PICKLE_OPS = 2_000_000
SAFETENSORS_MAX_HEADER = 100 * 1024 * 1024


@dataclass
class Finding:
    path: str
    severity: str  # "alto" | "medio" | "info"
    what: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.path}: {self.what}"


def _scan_pickle_bytes(data: bytes, label: str) -> list[Finding]:
    """Recorre opcodes; nunca llama a pickle.load."""
    out: list[Finding] = []
    strings: list[str] = []
    n = 0
    try:
        for op, arg, _pos in pickletools.genops(data):
            n += 1
            if n > MAX_PICKLE_OPS:
                out.append(Finding(label, "medio", "pickle demasiado largo, análisis truncado"))
                break
            if op.name in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8"):
                strings.append(str(arg))
                strings[:] = strings[-2:]
            elif op.name == "GLOBAL":
                module, _, name = str(arg).partition(" ")
                out.extend(_judge(label, module, name))
            elif op.name == "STACK_GLOBAL" and len(strings) >= 2:
                out.extend(_judge(label, strings[-2], strings[-1]))
    except Exception as exc:  # noqa: BLE001
        out.append(Finding(label, "medio", f"pickle ilegible o malformado ({type(exc).__name__})"))
    return out


def _judge(label: str, module: str, name: str) -> list[Finding]:
    root = module.split(".")[0]
    if root in DANGEROUS_MODULES or (root == "builtins" and name in DANGEROUS_BUILTINS) or (
        root == "__builtin__" and name in DANGEROUS_BUILTINS
    ):
        return [Finding(label, "alto", f"el pickle importa {module}.{name} (puede ejecutar código al cargarse)")]
    return []


def _looks_like_pickle(head: bytes) -> bool:
    return len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5


def scan_file(path: Path, root: Path | None = None) -> list[Finding]:
    rel = str(path.relative_to(root)) if root else str(path)
    out: list[Finding] = []
    if path.is_symlink():
        return [Finding(rel, "alto", "enlace simbólico dentro del modelo")]
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return [Finding(rel, "medio", f"no se pudo leer ({exc})")]
    ext = path.suffix.lower()

    if ext in EXEC_EXT:
        out.append(Finding(rel, "alto", f"extensión ejecutable/script ({ext}) dentro del modelo"))
    for magic, label in EXEC_MAGIC.items():
        if head.startswith(magic):
            if magic == b"MZ" and ext in {".onnx", ".bin", ".json", ".txt"} and head[:4] != b"MZ\x90\x00":
                continue  # 2 bytes son demasiado poco: exige la cabecera DOS típica
            out.append(Finding(rel, "alto", f"cabecera de {label}"))
            break

    if ext == ".safetensors":
        out.extend(_check_safetensors(path, rel, size))
    elif ext == ".gguf":
        if head[:4] != b"GGUF":
            out.append(Finding(rel, "alto", "extensión .gguf pero la cabecera no es GGUF"))
    elif ext == ".onnx":
        if size == 0:
            out.append(Finding(rel, "alto", "archivo ONNX vacío"))
        if head.startswith(ARCHIVE_MAGIC):
            out.append(Finding(rel, "medio", "ONNX con cabecera de archivo comprimido"))
    elif ext in PICKLE_EXT or _looks_like_pickle(head):
        out.extend(_scan_pickleish(path, rel, head))
    elif head.startswith(ARCHIVE_MAGIC) and ext not in {".zip"}:
        out.append(Finding(rel, "medio", "archivo comprimido donde no se espera ninguno"))
    return out


def _scan_pickleish(path: Path, rel: str, head: bytes) -> list[Finding]:
    out: list[Finding] = []
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as zf:
                found = False
                for info in zf.infolist():
                    if info.filename.endswith(".pkl"):
                        found = True
                        if info.file_size > 512 * 1024 * 1024:
                            out.append(Finding(rel, "medio", f"{info.filename} enorme, no se analiza"))
                            continue
                        out.extend(_scan_pickle_bytes(zf.read(info), f"{rel}!{info.filename}"))
                if not found:
                    out.append(Finding(rel, "info", "zip sin pickle dentro"))
        except zipfile.BadZipFile:
            out.append(Finding(rel, "medio", "zip corrupto"))
    else:
        data = path.read_bytes() if path.stat().st_size <= 512 * 1024 * 1024 else b""
        if not data:
            out.append(Finding(rel, "medio", "archivo pickle enorme, no se analiza"))
        else:
            out.extend(_scan_pickle_bytes(data, rel))
    if not any(f.severity == "alto" for f in out):
        out.append(Finding(rel, "info", "formato pickle: aunque no se vea nada malo, prefiere safetensors u ONNX"))
    return out


def _check_safetensors(path: Path, rel: str, size: int) -> list[Finding]:
    try:
        with path.open("rb") as fh:
            raw = fh.read(8)
            if len(raw) < 8:
                return [Finding(rel, "alto", "safetensors truncado")]
            (n,) = struct.unpack("<Q", raw)
            if n > SAFETENSORS_MAX_HEADER or n + 8 > size:
                return [Finding(rel, "alto", "cabecera safetensors con tamaño imposible")]
            json.loads(fh.read(n))
    except (ValueError, OSError):
        return [Finding(rel, "alto", "cabecera safetensors no es JSON válido")]
    return []


def scan_path(target: Path) -> list[Finding]:
    """Escanea un archivo o una carpeta completa."""
    if target.is_file():
        return scan_file(target, target.parent)
    out: list[Finding] = []
    for p in sorted(target.rglob("*")):
        if p.is_dir() and not p.is_symlink():
            continue
        out.extend(scan_file(p, target))
    return out


def verdict(findings: list[Finding]) -> str:
    if any(f.severity == "alto" for f in findings):
        return "PELIGROSO"
    if any(f.severity == "medio" for f in findings):
        return "REVISAR"
    return "LIMPIO"
