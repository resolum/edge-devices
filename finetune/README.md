# Ajuste fino del intérprete (Qwen) para Alivia

**No empieces por aquí.** Primero mide el modelo base; solo ajusta si no cumple.

## 1. Mide el modelo base

```bash
python scripts/make_dataset.py                       # data/llm/*.jsonl (sintético)
python scripts/eval_llm.py --model qwen2.5:3b        # casos escritos a mano + casas de prueba
```

Cada orden se evalúa con el mismo prompt, esquema y validación que usa el edge. Mira sobre todo:

- `exacto`: mismo estado y mismas acciones que la respuesta correcta.
- `acciones_de_mas`: propuso algo que no debía (lo peor; igual pasa por confirmación hablada).
- `por_tipo`: dónde falla (ambigüedades, correcciones, escenas...).

Si el modelo base ya cumple tu umbral (por ejemplo `--min-exact 0.9 --max-extra 0.02`), **no ajustes nada**: ya está.
Primero prueba cambios baratos: reformular el prompt en `commands/infrastructure/llm.py` y volver a medir.

> Los casos generados salen de plantillas; el modelo ajustado las aprenderá y su nota en ese conjunto será optimista.
> La medida honesta son los casos escritos a mano (`tuning/handwritten.py`) y tus propias frases reales:
> agrega las que el edge entienda mal a un `.eval.jsonl` y pásalo con `--extra`.

## 2. Ajusta (solo si hace falta)

```bash
pip install -r finetune/requirements.txt             # entorno aparte
python finetune/train_lora.py --base Qwen/Qwen2.5-3B-Instruct --revision <commit> --merge
```

Con ~2 400 muestras y una RTX 3060 de 6 GB calcula del orden de media hora a una hora por época; si se queda sin memoria baja
`--max-len` o usa un Colab/Kaggle (los datos son sintéticos). Estos tiempos son estimaciones, no medidas.

## 3. Conviértelo e impórtalo en Ollama

```bash
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
python convert_hf_to_gguf.py ../out/alivia-lora-merged --outfile alivia-f16.gguf --outtype f16
./llama-quantize alivia-f16.gguf alivia-q4_k_m.gguf Q4_K_M
cd .. && cp finetune/Modelfile.example Modelfile      # ajusta ruta y copia TEMPLATE/stop del modelo base
ollama create alivia-qwen -f Modelfile
```

## 4. Compáralo y decide

```bash
python scripts/eval_llm.py --model qwen2.5:3b alivia-qwen --min-exact 0.9 --max-extra 0.02
```

Solo si el ajustado gana en los casos **escritos a mano** y no empeora `acciones_de_mas`, ponlo en `[llm].model` y registra su huella:

```bash
python scripts/verify_models.py --pin-ollama alivia-qwen
```

## Licencia

`Qwen2.5-3B-Instruct` usa la licencia **Qwen Research** (solo uso no comercial), y un modelo ajustado a partir de él la hereda.
Está bien para el proyecto académico. Para comercializar Alivia (Resolum) hay que pedir licencia a Alibaba Cloud o ajustar
un modelo con licencia permisiva (por ejemplo, uno Apache 2.0): repite los pasos con `--base` y vuelve a medir.
