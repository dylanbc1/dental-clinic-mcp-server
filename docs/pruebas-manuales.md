# Pruebas manuales

> 🇬🇧 [Read it in English](./manual-testing.md)

Trece pruebas para que evalúes el proyecto tú mismo. Cada una dice **qué correr**
y **qué deberías ver**. Si algo no coincide, esa capa está rota.

Tiempo total: unos 25 minutos. Necesitas Docker corriendo y `uv` instalado.

```bash
cd clinica-mcp-server
cp .env.example .env
make up
```

`make up` debe terminar en ~10 segundos con los cuatro servicios en `healthy`.

---

## Bloque A · Que arranque y sea reproducible (5 min)

### A1 · El quickstart no miente

```bash
make down && docker compose down -v      # borra todo
time make up
```

**Esperas:** cuatro contenedores `healthy` en menos de 20 segundos. `make up`
migra, siembra y levanta la API antes de devolver el control, así que si el
comando terminó, el sistema responde.

```bash
curl -s localhost:8000/listo
```
→ `{"estado":"listo"}`

### A2 · Los datos son sintéticos y suficientes

```bash
docker compose exec -T postgres psql -U clinica -d clinica -c "
select regimen, afiliacion_activa, count(*) from paciente group by 1,2 order by 1;
select estado, count(*) from cita group by 1 order by 2 desc;
select count(*) as cupos_libres from agenda_slot where estado='libre';"
```

**Esperas:** los cuatro regímenes representados, algunos con `afiliacion_activa =
f` (si no hubiera ninguno, `validar_afiliacion` no tendría nada que atrapar),
citas en los seis estados, y más de mil cupos libres para agendar.

### A3 · El seed es determinista

```bash
uv run python -m backend.seed --fecha-base 2026-08-31
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select md5(string_agg(documento||nombre, '' order by documento)) from paciente;"

uv run python -m backend.seed --fecha-base 2026-08-31
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select md5(string_agg(documento||nombre, '' order by documento)) from paciente;"
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
curl -si "localhost:9000/authorize?response_type=code&client_id=clinica-demo\
&redirect_uri=http://localhost:6274/oauth/callback&state=x" | grep -i location
```

**Esperas:** el redirect lleva `error=invalid_request` con un mensaje que dice que
falta `code_challenge`.

Y un `redirect_uri` no registrado (intento de exfiltrar el código):

```bash
curl -si "localhost:9000/authorize?response_type=code&client_id=clinica-demo\
&redirect_uri=https://atacante.test/robar&code_challenge=x&code_challenge_method=S256" \
  | head -1
```

**Esperas:** `HTTP/1.1 400`, **no** un redirect. Redirigir a una URI que el
atacante controla es un open redirect.

### B3 · Capa 2 · Un token `read` no puede escribir

```bash
TOKEN_READ=$(uv run python scripts/obtener_token.py --scope "read")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_READ" \
  --method tools/call --tool-name cancelar_cita \
  --tool-arg cita_id=1 --tool-arg motivo="prueba manual"
```

**Esperas:** `SCOPE_INSUFICIENTE`, con el scope que falta (`write`), los que sí
tienes (`['read']`) y la frase *"No vuelvas a llamar esta herramienta con el token
actual"*. Ese último detalle es lo que evita que un agente entre en bucle.

### B4 · Capa 2 · Los scopes no anidan

```bash
TOKEN_RW=$(uv run python scripts/obtener_token.py --scope "read write")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name registrar_motivo_consulta \
  --tool-arg cita_id=1 --tool-arg motivo="dolor de muela"
```

**Esperas:** rechazado. Tener `write` no da acceso clínico. Esta es la decisión de
diseño que más parece un descuido y no lo es: agendar y diagnosticar son tipos
distintos de autoridad, no cantidades distintas de la misma.

### B5 · Capa 3 · Una escritura no escribe

```bash
# Toma un cupo libre
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name consultar_disponibilidad --tool-arg limite=1

# Agenda (cambia SLOT_ID y PACIENTE_ID por los que veas)
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name agendar_cita \
  --tool-arg paciente_id=1 --tool-arg slot_id=SLOT_ID
```

**Esperas:** `"requiere_confirmacion": true`, una lista `esto_va_a_pasar`, y un
`token_confirmacion`. **Nada se agendó.** Vuelve a pedir disponibilidad: el cupo
sigue libre.

Verifícalo en la base:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select estado from agenda_slot where id = SLOT_ID;"
```
→ `libre`

### B6 · Capa 3 · El token de confirmación aguanta manipulación

```bash
# 1. Ejecuta de verdad
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name confirmar_operacion \
  --tool-arg token_confirmacion="EL_TOKEN"
```
→ `"ejecutada": true`. Ahora la cita existe.

```bash
# 2. Reúsalo
```
→ `APROBACION_YA_USADA`

```bash
# 3. Cámbiale un carácter al token y reintenta
```
→ `APROBACION_INVALIDA`

```bash
# 4. Espera 5 minutos con una propuesta nueva sin confirmar y confírmala
```
→ `APROBACION_EXPIRADA`

**Por qué importa el orden:** prueba el 3 con un token también vencido. Debe decir
`APROBACION_INVALIDA`, no `APROBACION_EXPIRADA`. Decirle a un atacante que su
token falsificado "expiró" le confirma que su payload se parseó.

### B7 · Capa 4 · Los errores te dicen qué hacer

Intenta agendar en el cupo que acabas de ocupar:

```bash
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name agendar_cita \
  --tool-arg paciente_id=2 --tool-arg slot_id=EL_MISMO_SLOT
```

**Esperas:** `SLOT_NO_DISPONIBLE` con los tres cupos libres más cercanos, con hora
y profesional. Compara con lo que devuelve el 92% del ecosistema: `500`.

Y prueba que un bug real no filtra nada:

```bash
curl -s localhost:8000/citas/999999 | python3 -m json.tool
```

**Esperas:** un JSON con `codigo`, `mensaje` y `sugerencia`. Sin `Traceback`, sin
SQL, sin nombres de clases internas.

### B8 · Capa 5 · La auditoría registra, sin copiar datos

```bash
docker compose logs mcp | grep tool.invocacion | tail -5 | python3 -m json.tool 2>/dev/null \
  || docker compose logs mcp | grep tool.invocacion | tail -5
```

**Esperas:** una línea JSON por llamada, **incluidas las rechazadas**, con
`sujeto`, `scope_requerido`, `resultado` y `con_aprobacion_humana`. Y fíjate en
que `documento` y `motivo` aparecen como `«redactado»`: el log registra que la
llamada ocurrió, no el dato del paciente.

Y el historial de la cita en la base:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -c \
  "select estado_anterior, estado_nuevo, usuario, momento from cita_historial
   order by id desc limit 5;"
```

**Esperas:** el `usuario` es el sujeto del token (`recepcion@clinica.local`), no
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

Busca un paciente en mora y agéndale una cita:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select paciente_id, sum(monto)::int from cargo
   where estado='pendiente' and vencimiento < current_date
   group by 1 order by 2 desc limit 1;"
```

Agenda con ese `paciente_id`. **Esperas:** la propuesta sale con una advertencia
`"...en mora... No impide agendar"` y un `token_confirmacion` válido. La cita se
agenda. Las clínicas no niegan atención por un copago sin pagar.

### C2 · La afiliación vencida cambia la tarifa, no el acceso

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select id from paciente where afiliacion_activa=false and regimen<>'particular' limit 1;"
```

Llama `validar_afiliacion` con ese id. **Esperas:** `regimen_efectivo:
"particular"`, `bloquea_agendamiento: false`, y una sugerencia sobre reactivar
ante la EPS.

### C3 · La máquina de estados no admite atajos

Sobre una cita en estado `agendada`, propón y confirma `registrar_asistencia` con
`estado=atendida` (saltándose `confirmada` y `en_espera`).

**Esperas:** `TRANSICION_INVALIDA`, listando las transiciones que sí serían
válidas. Nota que **la aprobación humana no vuelve legal una operación ilegal**:
aprobaste, y aun así el dominio la rechaza.

### C4 · Doble reserva imposible

```bash
uv run pytest tests/integration/test_concurrencia.py -v 2>&1 | tail -20
```

**Esperas:** 15 pruebas verdes. La clave es
`test_dos_agentes_sobre_el_mismo_cupo_solo_uno_gana`: dos conexiones reales a
Postgres compitiendo, una gana y la otra recibe un conflicto limpio. Una
validación en aplicación pierde esa carrera siempre.

---

## Bloque D · Lo automático (3 min)

### D1 · La suite completa

```bash
make test-fast
```

**Esperas:** 777 pruebas verdes, cobertura ≥95% (hoy 99%).

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

## Cómo evaluar lo que viste

Si quieres ser duro con el proyecto, estas son las preguntas que yo haría:

| Pregunta | Dónde mirar |
|---|---|
| ¿La seguridad es real o son comentarios? | B1 a B9. Todas fallan de forma verificable. |
| ¿El dominio es auténtico o inventado? | C1 y C2: la mora que no bloquea y la afiliación que solo cambia la tarifa son reglas del sector, no del programador. |
| ¿Las pruebas prueban algo? | `tests/security/test_scopes.py` enumera las 39 combinaciones, no muestrea. `test_concurrencia.py` usa dos conexiones reales. |
| ¿Sabe dónde están los límites? | `docs/security.es.md`, sección "Límites conocidos": AS en memoria, rate limiter en proceso, `X-Actor` sin firmar. Están dichos, no escondidos. |
| ¿Funciona fuera de la máquina del autor? | `make up` desde cero, y el job `e2e` de CI hace exactamente lo mismo. |

Para limpiar todo:

```bash
make down && docker compose down -v
```
