#!/usr/bin/env python
"""Ajuste fino (QLoRA) del intérprete de órdenes de Alivia con el conjunto de datos sintético.

  python scripts/make_dataset.py
  python finetune/train_lora.py --base Qwen/Qwen2.5-3B-Instruct --data data/llm --out out/alivia-lora --merge

Pensado para una GPU de 6 GB (RTX 3060 portátil): 4 bits, lote 1 con acumulación, checkpointing. Si no alcanza la
memoria, baja --max-len o usa un Colab/Kaggle con T4 (los datos son sintéticos, no hay nada privado que subir).

Seguridad: solo pesos en safetensors (use_safetensors=True) y trust_remote_code=False: no se ejecuta código del repositorio
del modelo ni se deserializan pickles. Fija --revision a un commit concreto para que el modelo base no cambie entre corridas.

LICENCIA: Qwen2.5-3B-Instruct es solo para uso no comercial (Qwen Research). Un modelo ajustado a partir de él hereda esa
restricción. Si Alivia se va a comercializar, ajusta un modelo con licencia permisiva (p. ej. Apache 2.0) y pásalo a --base.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def load_rows(path: Path) -> list[list[dict]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line)["messages"])
    return rows


def encode(tok, messages: list[dict], max_len: int):
    """Entrena solo con la respuesta final del asistente (el JSON); el prompt y los turnos previos no cuentan en la pérdida."""
    prompt_ids = tok.apply_chat_template(messages[:-1], add_generation_prompt=True, tokenize=True)
    full_ids = tok.apply_chat_template(messages, add_generation_prompt=False, tokenize=True)
    if full_ids[: len(prompt_ids)] != prompt_ids or len(full_ids) > max_len:
        return None
    labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids):]
    return {"input_ids": full_ids, "attention_mask": [1] * len(full_ids), "labels": labels}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="Qwen/Qwen2.5-3B-Instruct")
    ap.add_argument("--revision", default=None, help="commit del repositorio del modelo base (recomendado)")
    ap.add_argument("--data", default="data/llm")
    ap.add_argument("--out", default="out/alivia-lora")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--max-len", type=int, default=1536)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--merge", action="store_true", help="al terminar, fusiona el adaptador en pesos completos (para convertir a GGUF)")
    args = ap.parse_args()

    import torch
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, DataCollatorForSeq2Seq, Trainer,
                              TrainingArguments)

    if "Qwen2.5-3B" in args.base:
        print("AVISO de licencia: Qwen2.5-3B-Instruct es de uso no comercial (Qwen Research).", file=sys.stderr)
    if not torch.cuda.is_available():
        print("No hay GPU CUDA: entrenar en CPU no es viable. Usa la GPU o un Colab.", file=sys.stderr)
        return 2
    bf16 = torch.cuda.is_bf16_supported()
    dtype = torch.bfloat16 if bf16 else torch.float16

    tok = AutoTokenizer.from_pretrained(args.base, revision=args.revision, trust_remote_code=False)
    data_dir = Path(args.data)
    train = [e for m in load_rows(data_dir / "train.jsonl") if (e := encode(tok, m, args.max_len))]
    val = [e for m in load_rows(data_dir / "val.jsonl") if (e := encode(tok, m, args.max_len))]
    random.Random(7).shuffle(train)
    print(f"Muestras: {len(train)} entrenamiento, {len(val)} validación (descartadas por largo: se reportan arriba/abajo)")
    if not train:
        print("Ninguna muestra cabe en --max-len; súbelo", file=sys.stderr)
        return 2

    model = AutoModelForCausalLM.from_pretrained(
        args.base, revision=args.revision, trust_remote_code=False, use_safetensors=True,
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype),
        torch_dtype=dtype, device_map={"": 0},
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05, task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    ))
    model.print_trainable_parameters()

    targs = TrainingArguments(
        output_dir=args.out, num_train_epochs=args.epochs, learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
        per_device_train_batch_size=1, per_device_eval_batch_size=1, gradient_accumulation_steps=args.grad_accum,
        bf16=bf16, fp16=not bf16, gradient_checkpointing=True, optim="paged_adamw_8bit", logging_steps=10,
        eval_strategy="steps", eval_steps=50, save_strategy="steps", save_steps=50, save_total_limit=2,
        load_best_model_at_end=True, metric_for_best_model="eval_loss", report_to="none", seed=7,
    )
    Trainer(model=model, args=targs, train_dataset=train, eval_dataset=val or None,
            data_collator=DataCollatorForSeq2Seq(tok, padding=True, label_pad_token_id=-100)).train()
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"Adaptador guardado en {args.out}")

    if args.merge:
        del model
        torch.cuda.empty_cache()
        base = AutoModelForCausalLM.from_pretrained(args.base, revision=args.revision, trust_remote_code=False, use_safetensors=True, torch_dtype=dtype)
        merged = PeftModel.from_pretrained(base, args.out).merge_and_unload()
        merged.save_pretrained(f"{args.out}-merged", safe_serialization=True)
        tok.save_pretrained(f"{args.out}-merged")
        print(f"Modelo fusionado en {args.out}-merged. Siguiente: finetune/README.md (convertir a GGUF e importar en Ollama).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
