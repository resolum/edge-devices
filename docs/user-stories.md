# Historias de usuario

```gherkin
Feature: Acceso de micrófonos según el backend
  Scenario: Micrófono autorizado
    Given la tabla del backend incluye un MICROPHONE en estado IN_USE con la MAC del ESP32
    When el ESP32 envía hello con su MAC y el token correcto
    Then el edge responde ready

  Scenario: Sin tabla
    Given el edge aún no recibió ninguna tabla
    When un ESP32 envía hello
    Then el edge lo rechaza con "sin tabla"

  Scenario: Dispositivo en mantenimiento
    Given el backend marca el micrófono como MAINTENANCE
    Then el edge cierra la sesión con "acceso revocado" y deniega reconexiones

Feature: Órdenes por voz
  Scenario: Orden confirmada
    Given un micrófono autorizado y una puerta IN_USE en la entrada
    When el usuario dice "Alivia, cierra la puerta de la entrada" y responde "sí" a la confirmación
    Then el edge publica el comando MQTT y anuncia el resultado

  Scenario: Actuador que perdió acceso antes de ejecutar
    Given el usuario confirmó una acción
    And el backend retiró el actuador
    Then el edge no publica el comando y dice que no tiene permiso

  Scenario: Backend caído
    Given el edge tiene una tabla guardada
    When no hay conexión con el backend
    Then sigue operando con la última tabla
```
