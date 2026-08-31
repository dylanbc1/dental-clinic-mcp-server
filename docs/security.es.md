# Seguridad

> 🇬🇧 [Read it in English](./security.md)

## Por qué existe esta sección

Las auditorías públicas del ecosistema MCP en 2026 encontraron que, de más de
22.000 servers listados, el **40% no exige autenticación**, el **79% maneja
credenciales en texto plano** y solo el **8,5% implementa OAuth**. En enero de
2026 el server de referencia de Anthropic acumuló tres CVEs: path traversal,
borrado arbitrario de archivos y RCE. La vara está en el piso; superarla es un
acto deliberado.

El fallo concreto contra el que se diseña: en julio de 2025 un agente de IA borró
una base de datos de producción en SaaStr durante un code-freeze. Tenía permisos
de escritura que nunca necesitó y nadie podía revocárselos granularmente.

El detalle importante es que **el token del agente era válido**. La autenticación
no lo habría detenido. Los scopes por sí solos tampoco, si el scope estaba
concedido. Lo que faltaba era un humano entre la intención y el efecto. Por eso
existen las capas 2 y 3, y por eso son controles distintos y no uno solo.

## La frontera regulatoria

Agendar una cita **no** es acto médico. Registrar un *motivo de consulta* sí lo
es: es dato clínico, y arrastra la Resolución 2654/2019 (telesalud), el
consentimiento informado y el registro RNBD ante la SIC.

Esa frontera no es adorno. Es la razón por la que el catálogo se parte en
`read` / `write` / `clinical` en vez de `read` / `write`, y por la que
`registrar_motivo_consulta` exige consentimiento registrado *además* del scope y
la aprobación. La pregunta de un ente regulador no es «¿estaba autorizado el
llamador?» sino «¿consintió el paciente y quién tocó el dato?», así que el
sistema responde las dos.

## Las cinco capas

| # | Capa | Dónde |
|---|---|---|
| 1 | OAuth 2.1 + PKCE, cero API keys | `mcp_server/auth.py`, `mcp_server/oauth/` |
| 2 | Scopes por herramienta (`read`/`write`/`clinical`) | `mcp_server/auth.py` |
| 3 | Human-in-the-loop en toda mutación | `mcp_server/aprobacion.py` |
| 4 | Errores estructurados y accionables | `backend/domain/errores.py`, `mcp_server/errores.py` |
| 5 | Auditoría + guardas de transporte | `mcp_server/auditoria.py`, `mcp_server/limites.py`, `backend/models.py` |

---

### Capa 1, OAuth 2.1 + PKCE

El MCP server es un **resource server**. No emite nada: verifica tokens contra el
JWKS publicado por el Authorization Server. No hay API key en este proyecto, ni
archivo de configuración que pueda contener una, ni ruta de código que lea un
secreto compartido.

El handshake de descubrimiento es el estándar, y es lo que hace usable el server
para un cliente que nunca lo ha visto:

```
cliente ──POST /mcp────────────────────────────────▶ 401
        ◀──── WWW-Authenticate: Bearer resource_metadata="…"
        ──GET /.well-known/oauth-protected-resource─▶ { authorization_servers: [ … ] }
        ──GET /.well-known/oauth-authorization-server▶ { authorization_endpoint, token_endpoint, jwks_uri }
        ──GET /authorize?code_challenge=…&method=S256▶ 302 ?code=…
        ──POST /token  code + code_verifier ────────▶ { access_token }
```

El Authorization Server propio **impone** lo que OAuth 2.1 exige en vez de solo
anunciarlo, y cada punto tiene su prueba:

- PKCE es obligatorio y solo `S256`; `plain` se rechaza.
- Los grants implicit y password no existen.
- Los códigos son de un solo uso y expiran en 60 segundos; un código reusado
  quema el original.
- Una `redirect_uri` no registrada recibe `400`, nunca un redirect, redirigir a
  una URI provista por un atacante es un open redirect y una vía de exfiltración
  del código.
- Los scopes desconocidos se rechazan en vez de descartarse en silencio, así que
  un cliente que pide `admin` se entera de que no existe.

La verificación del token comprueba cuatro cosas, y las cuatro importan:

| Comprobación | Qué impide |
|---|---|
| Firma (RS256 contra JWKS) | Tokens falsificados |
| `exp` | Replay de un token viejo |
| `iss` | Un token bien firmado por otro Authorization Server |
| `aud` | **El confused deputy**: un token emitido para otro resource server, reusado aquí |

El verificador fija `RS256`, así que la sustitución clásica por `alg: none` nunca
tiene oportunidad. Una verificación fallida no devuelve motivo: distinguir
«expirado» de «firma inválida» ante un llamador no autenticado es reconocimiento
gratis para un atacante.

> **Intercambiable por construcción, y verificado.** `docker compose --profile
> keycloak up` levanta un realm real de Keycloak con los mismos tres scopes *y un
> segundo MCP server que confía en él*, lado a lado con el original. Misma
> imagen, mismo código; solo cambian `OAUTH_ISSUER` y `OAUTH_JWKS_URL`.
> `scripts/verificar_keycloak.py` obtiene un token de Keycloak, lo usa, y después
> muestra a cada servidor devolviendo `401` ante el token del otro.
>
> Construirlo dejó la lección que vale la pena conservar: **Keycloak no pone la
> audiencia del resource server en el token si no configuras un mapper.** Como
> este verificador *exige* `aud`, el cambio falló ruidosamente hasta que se
> agregó el mapper, que es el resultado correcto. Un resource server que acepta
> un token sin audiencia acepta cualquier token que ese IdP haya emitido, a quien
> sea, y eso es exactamente el confused deputy.

### Capa 2, scopes por herramienta

Tres scopes. Cada herramienta declara exactamente uno.

```
read      consultas, sin efectos secundarios
write     todo lo que muta la agenda o la cartera
clinical  la única herramienta que toca dato clínico
```

**Los scopes no anidan.** `write` no implica `read`; `clinical` no implica
`write`. Es la decisión de diseño que más parece un descuido y es deliberada:
«administrativo» y «clínico» son *tipos* distintos de autoridad, no cantidades
distintas de la misma. Un agente que agenda citas no tiene por qué leer un
síntoma, y uno que transcribe síntomas no tiene por qué cancelar una visita.
Least privilege es la autoridad más pequeña que hace el trabajo, no una escalera.

La matriz completa (13 tools × 3 scopes, 39 combinaciones) está enumerada en
`tests/security/test_scopes.py`. No muestreada: enumerada. Una suite de permisos
escrita por ejemplos siempre acaba con un hueco.

Dos sutilezas que las pruebas fijan:

- **La denegación ocurre antes de cualquier consulta.** Un rechazo no debe
  depender del dato ni filtrar si el registro existe.
- **`confirmar_operacion` no tiene scope propio.** Verifica el scope de la acción
  nombrada *dentro del token*. Una herramienta que exigiera `write` dejaría que
  un token `write` ejecutara una propuesta clínica en silencio, así que el scope
  se revisa en el momento del efecto, no en el de la intención. Un token emitido
  cuando el llamador tenía `clinical` no ejecuta si ese scope ya fue revocado.

### Una propuesta sobre la que nadie puede actuar

Antes de proponer, una tool de escritura comprueba lo que puede: que el cupo siga
libre y en el futuro, que la especialidad coincida, que el paciente no tenga otra
cita a esa hora y que la transición de estado sea legal. Todo con la misma
validación del backend que corre al agendar, así que ambas rechazan exactamente
por las mismas razones.

La alternativa es pedirle a alguien que apruebe una operación que va a fallar al
confirmar, lo que entrena a la gente a aprobar sin leer. Las comprobaciones se
repiten al ejecutar porque el estado puede cambiar en el medio, y esa segunda es
la que manda.

Los rechazos durante esa validación se auditan como todo lo demás. Un log que
solo registra propuestas exitosas no puede decirte que un agente pasó una hora
proponiendo algo imposible.

### Capa 3, human-in-the-loop

Toda herramienta de escritura y la clínica son de dos fases:

1. La herramienta **propone**: resumen en lenguaje llano, lista explícita de
   efectos, advertencias y un token de confirmación firmado. Nada ha cambiado.
2. Una persona lo lee y aprueba, lo que llama `confirmar_operacion` con ese
   token. Solo entonces corre la mutación.

El token es lo que hace seguro exponer la fase 2 como una herramienta que el
modelo puede llamar:

| Propiedad | Ataque que derrota |
|---|---|
| Firmado con HMAC-SHA256 | Falsificar una propuesta, o editar los argumentos de una ya aprobada |
| De un solo uso | Reusar una confirmación para cancelar dos veces la misma cita |
| Vigencia de 5 minutos | Que una aprobación de la mañana autorice una acción de la noche |
| Atado al sujeto | Canjear la aprobación de otra persona |
| Lleva el nombre de la acción | Canjear una aprobación de `cancelar_cita` para ejecutar `agendar_cita` |

El orden de validación importa y está probado: primero la firma, después la
expiración. Decirle a un atacante que su token falsificado «expiró» le confirma
que su payload se parseó. Un canje que falla por *sujeto equivocado* tampoco
gasta el nonce: eso sería una denegación de servicio contra quien sí puede
aprobar.

La aprobación autoriza una acción; no vuelve legal una ilegal. Una propuesta
confirmada para marcar como `atendida` una cita sin confirmar sigue fallando en
la máquina de estados.

### Capa 4, errores estructurados

Todo fallo modelado lleva código estable, mensaje y la parte que casi nadie
implementa: un **siguiente paso accionable**.

```json
{
  "error": true,
  "codigo": "SLOT_NO_DISPONIBLE",
  "mensaje": "El cupo del 2026-09-03 09:00 ya no está libre.",
  "sugerencia": "Los cupos libres más cercanos son: 2026-09-03 09:30 (Dra. Ospina), 2026-09-03 11:00 (Dr. Cadena).",
  "detalles": { "slot_id": 88, "alternativas": [{ "slot_id": 91 }, { "slot_id": 96 }] }
}
```

No es cosmético. Un modelo que recibe `500 Internal Server Error` reintenta a
ciegas y quema tokens; uno que recibe el payload de arriba se recupera en su
propio turno. Verificado por pruebas:

- Todo error de dominio mapea a un `4xx`. Un fallo modelado le toca resolverlo al
  llamador; mapearlo a `5xx` afirmaría que el servidor se rompió.
- No hay dos clases de error con el mismo código, los códigos son parte del
  contrato de las herramientas.
- **Una sola envoltura, en todas partes.** El cuerpo 422 propio de FastAPI
  (`{"detail": [...]}`) es una segunda forma de error; se remapea, porque dos
  formas obligan al llamador a ramificar según cuál le llegó.
- Una excepción inesperada, es decir un bug real, se registra completa y se responde con
  un único error estructurado opaco. El stack trace nunca llega al modelo.
- Los fallos de permiso están redactados para cortar el bucle de reintento: *«No
  vuelvas a llamar esta herramienta con el token actual: el resultado será el
  mismo.»*

### Capa 5, auditoría y guardas de transporte

**Dos registros separados**, y confundirlos es un error común:

- **Los cambios de estado** viven en `cita_historial`, append-only (no tiene
  columna `actualizada_en`, por diseño) y escrito en la misma transacción que el
  cambio. Un hueco de auditoría no puede ocurrir. Es el registro que pediría un
  ente regulador.
- **Las invocaciones de herramientas** viven en el log JSON estructurado: quién
  llamó qué, con qué scope, si hubo aprobación y si tuvo éxito, *incluidas las
  llamadas rechazadas.* Un log que solo registra éxitos no puede decirte que un
  agente pasó una hora fallando contra un scope que no tiene.

El acceso clínico tiene su propio tipo de evento (`clinico.acceso`), porque la
Res. 2654 pregunta quién tocó el dato clínico y enterrarlo en el flujo genérico
lo vuelve incontestable en una auditoría.

**Nada sensible se duplica en el log.** El motivo de consulta, los teléfonos, los
documentos, los nombres y los tokens de confirmación se redactan. Un log de
auditoría no es excusa para copiar datos del paciente a un segundo lugar peor
protegido, y un token de aprobación registrado es una aprobación reusable.

**Guardas de transporte:**

- *Validación de Host y Origin* (anti DNS-rebinding). Sin ella, una página que el
  usuario visita en su navegador puede alcanzar un servidor atado a `127.0.0.1` y
  manejarlo. Es el ataque que vuelve falso el consuelo de «solo escucha en
  localhost». La lista blanca es explícita, sin comodines, y los nombres sin
  puerto se expanden automáticamente, porque el header que envía un navegador es
  `localhost:8080`, no `localhost`. (Una lista blanca sin puerto rechaza en
  silencio todas las peticiones legítimas, que es como esta guarda suele terminar
  desactivada.)
- *Rate limiting* de ventana deslizante, con clave por sujeto autenticado y
  caída a dirección del cliente. Protege la base de la clínica de un agente
  atascado en un bucle de reintentos, el fallo ordinario y no el adversarial, así
  que el 429 lleva `Retry-After` y le dice al llamador que lea el último error en
  vez de repetir. Una ventana fija permitiría el doble de la tasa prevista en la
  costura entre ventanas.
- *Solo Streamable HTTP.* SSE está deprecado para producción y no se ofrece.

## Garantías que viven en la base de datos

Dos controles están en el esquema y no en el código de aplicación, porque una
validación en aplicación es una por la que un segundo proceso pasa de largo:

```sql
CREATE UNIQUE INDEX uq_cita_slot_activa ON cita (slot_id)
  WHERE estado IN ('agendada','confirmada','en_espera','atendida');
```

- **La doble reserva es imposible.** Dos agentes leen «cupo libre» antes de que
  alguno escriba; ningún `if` gana esa carrera.
  `tests/integration/test_concurrencia.py` corre dos conexiones vivas contra un
  cupo y verifica que sobreviva exactamente una. El bloqueo optimista sobre
  `agenda_slot.version_id` cubre las ediciones concurrentes del cupo mismo.
- **Los reintentos son idempotentes.** `cita.idempotency_key` es única, así que
  un agente que reintenta una reserva que expiró recibe un conflicto en vez de
  una segunda cita.

## Modelo de amenazas (STRIDE-lite)

| Amenaza | Vector | Control | Riesgo residual |
|---|---|---|---|
| **S**uplantación | Token falsificado o reusado | RS256 + JWKS, `iss`, `aud`, `exp`; `alg` fijado | Compromiso de la llave del AS. Se mitiga rotando `OAUTH_PRIVATE_KEY_PEM`; la llave efímera de desarrollo se invalida al reiniciar, por diseño |
| **S**uplantación | Canjear la aprobación de otro | Token atado al `sub`, firmado con HMAC | Compromiso de la clave de firma ⇒ rotar `APPROVAL_SIGNING_KEY`; los tokens viven 5 minutos |
| **T**ampering | Editar argumentos de una propuesta aprobada | HMAC sobre todo el payload, con versión | Ninguno conocido |
| **T**ampering | Doble reserva por carrera | Índice único parcial + bloqueo optimista | Ninguno a nivel de base de datos |
| **R**epudio | «Yo nunca cancelé esa cita» | `cita_historial` append-only, actor del token, misma transacción | El backend confía en el header `X-Actor`, aceptable porque no es alcanzable desde fuera de la red de compose; un despliegue público exige mTLS o un header firmado |
| **I**nformación | Dato clínico llegando a quien no debe | Scope `clinical` + consentimiento + nunca lo devuelven las tools de lectura | Quien legítimamente tiene `clinical` ve el dato, para eso es |
| **I**nformación | Fuga de datos del paciente por logs | Redacción de campos clínicos e identificadores | El propio pipeline de logs debe estar protegido |
| **I**nformación | Stack traces o fragmentos SQL en errores | Envoltura opaca única para fallos inesperados | Ninguno conocido |
| **D**enegación | Agente en bucle de reintentos | Rate limit deslizante; errores redactados para cortar bucles | Contador en proceso: con varias réplicas el límite efectivo se multiplica (ver abajo) |
| **D**enegación | Quemar un token de aprobación legítimo | El fallo por sujeto no gasta el nonce | Ninguno conocido |
| **E**levación | Token sobredimensionado (la forma SaaStr) | Scopes por herramienta que no anidan; scope revisado al ejecutar | Un operador aún puede conceder `clinical` a algo que no lo necesita, problema de política, no de código |
| **E**levación | Acceso desde el navegador a un servidor local | Validación de Host/Origin, lista blanca explícita | Ninguno conocido |
| **E**levación | Agente mutando datos unilateralmente | Aprobación en dos fases en toda escritura | Una persona que aprueba sin leer. Se mitiga redactando propuestas para leerse en voz alta, no con código |

## Límites conocidos, dichos sin rodeos

Son fronteras deliberadas de un proyecto de portafolio, no descuidos:

1. **El Authorization Server guarda estado en memoria** y auto-aprueba el paso de
   consentimiento. Demuestra que el protocolo se entiende; no es donde deben
   vivir tus identidades de producción. `--profile keycloak` es la respuesta para
   un despliegue real.
2. **El registro de nonces y el rate limiter están en proceso.** Correcto para
   una réplica; con más de una, ambos van a Redis. Las dos interfaces son lo
   bastante estrechas para intercambiarse sin tocar una herramienta.
3. **El backend confía en `X-Actor`.** No es alcanzable desde fuera de la red de
   compose y el MCP server es su único cliente. Un despliegue público necesita
   mTLS o un header firmado en ese salto.
4. **La llave de firma efímera es intencional en desarrollo.** Cada reinicio
   invalida todos los tokens vigentes, que es exactamente lo que debe pasarle a
   una llave que nadie eligió persistir.

## Manejo de secretos

- No existe ninguna credencial en este repositorio. `.env.example` documenta la
  forma, con valores que son placeholders locales evidentes.
- `alembic.ini` lleva un `sqlalchemy.url` vacío; el valor real se inyecta desde
  `Settings` en tiempo de ejecución, así que hay un solo lugar donde configurarlo
  y ningún archivo versionado que pueda filtrarlo.
- `mcp_auth_enabled` viene en **on** por defecto. Derivarlo del nombre del
  entorno significaría que un error de tipeo en `APP_ENV` desactiva la
  autenticación en silencio.
- Las direcciones de bind son loopback en el código; `0.0.0.0` es una decisión de
  despliegue tomada explícitamente en `docker-compose.yml`.
- El contenedor corre como usuario sin privilegios (uid 10001).
- CI rompe el build si aparece en el código un literal con forma de secreto o una
  llave privada PEM.

## Datos

Todos los datos son sintéticos, generados con Faker y semilla fija. No hay
información real de pacientes en este proyecto ni ruta de código que pueda
introducirla. `tests/integration/test_seed.py::TestSinPiiReal` lo verifica.
