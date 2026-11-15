"""Vosk: detector de palabra de activación (gramática restringida) y reconocedor cerrado de sí/no.

Nota: la gramática dinámica solo funciona con los modelos *pequeños* (vosk-model-small-es-*).
"""
from __future__ import annotations

import json
import logging

from alivia_edge.shared.config import WakeCfg
from alivia_edge.voice.domain.ports import WakeResult
from alivia_edge.shared.domain.text_es import NO_TOKENS, VOSK_YESNO_WORDS, YES_TOKENS, normalize

log = logging.getLogger(__name__)


def load_vosk_model(path: str):
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    return Model(path)


class VoskWake:
    def __init__(self, model, cfg: WakeCfg) -> None:
        from vosk import KaldiRecognizer

        grammar = json.dumps(list(cfg.phrases) + ["[unk]"], ensure_ascii=False)
        self._rec = KaldiRecognizer(model, 16000, grammar)
        self._rec.SetWords(True)
        self._kw = normalize(cfg.keyword)
        self._hit_words: list[dict] | None = None

    def _has_kw(self, text: str) -> bool:
        return self._kw in normalize(text).split()

    def feed(self, pcm: bytes) -> bool:
        if self._rec.AcceptWaveform(pcm):
            res = json.loads(self._rec.Result())
            if self._has_kw(res.get("text", "")):
                self._hit_words = res.get("result", [])
                return True
            return False
        partial = json.loads(self._rec.PartialResult()).get("partial", "")
        if self._has_kw(partial):
            self._hit_words = None  # la confianza se mide al cerrar (finish)
            return True
        return False

    def finish(self) -> WakeResult:
        words = self._hit_words
        text = ""
        if words is None:
            res = json.loads(self._rec.FinalResult())
            words = res.get("result", [])
            text = res.get("text", "")
        else:
            text = " ".join(w.get("word", "") for w in words)
        conf = max((float(w.get("conf", 0.0)) for w in words if normalize(w.get("word", "")) == self._kw), default=None)
        self.reset()
        return WakeResult(text=text, conf=conf)

    def reset(self) -> None:
        self._rec.Reset()
        self._hit_words = None


class VoskYesNo:
    def __init__(self, model) -> None:
        from vosk import KaldiRecognizer

        grammar = json.dumps(VOSK_YESNO_WORDS + ["[unk]"], ensure_ascii=False)
        self._rec = KaldiRecognizer(model, 16000, grammar)

    def recognize(self, pcm: bytes) -> str | None:
        self._rec.Reset()
        for i in range(0, len(pcm), 8000):
            self._rec.AcceptWaveform(pcm[i : i + 8000])
        text = json.loads(self._rec.FinalResult()).get("text", "")
        if "[unk]" in text:
            return None  # algo fuera de la gramática: que decida Whisper
        tokens = normalize(text).split()
        if not tokens:
            return None
        if all(t in YES_TOKENS for t in tokens):
            return "yes"
        if any(t in NO_TOKENS for t in tokens):
            return "no"
        return None
