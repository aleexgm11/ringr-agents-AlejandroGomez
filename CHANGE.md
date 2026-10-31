# Notas de la prueba

## Problema 2: facturación duplicada

### Diagnóstico

`POST /calls/{id}/result` hacía tres escrituras sueltas (crear el resultado, marcar la llamada como
`finished` y sumar los minutos), sin transacción ni comprobar si ya había resultado. Cuando el runtime
reintentaba un POST cuya respuesta se había perdido, se duplicaba todo.

| | Escenario | Efecto |
| --- | --- | --- |
| A | El mismo POST dos veces | Dos resultados, minutos dobles |
| B | Dos POST idénticos a la vez | Igual que A. Comprobar antes de insertar no basta |
| C | Fallo entre las escrituras | Resultado sin facturar, o al revés |
| D | Dos resultados del mismo tenant a la vez | Minutos perdidos: la suma se hacía en Python y una escritura pisaba a la otra |

### Cambios

| Mecanismo | Resuelve |
| --- | --- |
| Si ya hay resultado: 200 si es idéntico, 409 si es distinto | A |
| `select_for_update()` sobre la `Call` | B |
| `transaction.atomic()` | C |
| `update(minutes_used=F('minutes_used') + x)` | D |
| `UniqueConstraint` en `CallResult.call` (migración `0002`) | Red de seguridad en el esquema |

Hay un test por escenario en `tests/test_api.py`.

### Por qué 409 ante un resultado distinto

Es un 4xx, así que el runtime no reintenta. Conserva el primer resultado sin reescribir minutos ya
facturados y deja visible el bug que lo provocó. Un reintento legítimo siempre es idéntico.

### Alternativas descartadas

- **Solo la restricción única**: los reintentos acabarían en un 500 y no arregla D.
- **`Idempotency-Key`**: el `call_id` ya es la clave natural, y obligaría a cambiar el contrato.
- **Bloquear el `Tenant`**: serializaría todas las llamadas del cliente más grande.

### Pendiente

- Limpiar los duplicados existentes antes de aplicar la migración `0002` en producción.
- Recalcular `minutes_used` con los resultados únicos.
- Las llamadas sin contestar facturan 1 minuto (`ceil` de una duración casi nula). Hay que revisarlo con
  el problema 3.