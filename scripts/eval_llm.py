#!/usr/bin/env python
"""Mide qué tan bien interpreta un modelo de Ollama las órdenes (y cuánto tarda), igual que en producción.

  python scripts/eval_llm.py --model qwen2.5:3b                       # base, sobre los casos a mano + data/llm/test
  python scripts/eval_llm.py --model qwen2.5:3b alivia-qwen           # compara base y ajustado
  python scripts/eval_llm.py --model alivia-qwen --min-exact 0.9 --max-extra 0.02   # sale con 1 si no cumple

Usa el mismo prompt, esquema y validación que el edge (OllamaLlm). Necesita Ollama corriendo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from alivia_edge.commands.infrastructure.llm import OllamaLlm  # noqa: E402
from alivia_edge.shared.config import LlmCfg  # noqa: E402
from alivia_edge.tuning import evaluate as ev  # noqa: E402
from alivia_edge.tuning import handwritten  # noqa: E402


async def run_model(model: str, args, cases) -> ev.Report:
    cfg = LlmCfg(url=args.url, model=model, timeout_s=args.timeout, keep_alive="10m", num_ctx=args.num_ctx)
    llm = OllamaLlm(cfg)
    await llm.start()
    try:
        await llm.model_digest()
        first = cases[0]
        await llm.interpret(first.history, first.household.mic_room, first.household.catalog())  # calienta: no cuenta en la latencia
        return await ev.evaluate(cases, llm.interpret)
    finally:
        await llm.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", nargs="+", default=["qwen2.5:3b"])
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--data", default="data/llm/test.eval.jsonl", help="casos generados (si existe)")
    ap.add_argument("--extra", help="otro .eval.jsonl con casos reales tuyos")
    ap.add_argument("--no-handwritten", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--report", help="guarda el resumen en este JSON")
    ap.add_argument("--min-exact", type=float, default=0.0)
    ap.add_argument("--max-extra", type=float, default=1.0)
    ap.add_argument("--max-invalid", type=float, default=1.0)
    args = ap.parse_args()

    cases: list[ev.Case] = []
    if not args.no_handwritten:
        cases += ev.from_samples(handwritten.cases())
    for path in (args.data, args.extra):
        if path and Path(path).is_file():
            cases += ev.load_cases(path)[: args.limit]
    if not cases:
        print("No hay casos: ejecuta scripts/make_dataset.py o no uses --no-handwritten", file=sys.stderr)
        return 2

    ok = True
    summaries = {}
    for model in args.model:
        print(f"\n=== {model}  ({len(cases)} casos) ===")
        rep = asyncio.run(run_model(model, args, cases))
        s = rep.summary()
        summaries[model] = s
        print(json.dumps({k: v for k, v in s.items() if k != "por_tipo"}, ensure_ascii=False, indent=1))
        print("por tipo:", s["por_tipo"])
        for f in rep.failures[:10]:
            print("  FALLO", f)
        if s["exacto"] < args.min_exact or s["acciones_de_mas"] > args.max_extra or s["json_invalido"] > args.max_invalid:
            ok = False
            print(f"  NO CUMPLE: exacto>={args.min_exact}, acciones_de_mas<={args.max_extra}, json_invalido<={args.max_invalid}")
    if args.report:
        Path(args.report).write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
