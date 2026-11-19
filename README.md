# alivia-edge - edge de voz de Alivia

Servicio Python (asyncio) que corre en tu PC. Recibe el audio del micrófono de tu ESP32 (WiFi/WebSocket o Bluetooth),
detecta "Alivia", conversa con el usuario por el parlante del mismo ESP32 y, **solo tras una confirmación hablada**, manda
el comando por MQTT al edge de actuadores.

```
ESP32 (INMP441 + MAX98357A) ── WebSocket :8765/ws  ┐
                            └─ Bluetooth SPP (COM)  ┴─►  EDGE DE VOZ (este proyecto)
                                                           Vosk (palabra de activación)
                                                           Silero VAD (fin de frase)
                                                           faster-whisper large-v3-turbo
                                                           Qwen 2.5 3B (Ollama) → JSON validado vs catálogo
                                                           Piper es_MX → PCM 16 kHz → parlante del ESP32
                                                                │
                                              Mosquitto (MQTT)  ▼  alivia/actuators/<deviceId>/command|result
                                                           EDGE DE ACTUADORES (aparte) → puerta / luz / ventana
```

## Flujo de una conversación

1. **Escuchando:** Vosk con gramática cerrada (`alivia`, `oye alivia`, `ey alivia`...). Se valida con la confianza de Vosk
   para evitar falsos disparos.
2. **Frase completa o solo la palabra:** si tras "Alivia" hay más voz ("Alivia, cierra la puerta"), se transcribe todo y se
   sigue; si solo dijo "Alivia", Piper responde *"Dígame"* (o *"Sí"*, *"¿Qué desea?"*) y se abre una ventana de ~10 s
   (se cierra antes si pasan 4 s sin hablar o cuando Silero detecta fin de frase).
3. **Whisper** transcribe; **Qwen** devuelve un JSON `{status, scene, actions, question}` restringido por esquema a los ids
   y acciones del catálogo. Nada fuera del catálogo llega a ejecutarse.
4. **Aclaración:** si Qwen duda pero tiene una hipótesis → *"No estoy seguro de haber entendido. ¿Quiere que abra la
   puerta?"*; si falta información → la pregunta aclaratoria de Qwen. Máximo 2 rondas; luego *"No logré entenderle"*.
5. **Confirmación obligatoria:** *"¿Quiere que cierre la puerta, apague la luz y cierre la ventana?"* - la pregunta se arma
   **en código** desde las acciones ya validadas, no con texto del LLM. Responde "sí"/"no" (Vosk rápido; si no es
   concluyente, Whisper). "No, la ventana" se reinterpreta con el contexto. Sin respuesta en 8 s → se cancela.
6. **Ejecución:** un comando MQTT por acción, con `correlationId`. Se espera el resultado (5 s por defecto) y Piper informa:
   *"Listo. La puerta quedó cerrada, la luz quedó apagada…"*, o *"…pero no pude apagar la luz"* / *"no recibí confirmación…"*.
7. Mientras suena el parlante, el micrófono se **ignora** (anti-eco) y vuelve a *Escuchando*.

## Pedido de ayuda y batería baja (avisos al familiar)

- **Por voz:** si lo que se dice con la palabra de activación (o después del saludo) es un pedido de ayuda («Alivia,
  ayuda», «auxilio», «me caí», «no puedo levantarme», «me siento mal», «llama a mi hija»...), no pasa por el LLM ni pide
  confirmación: el edge avisa al backend en `POST /api/v1/edge/events` (`HELP_REQUESTED`, con lo que dijo la persona) y
  responde «Ya estoy avisando a su familia». «Ayúdame a cerrar la puerta» sigue siendo una orden.
- **Con el botón del nodo:** el ESP32 envía `{"type":"help_request"}` por el mismo enlace (WiFi o Bluetooth).
- **Batería baja:** el ESP32 envía `{"type":"telemetry","batteryPct":<0-100>}`. Se avisa una vez al bajar de
  `[backend].low_battery_pct` y no otra vez hasta recargar por encima de `battery_rearm_pct`.

El backend notifica con una notificación push a los familiares a cargo del hogar (la ayuda llega como alerta urgente).
Los eventos esperan en una cola en memoria y se reintentan mientras no haya red; cada uno lleva un `eventId`, así que
reenviarlo no notifica dos veces.

## Instalación

Requisitos: Python 3.11+, [Ollama](https://ollama.com), y (cuando tengas actuadores) Mosquitto.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows      |   source .venv/bin/activate   (Linux/macOS)
pip install -e .
python scripts/fetch_models.py    # Vosk es (pequeño), voz Piper es_MX, Silero VAD  → ./models
ollama pull qwen2.5:3b
```

Whisper `large-v3-turbo` se descarga solo en el primer arranque (~1.6 GB).
Si `pip` no instala `piper-tts` en tu sistema, baja el binario de Piper y usa `[tts] backend = "exe"` (ver `config.example.toml`).

> **Rendimiento:** `large-v3-turbo` va fluido con GPU NVIDIA (`device = "cuda"`). Solo con CPU, la transcripción de una
> frase corta puede tardar varios segundos; si se siente lento prueba `model = "small"` o `"medium"` en `[stt]`.

## Configuración

```bash
cp config.example.toml config.toml        # (Windows: copy)
set ALIVIA_WS_TOKEN=<token del sketch>    # Windows   |   export ALIVIA_WS_TOKEN=...
set ALIVIA_EDGE_CREDENTIAL=<edgeId>.<secret>
```

- `config.toml`: token local, `[backend]`, `[database]`, `[rooms.labels]`, Bluetooth, modelos y tiempos.
- **No hay catálogo de dispositivos local.** El catálogo (puertas, luces, ventanas, cortinas) se construye solo con los
  actuadores que el backend asigna a la instalación y que están `IN_USE`. El nombre hablado es `<tipo> de <etiqueta del
  cuarto>`, con las etiquetas de `[rooms.labels]`.
- Valores del sketch que deben coincidir: `docs/firmware-notas.md` (el `hello` ahora lleva `mac`).

### Alta del edge en el backend (una vez)

1. Registrar el edge con su MAC y tipo `MICROPHONE_EDGE`.
2. Colocarlo en la instalación y emitir su credencial (se muestra una sola vez).
3. Poner la credencial en `ALIVIA_EDGE_CREDENTIAL` o `[backend].credential`.
4. Registrar el ESP32 como dispositivo MICROPHONE con su MAC y asignarlo; dejarlo `IN_USE`.

El timeout de lectura HTTP debe superar los 25 s del long poll; si hay proxy inverso, debe permitir peticiones largas.

## Cómo decide el acceso

El edge descarga del backend la tabla de dispositivos (`GET /api/v1/edge/device-table`, con ETag y long poll), la guarda
en SQLite y confirma la versión aplicada (`PUT /api/v1/edge/sync-state`).

- Un dispositivo entra solo si está en la tabla y su estado es `IN_USE`. La identidad es la MAC.
- Sin tabla se niega todo. Con 401 se conserva la última tabla y se registra una alarma. Con 404 se borra la tabla y se niega todo.
- Si la tabla cambia, las sesiones de micrófonos que pierden acceso se cierran con `acceso revocado`.
- Cada acción se vuelve a autorizar justo antes de publicar el comando MQTT.
- Sin internet funciona con la última tabla guardada.

## Modelos: verificación, licencias y ajuste

- `python scripts/fetch_models.py` descarga de la fuente oficial y verifica SHA-256 (`models.lock.json`); `scripts/verify_models.py` los revisa sin descargar.
- Vosk y Silero **no** están en Hugging Face (fuente oficial: Alpha Cephei y Silero); Whisper, Qwen y Piper sí.
- **Qwen2.5-3B es de uso no comercial** y **`piper-tts` es GPL-3.0**: está bien para el proyecto académico, hay que decidir antes de comercializar. Todo en `docs/modelos.md`.
- `python scripts/bench.py` mide cada etapa en tu equipo; `python scripts/eval_llm.py` mide el intérprete (aciertos, acciones de más, latencia).
- Ajuste fino del LLM (solo si el modelo base no cumple): `finetune/README.md`.

## Ejecutar

```bash
python -m alivia_edge --check             # carga modelos/Ollama/MQTT y sale (diagnóstico)
python -m alivia_edge                     # arranca; verás "WebSocket escuchando en ws://0.0.0.0:8765/ws"
```

Luego enciende el ESP32 (`EDGE_HOST` = IP de tu PC). Abre el puerto 8765 en el firewall de Windows si no conecta.
Logs útiles: `palabra de activación detectada`, `Whisper (…s): '…'`, `interpretación: …`, `dice: …`, `MQTT -> …`.

## Probar sin el edge de actuadores

- `[mqtt] enabled = false` → **modo simulación**: el flujo de voz completo corre y responde éxito sin enviar nada
  (se avisa con un WARNING al arrancar).
- Con Mosquitto real: `python scripts/actuator_sim.py` (actuador de mentira; `--fail luz`, `--silent ventana` para
  simular errores y timeouts). Contrato completo: `docs/mqtt-contract.md`.

## Tests

```bash
pip install -e .[dev]
pytest
```

120 pruebas: integridad de modelos, evaluación del LLM, MAC, política de acceso, sincronización contra un backend falso con el contrato real (304, long poll, 409,
401, 404, 429/503), persistencia SQLite, tramas Bluetooth, VAD Silero real, y de punta a punta con un ESP32 falso por
WebSocket y Bluetooth (handshake con MAC, revocación en vivo, mantenimiento, confirmaciones, anti-eco, failover), más el
bus MQTT contra un broker real en proceso.

## Afinación (lo que depende de tu voz y tu cuarto)

| Síntoma | Ajuste |
|---|---|
| Se activa solo / con otras palabras | subir `[wake].min_conf` (0.5 → 0.7) |
| No me oye decir "Alivia" | bajar `min_conf` (o 0 para no validar); subir la ganancia del micrófono en el sketch |
| Corta mi frase a la mitad | subir `[vad].end_silence_ms` (700 → 900) |
| Tarda en reaccionar tras mi frase | bajar `[vad].end_silence_ms` |
| Se oye a sí mismo | subir `[speaker].tail_ms`; revisar volumen/ubicación del parlante |
| "Alivia" entra a la orden como "Olivia" | ya se filtra; añade variantes a `[wake].strip_names` |

## Qué NO está probado todavía

Probé con modelos y ESP32 simulados; **no** con Vosk/Whisper/Piper/Ollama reales ni con tu hardware (este entorno no puede
descargar sus modelos). Lo más probable que necesite ajuste en tu primera sesión real: `min_conf`, `end_silence_ms`,
`command_min_speech_ms` y el prompt de Qwen según cómo hables. Revisa los logs y dime qué ves.

## Estructura (DDD)

```
src/alivia_edge/
  access/     tabla de acceso del backend
    domain/ (MacAddress, NodeRef, EdgeAccessTable, AccessPolicy)  application/ (AccessService, AccessSyncService)
    infrastructure/ (HttpBackendGateway, SQLite)
  voice/      sesión de voz por dispositivo
    domain/  application/ (DeviceSession, Dialog)  interfaces/ (WebSocket, Bluetooth, protocolo)
    infrastructure/audio/ (Vosk, Silero, Whisper, Piper)  integration/ (MicrophoneAccessGate)
  commands/   catálogo dinámico, LLM, despacho
    domain/ (Catalog, Action, CommandRequest)  application/ (CatalogService, CommandDispatcher)
    infrastructure/ (OllamaLlm, MqttBus, historial SQLite)  integration/ (AccessAntiCorruptionLayer)
  shared/     config, texto en español, base de datos
  app.py      raíz de composición
```

Más detalle en `docs/adrs.md`, `docs/class-diagram.puml` y `docs/user-stories.md`.
