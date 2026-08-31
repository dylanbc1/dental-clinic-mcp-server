# Manejar el servidor con el MCP Inspector

> 🇬🇧 [Read it in English](./inspector.md)

El Inspector es la forma más rápida de comprobar que las capas de seguridad son
reales y no solo están descritas. Todo lo de abajo es el recorrido manual de lo
que `scripts/smoke.py` automatiza.

```bash
make up            # postgres + backend + authorization server + mcp
make inspector     # abre el Inspector, ya con un token válido
```

`make inspector` ejecuta primero el flujo completo de OAuth 2.1 + PKCE y le pasa
el bearer token resultante al Inspector. `make inspector-cli` hace lo mismo sin
navegador.

## Lista de comprobación

Cada paso está pensado para *fallar* de forma instructiva. Si alguno tiene éxito
donde no debería, la capa correspondiente está rota.

### 1 · El servidor rechaza a un cliente anónimo (capa 1)

Apunta el Inspector a `http://localhost:8080/mcp` sin cabecera `Authorization`.
Obtienes `401`, y el header `WWW-Authenticate` nombra
`/.well-known/oauth-protected-resource`. Abre esa URL: te dice qué Authorization
Server usar. Esa cadena es lo que permite a un cliente que nunca ha visto este
servidor autenticarse por su cuenta.

### 2 · Un token `read` no puede escribir (capa 2)

```bash
TOKEN=$(uv run python scripts/obtener_token.py --scope "read")
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN" \
  --method tools/call --tool-name cancelar_cita \
  --tool-arg cita_id=1 --tool-arg motivo="prueba de scope"
```

El rechazo nombra el scope que falta, lista los que sí tienes y le dice al modelo
que no reintente con el mismo token.

### 3 · Un token `write` no toca dato clínico (capa 2)

La misma llamada contra `registrar_motivo_consulta` con `--scope "read write"`.
También se rechaza: los scopes no anidan.

### 4 · Una tool de escritura no cambia nada por sí sola (capa 3)

Con `--scope "read write"`, llama `consultar_disponibilidad`, toma un `slot_id` y
llama `agendar_cita`.

El Inspector te muestra un **prompt de elicitación** en vez de un resultado: el
servidor respondió `input_required` describiendo qué pasaría. No ha cambiado
nada. Vuelve a llamar `consultar_disponibilidad`: el cupo sigue libre.

### 5 · Solo tu respuesta ejecuta (capa 3)

Responde el prompt con `confirmado: true`. El Inspector reenvía la misma llamada
con tu respuesta y el `requestState` sellado, y ahora la cita existe.

Responde `false` sobre un prompt nuevo y obtienes `OPERACION_NO_APROBADA` sin
tocar nada. Declina el prompt directamente y la llamada aborta igual.

Lo interesante es lo que el Inspector nunca te muestra: la confirmación **no es
un parámetro de la tool**. Mira el esquema en el listado. No hay campo para
ella, que es justamente por lo que un modelo no puede aprobar en tu nombre.

### 6 · Los errores te dicen qué hacer (capa 4)

Llama `agendar_cita` sobre el cupo que acabas de tomar. El error nombra los tres
cupos libres más cercanos, con horas y profesionales. Esa es la diferencia entre
un agente que se recupera en su propio turno y uno que entra en bucle.

### 7 · Todo queda registrado (capa 5)

```bash
docker compose logs mcp | grep tool.invocacion | tail -5
```

Una línea JSON por llamada, incluidas las rechazadas, cada una con el sujeto, el
scope requerido y si hubo aprobación humana. Fíjate en que el número de documento
y el motivo de consulta aparecen como `«redactado»`: el log de auditoría registra
que la llamada ocurrió, no el dato del paciente.

### 8 · El acceso clínico exige consentimiento, no solo permiso

Con `--scope "read write clinical"`, propón `registrar_motivo_consulta` sobre una
cita de un paciente sin consentimiento registrado, y confírmala. Se rechaza:
todas las puertas abiertas salvo la autorización del propio paciente, que es la
que debe seguir deteniéndolo.

## Conectar Claude Desktop o Cursor

Cualquier cliente MCP sobre la spec 2026-07-28 con Streamable HTTP y OAuth puede
conectarse a
`http://localhost:8080/mcp`; el cliente realiza por su cuenta el descubrimiento y
el flujo PKCE. Para un cliente sin soporte de OAuth, levanta el stack con
`MCP_AUTH_ENABLED=false`, pero ten en cuenta que eso desactiva por completo las
capas 1 y 2, algo razonable solo en una máquina que controlas.
