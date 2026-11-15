# Modelos: verificación, licencias, seguridad y rendimiento

Estado al 10 de noviembre de 2026. Qué se comprobó contra fuentes reales y qué no se pudo comprobar desde el entorno de desarrollo.

## 1. Quién es quién

| Etapa | Modelo | Dónde vive realmente | ¿Está en Hugging Face? | Licencia | Formato |
|---|---|---|---|---|---|
| Palabra de activación | `vosk-model-small-es-0.42` (39 MB, no 50) | alphacephei.com/vosk/models (oficial) | No oficial. Solo hay espejos de terceros | Apache 2.0 | Carpeta Kaldi dentro de un zip |
| Fin de frase | Silero VAD v6.2.3 (2,3 MB) | GitHub `snakers4/silero-vad` y paquete PyPI `silero-vad` | Solo espejos de terceros | MIT (texto leído en el paquete oficial) | ONNX |
| Transcripción | Whisper large-v3-turbo en CTranslate2 | HF `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | Sí | MIT | CTranslate2 (`model.bin`), FP16 |
| Comprensión | Qwen2.5-3B-Instruct | HF `Qwen/Qwen2.5-3B-Instruct` (organización oficial); Ollama `qwen2.5:3b` | Sí | **Qwen Research: solo uso no comercial** | safetensors; en Ollama, GGUF Q4 |
| Voz | Piper `es_MX-claude-high` (63 MB) | HF `rhasspy/piper-voices`, ruta `es/es_MX/claude/high` | Sí | Voz Apache 2.0 (la voz `ald` es Unlicense); repositorio MIT | ONNX + JSON |

Aclaraciones sobre lo que no coincidía con la tabla inicial:

- **Vosk y Silero no son de Hugging Face.** Sus fuentes oficiales son Alpha Cephei y Silero (GitHub y PyPI). Los espejos en HF son de terceros; en uno de ellos el zip de Vosk aparece marcado por el escáner como "pickle". Por eso el script descarga de la fuente oficial.
- **Whisper:** el nombre `large-v3-turbo` se resuelve dentro de faster-whisper 1.2.1 al repositorio `mobiuslabsgmbh/faster-whisper-large-v3-turbo`, no al de Systran. Es una conversión de `openai/whisper-large-v3-turbo`, no un modelo publicado por OpenAI. Cifras de memoria de la tabla confirmadas con el benchmark público de faster-whisper: FP16 unos 2,5 GB de VRAM, int8 unos 1,5 GB.
- **Piper:** la voz `claude` es Apache 2.0. Pero el **paquete `piper-tts`** (el motor) pasó a GPL-3.0 al mudarse a `OHF-Voice/piper1-gpl` (el repositorio original MIT quedó archivado en octubre de 2025).

## 2. Dos asuntos de licencia que tienes que decidir

**Qwen2.5-3B-Instruct no se puede usar comercialmente.** Es de las pocas tallas de Qwen2.5 con la licencia Qwen Research (la definición de "no comercial" es investigación o evaluación; el uso comercial exige pedir licencia a Alibaba Cloud). Para el proyecto académico está bien. Para Resolum como producto no. Opciones, en orden de esfuerzo:

1. Seguir así mientras sea académico (el edge lo avisa en el arranque).
2. Cambiar a un modelo con licencia permisiva. `Qwen3-4B-Instruct-2507` figura como Apache 2.0 en Hugging Face. Confirma el nombre exacto de la etiqueta en ollama.com/library y mídelo con `scripts/eval_llm.py` antes de adoptarlo. Un modelo de 4B ocupa algo más de VRAM: revísalo con `scripts/bench.py`.
3. Pedir licencia a Alibaba Cloud.

Un modelo ajustado a partir de Qwen2.5-3B hereda la restricción.

**`piper-tts` es GPL-3.0.** Usarlo en tu propio equipo no obliga a nada. Si distribuyes el edge a clientes, la GPL te obliga a ofrecer el código de lo que distribuyas junto a él. La salida limpia es `[tts] backend = "exe"` (el binario de Piper corre como proceso aparte) o cambiar de motor. Esto lo debe confirmar alguien que sepa de licencias antes de comercializar; no es asesoría legal.

## 3. Seguridad: lo que se comprobó y lo que no

Comprobado:

- **Silero:** el `.onnx` de la etiqueta v6.2.3 de GitHub y el del paquete oficial de PyPI tienen el mismo SHA-256 (`1a153a22...`). Ese hash viene fijado en `models.lock.json`. Se probó con tu clase `SileroVad`: silencio 0,009, voz sintética 0,999.
- **Antes se descargaba de `master`** (el archivo podía cambiar sin aviso). Ahora se descarga de la etiqueta v6.2.3 y se exige el hash.
- **Formatos:** ONNX, CTranslate2, GGUF y safetensors no son pickle: cargarlos no ejecuta código del modelo. El script de ajuste usa `use_safetensors=True` y `trust_remote_code=False`.
- **Zip de Vosk:** la extracción rechaza rutas con `..`, rutas absolutas, enlaces simbólicos y zips desproporcionados (la extracción anterior no lo comprobaba por sí misma).
- **Descargas:** solo HTTPS, con tope de tamaño y borrado del parcial si falla.

No se pudo comprobar desde el entorno de desarrollo (huggingface.co y alphacephei.com están bloqueados ahí):

- Los hashes de Vosk, Piper, Whisper y Qwen. Quedan en `models.lock.json` como "primer uso": la primera descarga los registra y desde ahí cualquier cambio se rechaza. Eso no prueba que la primera descarga fuera legítima: coteja el SHA-256 de cada archivo con el que muestra su página en Hugging Face, o con una segunda descarga desde otra red.
- Que los modelos carguen y funcionen con tu hardware. Tampoco se midió latencia real.

Comandos:

```bash
python scripts/fetch_models.py            # descarga, verifica y registra
python scripts/fetch_models.py --lock     # registra lo que ya tenías
python scripts/verify_models.py           # comprueba hashes sin descargar
python scripts/verify_models.py --pin-ollama qwen2.5:3b   # huella del modelo en Ollama
```

Al arrancar, el edge compara los hashes. Un modelo distinto al registrado impide arrancar; un modelo sin hash solo avisa (`[security] allow_unpinned = false` lo hace estricto). Para Whisper fija `[stt].revision` a un commit y para Qwen registra la huella con `--pin-ollama`.

## 4. Rendimiento

Cambios que ya están en el código:

| Ajuste | Qué hace |
|---|---|
| `compute_type = "auto"` según VRAM | GPU de menos de 8 GB usa `int8_float16` (Whisper baja de ~2,6 a ~1,6 GB y deja sitio al LLM); con más VRAM, `float16`; en CPU, `int8` |
| Pista de vocabulario para Whisper | El `initial_prompt` se arma con los nombres reales de tus dispositivos y habitaciones ("la luz del dormitorio"), no con un texto fijo |
| Descarte de eco del prompt | Whisper a veces devuelve el prompt con ruido; se descarta si trae el encabezado o copia casi todo. Una orden real no se descarta aunque aparezca en la pista |
| `seed` y `num_predict` en Ollama | Misma frase, misma interpretación; el JSON de una orden cabe en menos de 150 tokens |
| Contracción "del" | "la luz de el dormitorio" ahora es "la luz del dormitorio" (lo oía el usuario y lo veía el LLM) |

Estimación de memoria en una RTX 3060 de 6 GB (cifras de referencia, no medidas aquí): Whisper `int8_float16` ~1,6 GB + Qwen 3B Q4 ~2,2 GB + sobrecarga de CUDA ~0,8 GB, unos 4,6 GB. Con Whisper en `float16` suben a ~5,6 GB y queda casi sin margen.

Mide en tu equipo, etapa por etapa:

```bash
python scripts/bench.py                  # VAD, Vosk, Piper, Whisper (con y sin pista) y LLM
python scripts/bench.py --only stt llm
```

La espera entre que terminas de hablar y escuchar la confirmación es la suma de: silencio de fin de frase (`[vad].end_silence_ms`, 700 ms) + Whisper + LLM + primer audio de Piper. El benchmark usa audio sintetizado de Piper con ruido, sirve para comparar configuraciones, no reemplaza probar con tu voz.

## 5. Ajuste fino

Ver `finetune/README.md`. Resumen: Qwen se puede ajustar (QLoRA con datos sintéticos que usan el mismo prompt del edge) y hay una evaluación que mide aciertos y "acciones de más". **Primero mide el modelo base**: si ya cumple, no ajustes.

Lo que no se ajusta, y por qué:

- **Vosk:** ya está restringido a una gramática cerrada con la palabra de activación; no hay nada que ajustar. Si falla, sube o baja `[wake].min_conf`.
- **Silero y Piper:** no aplica. Silero se afina con umbrales (`[vad]`) y Piper con `length_scale`.
- **Whisper:** el ajuste fino exige cientos de grabaciones reales de tus usuarios con su transcripción, y no las hay. La pista de vocabulario da gran parte del beneficio sin entrenar. Si más adelante recolectas audios con consentimiento, se puede ajustar.

## Virus y malware en modelos
Qué se hace (`scripts/scan_models.py`, también automático dentro de `fetch_models.py`):
- Análisis estático: detecta pickles que importan `os`, `subprocess`, `eval`, etc. (sin deserializarlos), ejecutables
  y scripts dentro de las carpetas de modelo, enlaces simbólicos, zips anidados y cabeceras safetensors/GGUF falsas.
  Un hallazgo grave borra el archivo recién descargado.
- ClamAV opcional (`--clamav`) y hashes para VirusTotal (`--hashes`).
- Los formatos usados (ONNX, CTranslate2 `model.bin`, safetensors, GGUF) no ejecutan código al cargarse; el riesgo real
  era pickle y paquetes pip maliciosos. Instala solo los paquetes de `pyproject.toml`.

Qué NO se puede garantizar: un antivirus no detecta pesos envenenados ni puertas traseras de comportamiento. La
mitigación es proceder de fuentes oficiales, fijar hashes y que el LLM solo pueda devolver JSON validado contra el
catálogo (no ejecuta nada por sí mismo). En el sandbox de desarrollo no se pudo escanear Vosk, Piper, Whisper ni Qwen
(sin acceso a Hugging Face); solo `silero_vad.onnx`, que salió limpio. Hay que correr el escaneo en tu Mac.
