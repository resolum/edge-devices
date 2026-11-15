#!/usr/bin/env python
"""Genera el conjunto de datos sintético para ajustar y evaluar el intérprete (LLM).

  python scripts/make_dataset.py                      # ./data/llm  (train.jsonl, val.jsonl, *.eval.jsonl)
  python scripts/make_dataset.py --train-houses 120 --per-house 50

Todo es sintético (casas y órdenes inventadas): no hay voz ni datos de nadie, se puede subir a un Colab sin problema.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from alivia_edge.tuning import dataset  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/llm")
    ap.add_argument("--train-houses", type=int, default=60)
    ap.add_argument("--val-houses", type=int, default=8)
    ap.add_argument("--test-houses", type=int, default=12)
    ap.add_argument("--per-house", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    splits = dataset.build(a.train_houses, a.val_houses, a.test_houses, a.per_house, a.seed)
    counts = dataset.write(a.out, splits, a.seed)
    print(f"Escrito en {a.out}: {counts}")
    print("Tipos (train):", dict(Counter(s.tag for s in splits["train"]).most_common()))


if __name__ == "__main__":
    main()
