"""Reglas de texto: normalización, quitar la palabra de activación y clasificar sí/no."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def normalize(text: str) -> str:
    """Minúsculas, sin acentos ni signos, espacios colapsados."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9ñ\s]", " ", text.replace("ñ", "\x00")).replace("\x00", "ñ")
    return re.sub(r"\s+", " ", text).strip()


# --- sí / no ---------------------------------------------------------------------------
YES_TOKENS = {
    "si", "claro", "correcto", "exacto", "dale", "ok", "okay", "vale", "hazlo", "adelante", "confirmo",
    "confirmado", "afirmativo", "perfecto", "eso", "mismo", "asi", "es", "de", "acuerdo", "por", "favor",
}
NO_TOKENS = {"no", "nop", "negativo", "cancela", "cancelar", "cancelalo", "olvidalo", "detente", "nada"}

# Palabras para la gramática cerrada de Vosk (deben existir en su vocabulario).
VOSK_YESNO_WORDS = [
    "sí", "claro", "correcto", "exacto", "dale", "ok", "vale", "hazlo", "adelante", "confirmo",
    "afirmativo", "perfecto", "no", "negativo", "cancela", "cancelar", "olvídalo", "detente",
]


@dataclass
class Answer:
    kind: str  # "yes" | "no" | "other" | "none"
    text: str = ""  # transcripción completa (para reinterpretar si trae correcciones)
    has_content: bool = False  # "no, la ventana": hay algo más que el simple no


def classify_answer(text: str) -> Answer:
    tokens = normalize(text).split()
    if not tokens:
        return Answer("none")
    if all(t in YES_TOKENS for t in tokens):
        return Answer("yes", text)
    for i, tok in enumerate(tokens):
        if tok in NO_TOKENS:
            rest = [t for t in tokens[i + 1 :] if t not in NO_TOKENS and t not in {"gracias", "por", "favor"}]
            return Answer("no", text, has_content=len(" ".join(rest)) >= 3)
    return Answer("other", text, has_content=True)


# --- palabra de activación -------------------------------------------------------------
_ACCENT_CLASSES = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uú]"}
_INTERJECTIONS = r"(?:(?:oye|ey|hey|hola|ok|okay|eh)[\s,]+)?"


def _accent_pattern(name: str) -> str:
    return "".join(_ACCENT_CLASSES.get(c, re.escape(c)) for c in name.lower())


def strip_wake(text: str, names: list[str]) -> str:
    """Quita "Oye Alivia," del inicio de la transcripción. Whisper suele escribir "Olivia"."""
    alternatives = "|".join(_accent_pattern(n) for n in names)
    m = re.search(rf"\b{_INTERJECTIONS}(?:{alternatives})\b[\s,.:;!¡¿?]*", text, flags=re.IGNORECASE)
    if m and m.start() <= 40:
        return text[m.end() :].strip()
    return text.strip()


def is_meaningful(text: str, min_chars: int = 4) -> bool:
    return len(normalize(text).replace(" ", "")) >= min_chars


# --- español natural -------------------------------------------------------------------
def join_es(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " y " + parts[-1]


def capfirst(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s
