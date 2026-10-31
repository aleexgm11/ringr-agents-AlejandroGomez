# Notas de la prueba

## Problema 1: las llamadas de las campañas grandes tardan en arrancar

### Diagnóstico

Antes de cada llamada, el runtime pide `GET /calls/{id}/context`. La vista hacía una consulta por cada
llamada del historial del contacto (`previous.results.first()` dentro de un bucle, un N+1), más dos
cargas perezosas de `campaign` y `contact`.

| | acme / beta (sin historial) | gamma (15–20 llamadas por contacto) |
| --- | --- | --- |
| Consultas por `/context` | 6 | 25 |
| Una petición | ~25 ms | ~37 ms |
| 100 peticiones, 50 en paralelo | 0,24 s | 0,55 s |

- **Solo afecta al cliente grande** porque el coste depende del historial del contacto, no de la campaña.
- **Empeora según avanza la campaña**: cada llamada terminada se suma al historial (hasta el tope de 20).
- **La concurrencia lo amplifica**: el trabajo extra es Python (construir SQL, crear objetos) y compite
  por el GIL entre los hilos de cada worker. En ráfaga, la diferencia pasa de 1,5× a 2,3×.
- No es un problema de índices: cada consulta tarda ~0 ms. El coste está en el número de consultas.

### Cambios

| Cambio | Consultas |
| --- | --- |
| `select_related('contact')` al cargar la `Call` | Call + contacto en 1 |
| Ownership y guion por `campaign_id`, sin cargar la campaña | 1 + 1 |
| Historial con un `Subquery` del primer resultado y `.values()` | 1, sea cual sea el historial |
| **Total** | **4 siempre** (antes 6 + N) |

El `Subquery` mantiene la semántica del contrato: las 20 últimas llamadas terminadas, omitiendo las que
no tienen resultado. Descarté partir de `CallResult` con `select_related('call')`, porque devolvería los
20 últimos *resultados*, y `prefetch_related('results')`, porque `.first()` ignora el prefetch y vuelve a
consultar.

Tests: el número de consultas es el mismo con 0 y con 20 llamadas de historial
(`django_assert_num_queries`), y el historial respeta el orden y el tope de 20.

### Resultado

| | acme | beta | gamma |
| --- | --- | --- | --- |
| Consultas por `/context` | 4 | 4 | 4 |
| Una petición | ~25 ms | ~25 ms | ~25 ms |
| 100 peticiones, 50 en paralelo | | 0,21 s | 0,16 s |

### Para la parte 4

- `calls_callresult` tiene dos índices sobre `call_id` (el de la FK y el de `one_result_per_call`). El
  primero sobra y encarece cada inserción.
- gunicorn con workers síncronos (4 × 8 hilos) pone un techo a la concurrencia, y cualquier trabajo de
  Python por petición compite por el GIL.

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