# Contrato MQTT v2: edge de voz y edge de actuadores

Es el contrato que el edge de actuadores debe cumplir. El edge de voz lo implementa en
`commands/infrastructure/bus.py` y `scripts/actuator_sim.py` es un actuador de prueba.

## Topics

| Dirección | Topic | QoS | Retain |
|---|---|---|---|
| voz a actuador | `alivia/actuators/<deviceId>/command` | 1 | no |
| actuador a voz | `alivia/actuators/<deviceId>/result` | 1 | no |
| voz a todos | `alivia/voice/status` (`{"online": true/false}`, con LWT) | 1 | sí |

`<deviceId>` es el `deviceId` numérico que el backend asigna al dispositivo (por ejemplo `10`), no un nombre del catálogo.

## Comando (voz a actuador)

Un mensaje por acción:

```json
{
  "v": 2,
  "correlationId": "9f1c0e7a2b6d4c3e8a5b1d2f3c4e5a6b",
  "orderId": "0b1a2c3d4e5f60718293a4b5c6d7e8f9",
  "deviceId": 10,
  "mac": "AA:BB:CC:00:00:10",
  "deviceType": "DOOR",
  "roomKey": "entrada",
  "action": "close",
  "params": {},
  "source": "AA:BB:CC:00:00:01",
  "ts": 1731257421000,
  "ttlMs": 15000
}
```

- `deviceType`: DOOR, WINDOW, CURTAIN (`open`/`close`) o LIGHT (`on`/`off`).
- `source` es la MAC del micrófono que originó la orden.
- Si `ahora - ts > ttlMs` el actuador debe ignorar el comando.
- El edge de voz ya verificó contra la tabla del backend que el dispositivo está `IN_USE`. El actuador puede validar la MAC como defensa adicional.

## Resultado (actuador a voz)

```json
{ "v": 2, "correlationId": "9f1c0e7a2b6d4c3e8a5b1d2f3c4e5a6b", "ok": true, "detail": "", "ts": 1731257421800 }
```

- `ok` es `true` solo cuando la acción quedó verificada (fin de carrera, relé confirmado).
- Sin resultado en `[dialog].exec_timeout_s` el usuario escucha que no hubo confirmación.

## Reglas

1. Suscribirse a `alivia/actuators/+/command` con QoS 1.
2. Ser idempotente (QoS 1 puede duplicar).
3. Responder siempre, también con `ok:false`.
