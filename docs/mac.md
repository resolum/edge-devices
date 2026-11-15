# Compilar y correr Alivia Edge en una Mac (16 GB)

## Resultado de la revisión de paquetes (Python 3.12, Apple Silicon)
Todas las dependencias tienen wheel precompilado para macOS arm64, no hace falta compilar nada:
vosk, piper-tts, ctranslate2, onnxruntime, av, numpy, scipy (macosx_12_0), websockets, pyserial, paho-mqtt, httpx,
peewee, faster-whisper. Usa Python 3.12 y un entorno virtual: `python3.12 -m venv .venv && source .venv/bin/activate`.

## Memoria (16 GB, memoria unificada)
| Componente | Aproximado |
|---|---|
| Whisper large-v3-turbo (CPU, int8) | ~1,6 GB |
| Qwen2.5-3B en Ollama (Metal, Q4) | ~2 GB |
| Vosk small + Silero + Piper | < 0,5 GB |
| Broker + backend local + SQLite | < 0,3 GB |
Cabe con holgura. No necesitas cuantizar más.

## Diferencias con Linux/CUDA
- **Whisper corre solo en CPU.** CTranslate2 no tiene backend Metal. `choose_stt_runtime` ya elige `int8` en CPU.
  Espera un par de segundos por frase corta. Si es lento, sube `[stt] model` a `small`/`medium` o pide el adaptador
  mlx-whisper / whisper.cpp (usan la GPU del Mac) detrás del puerto `Stt`.
- **Ollama sí usa Metal** (GPU del chip M). `brew install ollama` y `ollama pull qwen2.5:3b`.
- **Fine-tuning con `finetune/train_lora.py` NO corre en Mac**: usa bitsandbytes (solo CUDA). Entrena en una GPU
  (Colab, otro PC) y trae el GGUF resultante. La evaluación (`scripts/eval_llm.py`) sí corre en Mac.
- **Bluetooth SPP** aparece como `/dev/cu.<nombre>` (no `/dev/rfcomm0`). Ajusta el puerto en la config.
- **Micrófono/altavoz**: macOS pide permiso la primera vez que una terminal usa el micrófono.
- **Gatekeeper**: si un binario descargado (p. ej. el ejecutable de Piper con `[tts] backend="exe"`) queda bloqueado,
  no lo desbloquees sin comprobarlo antes con `scripts/scan_models.py --clamav` y el hash en virustotal.com.

## Verificación de seguridad de modelos (paso a paso en la Mac)
```bash
python scripts/fetch_models.py          # descarga, verifica hash y hace escaneo estático
python scripts/verify_models.py         # hashes contra models.lock.json
brew install clamav && freshclam        # antivirus (una vez)
python scripts/scan_models.py --clamav --hashes
```
Con `--hashes` puedes pegar cada SHA-256 en virustotal.com (buscar por hash, no hace falta subir el archivo).
Para Whisper, cotejar el SHA-256 de `model.bin` con el que muestra la página del repo en Hugging Face.
