# Cómo funciona ringr-agents

# Nivel 0: en una frase

Un programa **hace llamadas telefónicas con un robot que habla**: llama a alguien, conversa siguiendo un guion con ayuda de una IA y, al colgar, **le cuenta a un servidor cómo fue la llamada** para que la guarde y la cobre.

---

# Nivel 1: las piezas y cómo se hablan

Hay cuatro actores:

```
┌──────────┐   "llama a X"   ┌───────────┐   HTTP   ┌─────────┐   SQL   ┌──────────┐
│  Dialer  │ ──────────────> │  Runtime  │ <──────> │   API   │ <─────> │ Postgres │
│ (fuera)  │                 │ (el robot)│          │ (Django)│         │          │
└──────────┘                 └─────┬─────┘          └─────────┘         └──────────┘
                                   │
                                   ▼
                     ┌──────────────────────────┐
                     │  Servicios externos      │
                     │  (teléfono, IA, voz)     │
                     │  → aquí son simulados    │
                     └──────────────────────────┘
```

| Actor | Qué es, en abstracto |
|---|---|
| **Dialer** | El que decide *cuándo* llamar. Mira las llamadas pendientes y, cuando llega su hora, se las pasa al runtime. No está en este repo |
| **Runtime** | El que *hace* la llamada. No sabe nada de la base de datos: todo se lo pregunta a la API |
| **API** | La que *sabe* las cosas: quién es el cliente, a quién se llama, qué guion seguir. También *apunta* cómo acabó cada llamada y cuánto se cobra |
| **Servicios externos** | Teléfono, LLM y voz sintética. Aquí son imitaciones que fallan a propósito para parecerse a la realidad |

### La vida de una llamada, en tres actos

1. **Preparar**: el runtime pregunta a la API "¿qué tengo que hacer con esta llamada?" y recibe a quién llamar, el guion, el historial y la configuración del cliente.
2. **Conversar**: el runtime marca y, mientras la persona habla, repite *escuchar → pensar (LLM) → hablar (TTS)*. La llamada acaba porque termina, porque la persona cuelga o porque se pasa de tiempo.
3. **Informar**: el runtime le dice a la API "esta llamada acabó así, duró tanto y esto es lo que se dijo". La API lo guarda, marca la llamada como terminada y suma minutos a la factura del cliente. Si la API no contesta, el runtime **vuelve a enviarlo**.

Con esto ya se entiende todo el sistema. Ahora entramos en cada pieza.

---

# Nivel 2: las tripas, pieza a pieza

Voy en el orden en que aparecen durante una llamada.

## 2.1 Los datos: qué sabe la API ([models.py](api/calls/models.py))

Antes de ver código conviene saber qué se guarda.

```
Tenant (cliente: acme, beta, gamma)
  │  minutes_used  ← lo que se factura
  │
  └── CampaignOwnership ── Campaign (una campaña de cobros)
                              ├── ScriptStep × N  (el guion, ordenado)
                              └── Call × N        (cada intento de llamada)
                                    ├── Contact   (a quién: nombre, teléfono)
                                    ├── status    pending | finished
                                    ├── scheduled_at  ← el dialer mira esto
                                    └── CallResult × N  (cómo acabó)
```

Tres detalles importantes:
- Campaign no apunta al Tenant directamente. Pasa por `CampaignOwnership`, y por eso en el código aparece `call.campaign.ownerships.get().tenant`.
- `CallResult` es una FK, no 1:1. **Nada impide que una llamada tenga varios resultados.**
- El seed ([seed.py](api/calls/management/commands/seed.py)) crea acme con 50 llamadas, beta con 250 y gamma con 700. Además, cada contacto de gamma tiene un historial de entre 15 y 20 llamadas antiguas.

## 2.2 Quién monta el runtime ([deps.py](agent_runtime/deps.py), [config.py](agent_runtime/config.py))

`config.py` lee [fakes.toml](fakes.toml): probabilidades de fallo, latencias y `time_scale = 0.05`. Ese último valor hace que el tiempo simulado vaya 20 veces más rápido, así que una llamada de 3 minutos dura unos 9 s reales.

`build_deps(config)` construye la "caja de herramientas" que recibe cada llamada:

```python
Deps(config, api=ApiClient, llm=FakeLLM, tts=fake_tts, transport_factory=FakeTransport)
```

La idea de diseño es que **nadie construye sus propias dependencias**. Cualquier pieza se puede sustituir con `build_deps(config, api=otra_cosa)`, y así lo hacen los tests.

## 2.3 El punto de entrada ([runner.py](agent_runtime/runner.py))

`run_call(call_id, deps)` es el director de orquesta. Hace los tres actos:

```python
# Acto 1: preparar
ctx = await deps.api.get_context(call_id)
cfg = await deps.api.get_tenant_config(ctx.tenant_id)
#   si falla → devuelve ERROR (y no informa a nadie)

# Acto 2: conversar, con un límite de tiempo
outcome = await asyncio.wait_for(session.run(), timeout=9 s)
#   TimeoutError → TIMEOUT
#   otra excepción → ERROR

# Acto 3: informar
await report_result(...)   # hasta 3 intentos
```

`report_result` aplica esta regla de reintentos, que es parte del contrato fijo:
- Si no hay respuesta (timeout o error de conexión) o llega un **5xx**, reintenta tras 0,2 s y luego 0,4 s.
- Si llega un **2xx o 4xx**, se para.

## 2.4 Cómo habla con la API ([api_client.py](agent_runtime/api_client.py))

Es un envoltorio de httpx con tres métodos:

| Método | Detalle |
|---|---|
| `get_context` | Convierte el JSON en un `CallContext`. El contacto queda como texto `"Nombre (+34…)"` y el historial como una lista de outcomes |
| `get_tenant_config` | Guarda la respuesta en una caché en memoria por tenant, sin caducidad |
| `post_result` | Usa un timeout **corto** (1 s). Las lecturas tienen 10 s |

## 2.5 Lo que responde la API ([views.py](api/calls/views.py))

**`GET /calls/{id}/context`** hace estas consultas:
1. Busca la Call.
2. Busca su ownership para sacar el tenant.
3. Busca las 20 últimas Calls `finished` **del mismo contacto**.
4. **Por cada una**, pide `results.first()`, lo que añade una consulta por llamada.
5. Lee el guion, el contacto y la campaña.

**`GET /tenants/{id}/config`** devuelve `{language, system_prompt}`.

**`POST /calls/{id}/result`**:
```python
payload = parse_result(body)                  # valida outcome y duration_s → si no, 400
CallResult.objects.create(...)                # 1. guarda el resultado
call.status = FINISHED; call.save()           # 2. marca la llamada
tenant.minutes_used = tenant.minutes_used + ceil(duración/60)
tenant.save()                                 # 3. factura
```
Son tres escrituras sueltas, sin transacción y sin comprobar si ya había un resultado.

## 2.6 La conversación ([session.py](agent_runtime/session.py))

`CallSession.run()` es el corazón del robot:

```
transport.open()              ← marca. Si nadie contesta, lanza NoAnswer
bucle:
   evento = transport.next_event()
     Hangup     → devuelve USER_HANGUP
     EndOfCall  → devuelve COMPLETED
     Utterance  → la persona ha dicho algo:
         1. apuntar "user: …" en el transcript
         2. pausa breve (por si sigue hablando)
         3. reply = _reply()          ← LLM, hasta 3 intentos
         4. apuntar "agent: …"
         5. audio = tts(reply); transport.play(audio)
         6. guardar los últimos 50 KB de audio (la "grabación")
finally: transport.close()
```

`_prompt()` monta lo que se envía al LLM: el system prompt del tenant, el idioma, el contacto, las llamadas previas, el guion numerado y la conversación hasta ahora, terminando en `agent:` para que el LLM continúe.

`_reply()` reintenta el LLM hasta 3 veces y captura `BaseException`. Ese detalle importa más adelante, porque `BaseException` también incluye la cancelación del timeout.

## 2.7 El mundo simulado ([fakes.py](agent_runtime/fakes.py), [middleware.py](api/calls/middleware.py))

Estas piezas no se pueden tocar, pero hay que entenderlas porque son las que provocan los problemas.

**FakeTransport (el teléfono).** Del `call_id` se deriva de forma determinista:
- cuántas frases dice la persona (entre 4 y 12) y cuáles;
- las pausas entre ellas;
- si cuelga a mitad (5 %);
- si no contesta nunca (10 %), en cuyo caso `open()` lanza `NoAnswer`.

**FakeLLM (la IA).**
- Responde token a token con una de tres frases.
- **Se cuelga** según el prompt. Como el reintento usa el mismo prompt, se vuelve a colgar. Un cuelgue es un `await` sobre un Future que nunca se resuelve.
- Falla con `LLMError` al azar (2 %).

**SimulatedNetworkMiddleware (la red), en el lado de la API.**
- Añade 20 ms de latencia a cada petición.
- En un 3 % de los POST, **la vista se ejecuta y guarda los cambios**, pero la respuesta tarda más de lo que el cliente espera. El runtime cree que ha fallado y reintenta.

**gunicorn** ([Dockerfile](Dockerfile)) usa 4 workers × 8 hilos, así que dos peticiones iguales pueden procesarse **a la vez**.

## 2.8 Los tests ([tests/](tests/))

- [conftest.py](tests/conftest.py) apaga la latencia y las respuestas perdidas.
- [test_api.py](tests/test_api.py) usa el cliente de Django contra una base de datos real.
- [test_runtime.py](tests/test_runtime.py) ejecuta `run_call` completo con una API falsa hecha con `httpx.MockTransport`.

---

# Nivel 3: un caso concreto atravesando todas las capas

**Llamada `abc-123` de gamma a "gamma contact 7".**

| # | Capa | Qué pasa |
|---|---|---|
| 1 | Dialer | Ve `abc-123` pendiente y con `scheduled_at` ya pasado, y llama a `run_call("abc-123", deps)` |
| 2 | runner → api_client → views | `GET /context`: unas 25 consultas (una por cada llamada del historial). Devuelve el tenant 3, el contacto, los 3 pasos del guion y 20 outcomes previos |
| 3 | runner → api_client | `GET /tenants/3/config`. Si otra llamada de gamma ya lo pidió, sale de la caché sin red |
| 4 | session → FakeTransport | `open()`: contesta |
| 5 | session | *"Hello, who is this?"* → pausa → LLM → *"I am calling about the pending balance…"* → TTS → play |
| 6 | session | Se repite el ciclo con *"I already paid that last month."* y unas cuantas frases más |
| 7 | session | `EndOfCall` da `COMPLETED`, y `close()` |
| 8 | runner | Duración: 3,1 s reales / 0,05 = **62 s** |
| 9 | runner → views | `POST /result {completed, 62.0, transcript}`. Se crea el CallResult, la Call pasa a `finished` y gamma suma **+2 min** |
| 10 | runner → Dialer | Devuelve `COMPLETED` |

### Y los caminos torcidos del mismo caso

- **En el paso 9 la respuesta se pierde (3 %).** La API **ya guardó** todo, pero el runtime no se entera y reintenta. Resultado: dos CallResult y **+4 min**. Es el problema 2.
- **En el paso 4 nadie contesta (10 %).** `NoAnswer` llega a `run_call` como excepción genérica y se informa como `error`. Es el problema 3.
- **En el paso 5 el LLM se cuelga.** A los 9 s, `wait_for` cancela la sesión, pero `_reply` captura la cancelación como si fuera un fallo más y reintenta. El mismo prompt se vuelve a colgar y ya no hay una segunda cancelación. La llamada puede quedarse viva en memoria para siempre y no informar nunca. Encaja con "la memoria crece" y con "llamadas a medias". No está comprobado ejecutándolo: se deduce de leer el código.
- **En el paso 2 con gamma.** Cada contacto tiene historial, así que el contexto cuesta más consultas que en acme o beta. Es un candidato para el problema 1, pero habría que medirlo.
