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

## Problema 3: reintentar las llamadas que nadie contesta

### Qué había

`FakeTransport.open()` lanza `NoAnswer`, pero `CallSession.run()` no lo capturaba. La excepción llegaba
al `except Exception` de `run_call` y se informaba como `error`, mezclada con los fallos reales del LLM
o de la red. Además, como `ceil(0,001 / 60) = 1`, cada llamada sin contestar facturaba un minuto.

### Diseño

**Las reglas viven en `Tenant`.** El enunciado habla de "sus contratos", y en este modelo el contrato
es del tenant: sus campañas y su facturación cuelgan de él. Un diccionario en código obligaría a hacer
un deploy por cada cliente nuevo, y un modelo `RetryPolicy` aparte solo compensaría con varias políticas
por tenant, que nadie ha pedido.

| Campo | Tipo | Por qué |
| --- | --- | --- |
| `max_attempts` | `PositiveSmallIntegerField(default=1)` | Intentos totales contando el primero, la misma definición que la tabla del contrato. El valor por defecto es seguro: un tenant nuevo no reintenta hasta que se configure |
| `retry_delay` | `DurationField(default=0)` | Se suma directamente a un `datetime` y en `psql` se lee como `00:30:00`. Un entero dejaría la unidad implícita en el nombre |
| `calling_opens` / `calling_closes` | `TimeField(null=True)` | Una hora del día sin fecha. `NULL` significa "sin restricción" (gamma). Un falso `00:00–23:59:59` no puede expresar "todo el día" con el cierre exclusivo |

Un `CheckConstraint` exige que las dos horas vayan juntas y que la apertura sea anterior al cierre. La
segunda rama repite `isnull=False` a propósito: en SQL, `'09:00' < NULL` da `NULL`, y un CHECK que da
`NULL` se considera cumplido, así que la versión corta aceptaría una apertura sin cierre. Las franjas
que cruzan la medianoche quedan excluidas: ningún contrato las necesita y complicarían el cálculo.

La zona horaria es la constante `Europe/Madrid`, porque todos los contratos usan la hora de Madrid. Si
llega un cliente de otro país, se añade un campo con ese valor por defecto sin romper nada.

**Los intentos se guardan en `Call`.**

- `attempt` (1 para la original, 2 para el primer reintento…). Contar las llamadas de la campaña y el
  contacto no sirve, porque gamma ya tiene 15–20 por contacto en la misma campaña, y recorrer el historial
  en cada POST repetiría el problema 1. Es una desnormalización segura: el número se fija al crear la
  llamada y no cambia.
- `retry_of`, un `OneToOneField` a la llamada que originó el reintento. Da trazabilidad (se puede seguir
  la cadena de intentos) y, por ser único, **la base de datos impide dos reintentos de la misma llamada**.
  Varios `NULL` sí se permiten en un índice único de Postgres, así que las llamadas originales no chocan.
  `on_delete=SET_NULL`, porque perder el enlace es menos grave que borrar en cascada un reintento pendiente.

**El reintento se programa en `call_result`, dentro de la misma transacción.**

```python
CallResult.objects.create(...)       # resultado
call.status = FINISHED               # llamada terminada
Tenant...update(F() + billed_minutes(payload))   # facturación (0 si es no_answer)
if outcome == NO_ANSWER:
    schedule_retry(call, tenant, timezone.now())
```

Al ir detrás del control de duplicados del problema 2, el requisito de no programar dos reintentos se
cumple sin lógica nueva: un POST repetido devuelve 200 antes de llegar aquí, dos simultáneos se esperan
por el `select_for_update`, y el `OneToOne` de `retry_of` lo garantiza en el esquema. Al ir en la
transacción, si algo falla no queda un `no_answer` sin su reintento: la API devuelve un 500, el runtime
reintenta y todo empieza de cero. Descarté un proceso periódico que busque `no_answer` sin reintento (otra
pieza que operar, más retraso y otra fuente de carreras) y las señales `post_save` (esconden la lógica y es
fácil que salgan de la transacción).

La lógica del contrato vive en `calls/retries.py`, separada de la vista:

- `schedule_retry` decide si queda otro intento y crea la `Call` pendiente.
- `next_call_time` calcula la hora de salida. Es una función pura, sin base de datos y sin leer el reloj:
  si la espera termina dentro de `[apertura, cierre)` sale en ese momento; antes de abrir, sale ese día a
  la apertura; a partir del cierre, al día siguiente a la apertura. `zoneinfo` resuelve los cambios de hora.

La espera cuenta desde que se informa el `no_answer`, no desde el `scheduled_at` de la llamada: si el
dialer va con retraso, contar desde `scheduled_at` haría que el reintento saliera pegado al intento fallido.
`NoAnswer` salta al marcar, así que el informe llega segundos después del intento.

**Las llamadas `no_answer` no se facturan.** La regla vive en la API (`billed_minutes`), que es la dueña
de la facturación, y no en el runtime enviando `duration_s = 0`. Se guarda la duración real aunque se
facturen 0 minutos: el resultado registra lo que pasó y la facturación es una política sobre ello.

**Runtime.** `CallSession.run()` captura `NoAnswer` y devuelve `CallOutcome.NO_ANSWER`, junto a la
traducción que ya hacía de `Hangup` y `EndOfCall`. Va fuera del `try/finally`, porque una línea que nunca
se abrió no hay que cerrarla.

### Llamadas "a medias": el `except BaseException` de `_reply`

El enunciado menciona contactos con los que la llamada se quedó a medias. La causa estaba en
`CallSession._reply`, que capturaba `BaseException`, y eso incluye `asyncio.CancelledError`:

1. El `FakeLLM` se cuelga a mitad de una respuesta. Depende del prompt, así que es determinista.
2. El `wait_for` de `run_call` cancela la sesión al llegar a `call_timeout_s`.
3. `_reply` trataba la cancelación como un fallo del LLM y reintentaba con el mismo prompt, que se volvía
   a colgar. No llegaba una segunda cancelación, así que la llamada no terminaba nunca.

Consecuencias: la conversación quedaba a medias, no se informaba ningún resultado (la `Call` seguía
`pending`) y la sesión se quedaba en memoria con su transcript y su grabación. Encaja con el crecimiento de
memoria del runtime que describe el contexto, y además impedía que esas llamadas dispararan un reintento.

El arreglo es capturar solo `LLMError`, la excepción que documenta el proveedor. La cancelación se
propaga, el `finally` cierra la línea y la llamada se informa como `timeout`.

### Tests

| Fichero | Qué cubre |
| --- | --- |
| `tests/test_retries.py` | `next_call_time` sin base de datos: dentro de horario, el cierre excluido, antes de abrir, pasada la medianoche, el cambio de hora y sin horario |
| `tests/test_api.py` | `no_answer` no factura; sin contrato no se reintenta; el reintento sale tras la espera con `attempt` y `retry_of` correctos; fuera de horario espera a la apertura; el último intento no se reintenta; un `no_answer` repetido programa un único reintento; una llamada contestada no se reintenta |
| `tests/test_runtime.py` | Un `NoAnswer` se informa como `no_answer`; un LLM colgado termina como `timeout` y se informa; un LLM que falla termina como `error` |

El test del LLM colgado espera con `asyncio.wait(..., timeout=5)` en vez de hacer `await` directamente:
con el bug, la llamada no termina, y un `await` (o un `wait_for`, cuya cancelación también se tragaba)
dejaría el test colgado en lugar de fallar. La API falsa (`httpx.MockTransport`) hace los tests del runtime
deterministas e independientes de Docker. La parte de la API se prueba aparte, contra Postgres.

### Supuestos y pendiente

- El horario no distingue días de la semana ni festivos, porque el enunciado no lo menciona.
- La primera llamada no la limita el horario: eso es cosa del dialer. Solo se ajustan los reintentos.
- `max_attempts = 0` está permitido y se comporta como 1. Convendría un `CheckConstraint` de `>= 1`.
- El índice único de `retry_of` se construye recorriendo `calls_call`. En una tabla grande de producción
  habría que crearlo con `AddIndexConcurrently` para no bloquear las escrituras.
- Las migraciones `0003` y `0004` (esta última solo renombra un campo) se podrían fusionar en una, porque
  ninguna está en producción.
- Falta un test de integración del flujo completo (`run_call` contra la API de Django vía
  `httpx.ASGITransport`) que compruebe que un `no_answer` acaba en un reintento en la base de datos.


# Comentarios de posibles cambios

## Acerca del diseño de datos

La funcionalidad de reintento de llamadas esconde una posible mejora. Con el diseño actual los clientes se clasifican por tipos cerrados con ciertas caracteristicas cada uno, como el historial de llamadas previas. Al ser relacional, si se quiere añadir una nueva caracteristica se tiene que cambiar la tabla ampliando con los nuevos campos y determinando quien tiene cierta caracteristica o no. Otro caso puede ser si una nueva empresa tiene una promocion con una funcionalidad especifica, no se podria implementar sin cambiar la base de datos. Por tanto, esta parte de Tenants, campañas, etc. (negocio) Podria esta en una base No-SQL que destaca por versatilidad y meterse en un modelo hibrido con ambos. 

`Punto fuerte en contra`: la consistencia y la complejidad de las consultas cruzadas, si se hace en tiempo real el cruce entre ambas bases de datos crea mucho lag en las respuestas. Puede ser buena solucion implementar una capa intermedia en memoria como Redis donde se guarde en cache durante las campañas. Aumenta el coste computacional pero ofrece muy buena escalabilidad a diferentes necedisades 

*SQL* -> Tenant (informacion basica como id, estado cuenta...), Calls, CallResult

*NoSQL* -> Campaings, Tenant (caracteristicas del plan variable)