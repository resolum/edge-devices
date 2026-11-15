# Decisiones de arquitectura (ADR)

## ADR-1: Contextos DDD access, voice, commands, shared
Estado: aceptada. Cada contexto tiene capas domain, application, infrastructure, interfaces e integration. Los contextos se
hablan solo por capas anticorrupción (`voice/integration`, `commands/integration`), nunca por modelos ajenos.

## ADR-2: El backend es la fuente de verdad del acceso
Estado: aceptada. El edge no tiene lista local de dispositivos. Sin tabla se niega todo. Un dispositivo es válido si está en
la tabla y su estado es IN_USE.

## ADR-3: Identidad por MAC
Estado: aceptada. El `hello` lleva la MAC. El `deviceId` del sketch es solo etiqueta de log. Riesgo: la MAC se puede
falsificar en la LAN; se mitiga con el token local de emparejamiento.

## ADR-4: Sincronización por snapshot con ETag y long poll
Estado: aceptada. 304 sin cambios, 409 fuerza refetch sin ETag, 404 borra la tabla, 401 conserva la última y alarma,
429/503 con espera creciente respetando Retry-After. El acuse de versión es best effort.

## ADR-5: Catálogo dinámico desde la tabla
Estado: aceptada. Los actuadores autorizados generan el catálogo, el esquema y el prompt del LLM. La escena "todo" cierra
puertas y ventanas y apaga luces; las cortinas no entran.

## ADR-6: Doble autorización
Estado: aceptada. Se autoriza al conectar el micrófono y se vuelve a autorizar cada acción al ejecutarla.

## ADR-7: Persistencia con Peewee y SQLite
Estado: aceptada. Guarda la última tabla (operación offline) y el historial de resultados (ok, fail, timeout, denied). El
backend no tiene endpoint de telemetría, por eso el historial es local.

## ADR-8: WiFi y Bluetooth sin conmutación automática en firmware
Estado: aceptada con limitación. El edge escucha ambos y prioriza WiFi, pero el firmware elige transporte al compilar
(`USE_BLUETOOTH`). La conmutación en caliente requiere trabajo de firmware aparte.

## ADR-9: Modelos con fuente oficial, hash fijado y licencia visible
Estado: aceptada. Cada modelo se descarga de su fuente oficial (Vosk de Alpha Cephei, Silero de la etiqueta v6.2.3, Piper
de rhasspy/piper-voices). `models.lock.json` guarda el SHA-256 (cotejado para Silero, primer uso para el resto) y el edge
se niega a arrancar si un modelo no coincide. Las licencias no comerciales o copyleft (Qwen2.5-3B, piper-tts) se avisan en
el arranque. Detalle y cifras en `docs/modelos.md`.

## ADR-10: Medir antes de ajustar el LLM
Estado: aceptada. El intérprete se evalúa con casos escritos a mano y casas sintéticas (mismo prompt y validación que
producción). Solo se hace ajuste fino si el modelo base no cumple, y un modelo ajustado solo entra si gana en los casos a
mano sin aumentar las acciones de más. El conjunto sintético se parte por casas para evitar fuga entre entrenamiento y prueba.
