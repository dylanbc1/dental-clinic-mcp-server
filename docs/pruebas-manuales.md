# Pruebas manuales

> 🇬🇧 [Read it in English](./manual-testing.md)

Trece pruebas para que evalúes el proyecto tú mismo. Cada una dice **qué correr**
y **qué deberías ver**. Si algo no coincide, esa capa está rota.

Tiempo total: unos 25 minutos. Necesitas Docker corriendo y `uv` instalado.

```bash
git clone https://github.com/dylanbc1/dental-clinic-mcp-server
cd dental-clinic-mcp-server
cp .env.example .env
make up
```

`make up` debe terminar en ~15 segundos con los cuatro servicios en `healthy`.

> **Con qué cliente probar.** El MCP Inspector todavía no habla la spec
> 2026-07-28, así que sirve para las lecturas y el catálogo pero **no puede
> responder las confirmaciones**. Para las escrituras usa `make consola`, un
> cliente interactivo donde tú respondes. Los bloques B5 y B6 usan `curl` para
> que veas el protocolo crudo.
La primera vez tarda más porque construye las imágenes.

---

## Bloque A · Que arranque y sea reproducible (5 min)

### A1 · El quickstart no miente

```bash
make down && docker compose down -v      # borra todo
time make up
```

**Esperas:** cuatro contenedores `healthy` en menos de 30 segundos. `make up`
migra, siembra y levanta la API antes de devolver el control, así que si el
comando terminó, el sistema responde.

```bash
curl -s localhost:8000/ready
```
→ `{"status":"ready"}`

### A2 · Los datos son sintéticos y suficientes

```bash
docker compose exec -T postgres psql -U clinic -d clinic -c "
select regimen, affiliation_active, count(*) from patient group by 1,2 order by 1;
select status, count(*) from appointment group by 1 order by 2 desc;
select count(*) as free_slots from agenda_slot where status='free';"
```

**Esperas:** los cuatro regímenes representados, algunos con `afiliacion_activa =
f` (si no hubiera ninguno, `validate_affiliation` no tendría nada que atrapar),
citas en los seis estados, y más de mil cupos libres para agendar.

### A3 · El seed es determinista

```bash
uv run python -m backend.seed --base-date 2026-08-31
docker compose exec -T postgres psql -U clinic -d clinic -t -c \
  "select md5(string_agg(document_number||name, '' order by document_number)) from patient;"

uv run python -m backend.seed --base-date 2026-08-31
docker compose exec -T postgres psql -U clinic -d clinic -t -c \
  "select md5(string_agg(document_number||name, '' order by document_number)) from patient;"
```

**Esperas:** el mismo hash las dos veces. Cambia `--seed 999` y debe cambiar.

---

## Bloque B · Las cinco capas de seguridad (12 min)

Aquí es donde deberías ser exigente. Cada prueba está diseñada para **fallar** de
forma instructiva.

### B1 · Capa 1 · Sin token no hay servidor

```bash
curl -si -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -6
```

**Esperas:** `HTTP/1.1 401` y una cabecera `WWW-Authenticate: Bearer ...
resource_metadata="..."`. Sin esa cabecera un cliente que cumple la spec no tiene
forma de averiguar dónde autenticarse.

Sigue la cadena de descubrimiento:

```bash
curl -s localhost:8080/.well-known/oauth-protected-resource | python3 -m json.tool
curl -s localhost:9000/.well-known/oauth-authorization-server | python3 -m json.tool
```

**Esperas:** el primero apunta al segundo. El segundo declara
`"grant_types_supported": ["authorization_code"]` y
`"code_challenge_methods_supported": ["S256"]`. **No** debe aparecer `implicit`,
`password` ni `plain`: OAuth 2.1 los elimina.

### B2 · Capa 1 · PKCE es obligatorio de verdad

Un `/authorize` sin PKCE:

```bash
curl -si "localhost:9000/authorize?response_type=code&client_id=clinic-demo\
&redirect_uri=http://localhost:6274/oauth/callback&state=x" | grep -i location
```

**Esperas:** el redirect lleva `error=invalid_request` con un mensaje que dice que
falta `code_challenge`.

Y un `redirect_uri` no registrado (intento de exfiltrar el código):

```bash
curl -si "localhost:9000/authorize?response_type=code&client_id=clinic-demo\
&redirect_uri=https://atacante.test/robar&code_challenge=x&code_challenge_method=S256" \
  | head -1
```

**Esperas:** `HTTP/1.1 400`, **no** un redirect. Redirigir a una URI que el
atacante controla es un open redirect.

### B3 · Capa 2 · Un token `read` no puede escribir

```bash
TOKEN_READ=$(uv run python scripts/get_token.py --scope "read")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_READ" \
  --method tools/call --tool-name cancel_appointment \
  --tool-arg appointment_id=1 --tool-arg reason="prueba manual"
```

**Esperas:** `INSUFFICIENT_SCOPE`, con el scope que falta (`write`), los que sí
tienes (`['read']`) y la frase *"No vuelvas a llamar esta herramienta con el token
actual"*. Ese último detalle es lo que evita que un agente entre en bucle.

### B4 · Capa 2 · Los scopes no anidan

```bash
TOKEN_RW=$(uv run python scripts/get_token.py --scope "read write")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name record_visit_reason \
  --tool-arg appointment_id=1 --tool-arg reason="dolor de muela"
```

**Esperas:** rechazado. Tener `write` no da acceso clínico. Esta es la decisión de
diseño que más parece un descuido y no lo es: agendar y diagnosticar son tipos
distintos de autoridad, no cantidades distintas de la misma.

### B5 · Capa 3 · La primera llamada no escribe

Las tools de escritura se detienen a preguntarle a una persona. El Inspector
renderiza esa pregunta y reenvía tu respuesta solo; para verlo crudo, `curl`:

```bash
TOKEN_RW=$(uv run python scripts/get_token.py --scope "read write")

# Sin sesión que recuerde el handshake, cada llamada lleva su propia versión de
# protocolo y sus capacidades. Es el costo visible del transporte sin estado.
META='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{"elicitation":{}}}'

curl -s -X POST localhost:8080/mcp \
  -H "Authorization: Bearer $TOKEN_RW" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'mcp-method: tools/call' -H 'mcp-name: book_appointment' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"book_appointment\",\"arguments\":{\"patient_id\":PACIENTE_ID,\"slot_id\":SLOT_ID},$META}}"
```

**Esperas:** `"resultType": "input_required"`, un `inputRequests` con la pregunta
en lenguaje llano (nombra la hora y el profesional, no el `slot_id`) y un
`requestState` de unos 470 bytes que empieza por `v1.`. **No se agendó nada.**

```bash
docker compose exec -T postgres psql -U clinic -d clinic -t -c \
  "select status from agenda_slot where id = SLOT_ID;"
```
→ `libre`

Mira el `requestState`: **no puedes leer nada en él.** Está cifrado, no solo
firmado, así que no revela la operación ni el paciente a quien lo tenga.

### B6 · Capa 3 · La segunda llamada ejecuta, y solo esa

Reenvía **la misma llamada** añadiendo la respuesta de la persona y el estado.
`CLAVE` es la única clave del objeto `inputRequests` que te devolvieron:

```bash
  ...,\"params\":{\"name\":\"book_appointment\",\"arguments\":{ ...los mismos... },
     \"inputResponses\":{\"CLAVE\":{\"action\":\"accept\",\"content\":{\"confirmed\":true}}},
     \"requestState\":\"v1....\",$META}
```

**Esperas:** `"resultType": "complete"` y la cita creada. Ahora ataca el estado:

| Qué haces | Qué debe pasar |
|---|---|
| Cambias un carácter del `requestState` | Rechazado |
| Usas el estado de `confirm_appointment` para ejecutar `cancel_appointment` | Rechazado: está atado a la petición |
| Cambias `patient_id` en la segunda ronda | Rechazado: los argumentos son parte de lo aprobado |
| Pides el estado con un sujeto y lo canjeas con otro | Rechazado: está atado al principal |
| Respondes `"confirmed": false` | `OPERACION_NO_APROBADA`, sin tocar nada |

**Lo más importante:** pide la confirmación, cancela la cita desde otro lado
(`psql` o el Inspector), y **después** responde que sí. Debe rechazarse. El
resolver vuelve a correr en la segunda ronda, así que **la aprobación no congela
el mundo que vio**.

### B7 · Capa 4 · Los errores te dicen qué hacer

Intenta agendar en el cupo que acabas de ocupar:

```bash
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name book_appointment \
  --tool-arg patient_id=2 --tool-arg slot_id=EL_MISMO_SLOT
```

**Esperas:** `SLOT_UNAVAILABLE` con los tres cupos libres más cercanos, con hora
y profesional. Compara con lo que devuelve el 92% del ecosistema: `500`.

Y nota **cuándo** falla: al proponer, no al confirmar. Pedirle a una persona que
apruebe una operación que no puede funcionar es peor que un error. Lo mismo pasa
si el paciente ya tiene otra cita a esa hora, o si la especialidad del cupo no es
la que pediste. La comprobación se repite al ejecutar, porque el estado puede
cambiar entre una cosa y la otra.

Y prueba que un bug real no filtra nada:

```bash
uv run python scripts/call_api.py /appointments/999999
```

**Esperas:** un JSON con `code`, `message` y `suggestion`. Sin `Traceback`, sin
SQL, sin nombres de clases internas.

### B8 · Capa 5 · La auditoría registra, sin copiar datos

```bash
docker compose logs mcp | grep tool.invocation | tail -5 | python3 -m json.tool 2>/dev/null \
  || docker compose logs mcp | grep tool.invocation | tail -5
```

**Esperas:** una línea JSON por llamada, **incluidas las rechazadas**, con
`subject`, `required_scope`, `result` y `with_human_approval`. Y fíjate en
que `document_number` y `reason` aparecen como `«redacted»`: el log registra que la
llamada ocurrió, no el dato del paciente.

Compruébalo a propósito:

```bash
docker compose logs mcp | grep -c "dolor severo"   # el motivo que enviaste en B3
docker compose logs mcp | grep -c "redacted"
```

El primero debe dar `0` y el segundo, más de `0`.

Y el historial de la cita en la base:

```bash
docker compose exec -T postgres psql -U clinic -d clinic -c \
  "select previous_status, new_status, changed_by, occurred_at from appointment_history
   order by id desc limit 5;"
```

**Esperas:** el `user` es el sujeto del token (`recepcion@clinic.local`), no
`system` ni `mcp-server`. Una auditoría con el mismo usuario en cada fila no es
una auditoría.

### B9 · Capa 5 · DNS rebinding

```bash
curl -si -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Origin: http://sitio-malicioso.test' \
  -H "Authorization: Bearer $TOKEN_RW" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -1
```

**Esperas:** rechazado. Sin esta guarda, una página que visitas en tu navegador
puede alcanzar un servidor atado a `127.0.0.1` y manejarlo. Es el ataque que
vuelve falso el consuelo de "solo escucha en localhost".

---

## Bloque C · El dominio es real (5 min)

### C1 · La mora avisa, no bloquea

El seed incluye cartera arrastrada de antes de la ventana de agenda, porque los
cargos vencen a 30 días y la agenda solo llega dos semanas atrás: sin eso todos
los pacientes saldrían `al_dia` y esta regla no tendría con qué demostrarse.

Busca un paciente en mora y agéndale una cita:

```bash
docker compose exec -T postgres psql -U clinic -d clinic -t -c \
  "select patient_id, sum(amount)::int from charge
   where status='pending' and due_date < current_date
   group by 1 order by 2 desc limit 1;"
```

Agenda con ese `patient_id`. **Esperas:** la propuesta sale con una advertencia
`"...en mora... No impide agendar"` y aun así pide confirmación. La cita se
agenda una vez aprobada. Las clínicas no niegan atención por un copago sin pagar.

### C2 · La afiliación vencida cambia la tarifa, no el acceso

```bash
docker compose exec -T postgres psql -U clinic -d clinic -t -c \
  "select id from patient where affiliation_active=false and regimen<>'particular' limit 1;"
```

Llama `validate_affiliation` con ese id. **Esperas:** `effective_regimen:
"particular"`, `blocks_booking: false`, y una sugerencia sobre reactivar
ante la EPS.

### C3 · La máquina de estados no admite atajos

Sobre una cita en estado `scheduled`, propón y confirma `record_attendance` con
`status=attended` (saltándose `confirmed` y `waiting`).

**Esperas:** `INVALID_TRANSITION` **antes de preguntarte nada**, listando las transiciones que
sí serían válidas. Consulta la cita primero con `get_appointment`: el campo
`valid_transitions` te dice exactamente qué puede pasar después, que es la
misma información que el modelo usa para elegir la siguiente herramienta.

La comprobación se repite en la segunda ronda, y ahí es donde importa de verdad:
si alguien cancela la cita entre la pregunta y tu respuesta, el dominio la
rechaza igual. **La aprobación humana no vuelve legal una operación ilegal.**

### C4 · Doble reserva imposible

```bash
uv run pytest tests/integration/test_concurrency.py -v 2>&1 | tail -20
```

**Esperas:** 15 pruebas verdes. La clave es
`test_two_agents_on_the_same_slot_only_one_wins`: dos conexiones reales a
Postgres compitiendo, una gana y la otra recibe un conflicto limpio. Una
validación en aplicación pierde esa carrera siempre.

---

## Bloque D · Lo automático (3 min)

### D1 · La suite completa

```bash
make test-fast
```

**Esperas:** más de 800 pruebas verdes, cobertura ≥95% (hoy 99%).

### D2 · El recorrido completo del cliente

```bash
make smoke
```

**Esperas:** los nueve pasos en verde, desde el 401 hasta la escritura clínica
denegada por scope.

### D3 · El cambio de Authorization Server

```bash
make keycloak          # tarda ~1 min la primera vez
make keycloak-verify
```

**Esperas:** un token emitido por Keycloak funciona contra el servidor de :8081, y
**cada servidor devuelve 401 ante el token del otro**. Ese rechazo es la
validación de audiencia funcionando.

---

## Bloque E · Los supuestos que las de arriba no tocan (7 min)

Cinco sondas apuntadas a lo que las otras trece dan por hecho. Tres encontraron
bugs reales; abajo están los arreglos y sus pruebas de regresión. Se corren con
`make probe`, que reporta qué esperaba cada una, qué pasó y un veredicto.

### E1 · Un token vencido, no un scope equivocado

Todo lo anterior prueba scope insuficiente. Nada probaba un token
estructuralmente válido que simplemente caducó.

```bash
OAUTH_ACCESS_TOKEN_TTL_SECONDS=3 docker compose up -d oauth --wait
TOKEN=$(uv run python scripts/get_token.py --scope read)
sleep 6
curl -si -X POST localhost:8080/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H 'mcp-method: tools/list' \
  -H 'accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -1
docker compose up -d oauth --wait   # de vuelta a los 900s normales
```

**Esperas:** `401` con `WWW-Authenticate`. Ni un `500`, y mucho menos un
resultado. **Resultado:** pasa. Un resource server que acepta tokens vencidos no
está validando `exp`, y este sí.

### E2 · Reproducir una confirmación que ya funcionó

Alterar el `requestState` está cubierto. Reusar uno intacto después de que
funcionó, no.

**Esperas:** rechazo, o idempotencia. Nunca un segundo efecto. **Resultado: un
bug real.** El estado sellado no lleva marca de "gastado" en el servidor, a
propósito: el diseño es sin estado. Reproducir un `book_appointment` aprobado
volvió a ejecutar la tool y solo lo frenó el índice único.
`offer_slot_to_waiting_list` no está protegida ni por índice ni por la máquina de
estados, y reproducirla **ofreció el mismo cupo liberado a un segundo paciente**:
dos personas citadas para un solo cupo.

**Arreglado** haciendo la operación idempotente en vez de añadir estado: un cupo
ya prometido devuelve esa oferta vigente. `tests/integration/
test_retry_and_races.py::TestOfferingAFreedSlotTwice`.

### E3 · Idempotencia bajo un reintento real

El README promete que un agente que reintenta recibe la misma cita.

**Esperas:** el mismo `appointment_id` dos veces, una fila en la base.
**Resultado: un bug real.** Cierto en el backend, falso por el único camino que
tiene un agente. El resolver del MCP validaba el cupo antes de proponer, y en el
reintento ese cupo lo tenía el primer intento, así que la llamada moría con
`SLOT_UNAVAILABLE` y nunca llegaba a la rama idempotente.

**Arreglado:** la clave se consulta primero, y un reintento de una operación ya
completada no le pide aprobación a nadie, porque no va a pasar nada nuevo.
`tests/contract/test_write_tools.py::test_a_retry_with_the_same_key_asks_nobody
_and_books_nothing_new`.

### E4 · Diez reservas concurrentes por HTTP, no dos conexiones en psql

C4 hace competir dos conexiones de base de datos. Esta hace competir el camino
completo: OAuth, la ida y vuelta de confirmación, y la escritura.

**Esperas:** exactamente un éxito, el resto rechazado limpio, ningún `500`, una
cita viva. **Resultado: un bug real.** Un éxito, dos rechazos limpios y **seis
`500`**. El `version_id` optimista de `agenda_slot` levanta `StaleDataError`,
una clase distinta del `IntegrityError` que el código atrapaba, así que escapaba
como error no manejado. Toda la promesa sobre fallos accionables quedaba anulada
justo en el caso para el que existe.

**Arreglado** en tres sitios, más una red en la capa de API para que ninguna ruta
futura pueda responder a una carrera con un `500`. Quien pierde recibe ahora lo
mismo que quien llega un segundo tarde, alternativas incluidas: un hecho, una
forma, sin importar el tiempo. `tests/integration/test_retry_and_races.py::
TestALostRace`.

### E5 · Autorización horizontal, entre tenants

**No hay modelo de tenant, y eso es una decisión de alcance, no un defecto.**
`clinic` tiene una sola fila, y solo `professional` lleva `clinic_id`; pacientes,
cupos y citas no están acotados a una clínica. Cualquier token válido lee
cualquier paciente, y una sonda con un sujeto de otra clínica hace exactamente
eso.

Es honesto para una demostración de una sola clínica y es **incorrecto para un
despliegue multi-tenant**, donde sería un hueco de autorización horizontal.
Cerrarlo significa un tenant en cada fila, un claim de tenant en el token, y un
filtro que ninguna consulta pueda olvidar. Queda anotado aquí como límite
conocido para que nadie tenga que descubrirlo, que es la diferencia entre una
frontera documentada y un hueco silencioso.

## Cómo evaluar lo que viste

Si quieres ser duro con el proyecto, estas son las preguntas que yo haría:

| Pregunta | Dónde mirar |
|---|---|
| ¿La seguridad es real o son comentarios? | B1 a B9. Todas fallan de forma verificable. |
| ¿El dominio es auténtico o inventado? | C1 y C2: la mora que no bloquea y la afiliación que solo cambia la tarifa son reglas del sector, no del programador. |
| ¿Las pruebas prueban algo? | `tests/security/test_scopes.py` enumera las 39 combinaciones, no muestrea. `tests/integration/test_concurrency.py` usa dos conexiones reales. |
| ¿Sabe dónde están los límites? | `docs/security.es.md`, sección "Límites conocidos": AS en memoria, rate limiter en proceso, `X-Actor` sin firmar. Están dichos, no escondidos. |
| ¿Funciona fuera de la máquina del autor? | `make up` desde cero, y el job `e2e` de CI hace exactamente lo mismo. |

Para limpiar todo:

```bash
make down && docker compose down -v
```
