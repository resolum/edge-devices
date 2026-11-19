# Notas para el firmware (ESP32 con ModestIoT)

Lo que el edge espera de tu sketch `AliviaMicrophone.ino` y lo que hay que saber del respaldo por Bluetooth.

## Valores que deben coincidir

| Sketch | Edge (`config.toml`) |
|---|---|
| `WiFi.macAddress()` (nuevo campo `mac` del `hello`) | la MAC registrada en el backend para este micrófono; si usas Bluetooth, `[[bluetooth.links]] mac` |
| `DEVICE_ID "alivia-mic-01"` | solo etiqueta de log; ya no identifica al dispositivo |
| `EDGE_TOKEN "renatin"` | `[server].token` o variable `ALIVIA_WS_TOKEN` |
| `EDGE_HOST` | la IP del PC que corre el edge (`ipconfig` / `ip a`) |
| `EDGE_PORT 8765`, `EDGE_PATH "/ws"` | `[server].port`, `[server].path` |
| `AUDIO_SAMPLE_RATE 16000` | fijo: el edge rechaza otra frecuencia en el `hello` |

## Cambio obligatorio en el firmware: `mac` en el `hello`

El `hello` debe incluir `"mac": WiFi.macAddress()` (formato `AA:BB:CC:DD:EE:FF`). Sin ella el edge rechaza la conexión con
"falta la MAC del dispositivo". El edge decide el acceso con la tabla que le entrega el backend: el dispositivo debe estar
en la tabla, ser de tipo MICROPHONE y tener estado IN_USE. Si el backend lo pasa a MAINTENANCE, BROKEN o RETIRED, o lo
desasigna, el edge corta la sesión con `acceso revocado`. Sin tabla se niega todo.

El token `EDGE_TOKEN "renatin"` se mantiene como emparejamiento local, pero cámbialo antes de producción (es público en el repositorio).

## WiFi y Bluetooth

El edge escucha **los dos a la vez** y trata igual el audio de cualquiera de los dos. Si el mismo nodo aparece por WiFi y
por Bluetooth, **gana WiFi** (más ancho de banda); Bluetooth solo entra cuando el WiFi no está.

**Importante: el firmware actual elige el transporte al compilar** (`USE_BLUETOOTH` 0 o 1 en el sketch). No cambia solo de
WiFi a Bluetooth cuando se cae la red. Con el sketch tal como está, el respaldo funciona así:

1. Compilas con `USE_BLUETOOTH 1` (nodo por Bluetooth) o con `0` (nodo por WiFi), y
2. el edge soporta ambos sin tocar nada del lado del PC.

Para que el **nodo mismo** conmute en caliente (WiFi → Bluetooth al perder el router, y volver) hace falta un driver de
conectividad nuevo en el firmware que intente WiFi, y si no responde en N segundos abra Bluetooth. Eso es trabajo de
firmware aparte (WiFi y Bluetooth clásico a la vez en un ESP32 consumen mucha RAM y degradan el audio, así que hay que
probarlo bien). El lado del edge ya está preparado para ello.

> Sobre "si se pierde internet": todo el edge (Vosk, Whisper, Qwen, Piper, Mosquitto) corre **local**, sin internet. Con
> el router encendido el WiFi local sigue funcionando aunque no haya internet. Bluetooth solo hace falta si cae el propio
> WiFi/router.

## Bluetooth en el PC (el ESP32 es el servidor SPP)

1. Empareja el PC con `Alivia-Mic-01` (`BT_DEVICE_NAME`).
2. **Windows:** Configuración → Bluetooth → *Más opciones de Bluetooth* → pestaña *Puertos COM*: usa el puerto **saliente**
   ("Saliente", p. ej. `COM5`).
   **Linux:** `sudo rfcomm bind 0 <MAC-del-ESP32> 1` → `/dev/rfcomm0`.
3. En `config.toml`:

   ```toml
   [bluetooth]
   enabled = true
   [[bluetooth.links]]
   mac = "AA:BB:CC:00:00:01"
   port = "COM5"
   ```

El edge abre el puerto, reintenta con espera creciente (2 s → 15 s) si el nodo está fuera de alcance, y mientras el nodo
esté conectado por WiFi no intenta abrir Bluetooth.

## Comportamiento del firmware que conviene conocer

- El micrófono manda audio continuo (1280 B cada 40 ms); el edge no necesita secuencia ni marcas de tiempo.
- `hello` anuncia siempre `microphone` y `speaker`; el edge no depende de eso (asigna por `deviceId`).
- Mientras suena el parlante el edge **ignora el micrófono** (desde `audio_begin` hasta `playback_done` + 300 ms) para no
  oírse a sí mismo. Si tu parlante queda muy cerca del micrófono y aun así se activa solo, sube `[speaker].tail_ms`.
- `playback_done` puede perderse (el firmware lo descarta si el `ioLock` está ocupado 15 ms). El edge lo asume tras la
  duración del audio + `done_margin_s` (2 s), así que una pérdida solo retrasa un poco, no bloquea.
- `utteranceId` máximo 39 caracteres (el edge usa 17).
- El ESP32 aún **no tiene canal de entrada de comandos hacia los actuadores**: los actuadores van por su propio ESP32 y el
  edge de actuadores (por el broker), tal como definiste. Este nodo solo es micrófono + parlante.

## Mensajes del nodo para los avisos al familiar

El firmware puede enviar, en cualquier momento después del `ready` y por el mismo enlace:

- `{"type":"help_request"}` cuando la persona presiona el botón de ayuda. El edge avisa al familiar y dice por el
  parlante «Ya estoy avisando a su familia».
- `{"type":"telemetry","batteryPct":<0-100>}` cada cierto tiempo (por ejemplo cada minuto) si el nodo tiene batería.
  El edge avisa una sola vez cuando baja del umbral.
