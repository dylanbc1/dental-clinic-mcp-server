# dental-clinic-mcp-server

> Un MCP server de grado producción para una clínica odontológica: citas,
> validación de afiliación y cartera, con los controles de seguridad que el 92%
> del ecosistema MCP no tiene.
>
> 🇬🇧 [Read it in English](./README.md)

<p align="left">
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="MCP SDK v2" src="https://img.shields.io/badge/MCP%20SDK-v2-7c5cff">
  <img alt="OAuth 2.1 + PKCE" src="https://img.shields.io/badge/OAuth-2.1%20%2B%20PKCE-1f8b4c">
  <img alt="cobertura 99%" src="https://img.shields.io/badge/cobertura-99%25-1f8b4c">
  <img alt="813 pruebas" src="https://img.shields.io/badge/pruebas-813-1f8b4c">
</p>

```bash
cp .env.example .env && make up && make smoke
```

---

## Por qué existe

Hay más de 22.000 MCP servers publicados. Las auditorías muestran que el **40%
no tiene autenticación**, el **79% maneja credenciales en texto plano** y solo
el **8,5% implementa OAuth**. En enero de 2026 incluso el server de referencia
de Anthropic acumuló tres CVEs: path traversal, borrado de archivos y RCE.

El dominio no es el diferenciador; la ingeniería sí. Este repositorio demuestra
cómo se ve ese 8,5%.

El riesgo que ataca es concreto. En julio de 2025 un agente de IA borró una base
de datos de producción en SaaStr durante un code-freeze: tenía permisos de
escritura que nunca necesitó y nadie podía revocárselos granularmente. Su token
era válido, así que los scopes por sí solos no lo habrían detenido. Lo que
faltaba era un humano entre la intención y el efecto. Ambas cosas están
implementadas aquí.

## Dominio: por qué una clínica odontológica en Colombia

Nada en este server es inventado. La máquina de estados de la cita, la
validación de afiliación contra los regímenes colombianos, las reglas de copago
y la lista de espera son el proceso real que corre una clínica o IPS todos los
días. Alrededor del **25% de las citas agendadas no se usan cada mes**, y por eso
existen la confirmación a 48 horas y la liberación de cupos: las herramientas
que los implementan atacan un dolor cuantificado, no un guion de demo.

Hay además una frontera regulatoria que vuelve necesaria la seguridad:
**agendar una cita no es acto médico, pero registrar el motivo de consulta sí
toca dato clínico.** Ahí aplican la Resolución 2654/2019, el consentimiento
informado y el registro RNBD ante la SIC. En esa frontera viven exactamente el
scope `clinical` y la aprobación humana obligatoria.

> **Todos los datos son sintéticos**, generados con Faker y semilla fija. En
> ningún punto del proyecto hay información real de pacientes, y hay una prueba
> que lo verifica.

## Arquitectura

```mermaid
flowchart LR
    C["Cliente MCP<br/>Claude · Cursor · Inspector"]
    subgraph mcp["MCP server · Streamable HTTP"]
      direction TB
      A1["1· OAuth 2.1 + PKCE"] --> A2["2· Verificación de scope"] --> A3["3· Aprobación humana"] --> T["14 tools · 3 resources · 1 prompt"]
      T --> A4["4· Errores estructurados"]
      T --> A5["5· Auditoría + guardas de transporte"]
    end
    subgraph be["Backend de dominio · FastAPI"]
      API["API REST"] --> DOM["máquina de estados · cartera<br/>afiliación · lista de espera"] --> DB[("PostgreSQL 16")]
    end
    C --> A1
    T --> API
    style mcp fill:#f6f2ff,stroke:#7c5cff
    style be fill:#f0f7ff,stroke:#3b82f6
```

| Capa | Stack | Responsabilidad |
|---|---|---|
| Backend de dominio | FastAPI + PostgreSQL 16 + SQLAlchemy 2.x | Fuente de verdad. No sabe nada de MCP. |
| MCP server | MCP Python SDK v2, Streamable HTTP | Traduce el dominio a tools/resources/prompts. **Aquí viven todos los controles de seguridad.** |
| Authorization Server | OAuth 2.1 propio (o Keycloak) | Emite tokens. Intercambiable sin tocar el resource server. |

Separar el backend del MCP server es en sí el punto: en producción el MCP server
casi nunca *es* el sistema, envuelve uno que ya existe. El LLM nunca toca la base
de datos directamente.

Razonamiento completo y diagramas: [`docs/architecture.es.md`](./docs/architecture.es.md).

## El catálogo de herramientas

Catorce, no treinta. La precisión del modelo cae pasadas 25–30 tools, así que un
catálogo pequeño y descrito con precisión es el diseño, no una limitación.

| Scope | Herramientas |
|---|---|
| `read` | `buscar_paciente` · `consultar_disponibilidad` · `consultar_cita` · `listar_citas_paciente` · `consultar_cartera` · `validar_afiliacion` |
| `write` | `agendar_cita` · `confirmar_cita` · `cancelar_cita` · `reprogramar_cita` · `registrar_asistencia` · `ofrecer_cupo_lista_espera` |
| `clinical` | `registrar_motivo_consulta` |
|, | `confirmar_operacion` (ejecuta una propuesta aprobada) |

Resources: `clinica://info`, `politicas://cartera`, `agenda://hoy`.
Prompt: `recepcionista_odontologia`.

**Toda herramienta de escritura y la clínica devuelven una propuesta, no un
resultado.** Llamar `cancelar_cita` no cambia nada: devuelve un resumen en
lenguaje llano de lo que *pasaría* más un token firmado. Solo
`confirmar_operacion`, con ese token, muta algo:

```jsonc
{
  "requiere_confirmacion": true,
  "resumen": "Cancelar la cita 412 de Ana Gómez del 2026-09-03 09:00.",
  "esto_va_a_pasar": [
    "La cita pasará de 'confirmada' a 'cancelada'.",
    "El cupo quedará libre en la agenda.",
    "Si hay lista de espera para esa especialidad, se informará al siguiente."
  ],
  "advertencias": ["El paciente registra $180.000 COP en mora. No impide agendar."],
  "token_confirmacion": "eyJhY2Npb24iOi…",
  "vigencia_segundos": 300,
  "siguiente_paso": "Muestra este resumen a la persona responsable. …"
}
```

## Seguridad

Cinco capas, cada una respondiendo a una falla documentada del ecosistema.
Detalle completo y modelo de amenazas: [`docs/security.es.md`](./docs/security.es.md).

| # | Capa | Qué impide |
|---|---|---|
| 1 | **OAuth 2.1 + PKCE**, cero API keys | Acceso anónimo; un código de autorización robado |
| 2 | **Scopes por herramienta** `read`/`write`/`clinical`, sin anidar | El confused deputy; la forma SaaStr de tokens sobredimensionados |
| 3 | **Human-in-the-loop**: propuestas firmadas, de un solo uso y con expiración | Que un agente mute datos por su cuenta |
| 4 | **Errores estructurados** con siguiente paso accionable | Bucles de reintento a ciegas; stack traces filtrados |
| 5 | **Auditoría + guardas de transporte** | Cambios sin autor; DNS rebinding; agentes desbocados |

Tres endurecimientos adicionales a la especificación, porque los agentes
concurrentes los encuentran en la primera hora:

- **La doble reserva es imposible a nivel de base de datos**, con índice único
  parcial sobre el cupo más bloqueo optimista. Una validación en aplicación
  siempre pierde esa carrera.
- **Claves de idempotencia al agendar**, así un agente que reintenta recibe la
  misma cita y no un duplicado.
- **Guardar en UTC, presentar en America/Bogota**. Los datetime naive se
  rechazan en vez de adivinarse.

Los scopes deliberadamente **no anidan**. Un token `write` no puede leer el
motivo de consulta y un token `clinical` no puede cancelar una cita, porque
«administrativo» y «clínico» son *tipos* distintos de autoridad, no cantidades
distintas de la misma.

## Arranque rápido

```bash
cp .env.example .env      # solo placeholders locales; aquí no hay secretos reales
make up                   # postgres + backend + authorization server + mcp
make smoke                # recorre todo el camino del cliente e imprime cada paso
```

`make up` tarda unos diez segundos desde frío y deja:

| | |
|---|---|
| MCP server | `http://localhost:8080/mcp` |
| Docs de la API de dominio | `http://localhost:8000/docs` |
| Authorization Server | `http://localhost:9000/.well-known/oauth-authorization-server` |

### Conectar el MCP Inspector

```bash
make inspector            # abre el Inspector, ya autenticado
make inspector-cli        # o lista el catálogo sin navegador
```

`make token` imprime un access token obtenido con el flujo PKCE real, para curl
o para pegarlo en cualquier cliente. Prueba a emitir un token solo-`read` y
llamar `cancelar_cita`: el rechazo explica exactamente qué hacer después.

### Cambiar el Authorization Server por Keycloak

```bash
make keycloak             # Keycloak en :9100 + un segundo MCP server en :8081
make keycloak-verify      # demuestra que el cambio funciona y que los tokens no se cruzan
```

`--profile keycloak` levanta un **segundo MCP server** (misma imagen, mismo
código) que confía en un realm real de Keycloak en vez del Authorization Server
propio, y lo corre lado a lado con el original. Solo cambian `OAUTH_ISSUER` y
`OAUTH_JWKS_URL`.

`make keycloak-verify` obtiene un token de Keycloak, lo usa contra ese servidor,
y después demuestra que los dos **no** son intercambiables: cada servidor
devuelve `401` ante el token del otro. Ese rechazo es la validación de audiencia
funcionando, y es la razón de que el realm lleve un mapper de audiencia
explícito: Keycloak omite `aud` si no se lo pides, y un resource server que
acepta un token sin audiencia acepta cualquier token que ese IdP haya emitido, a
quien sea.

## Desarrollo

```bash
make install     # uv sync
make lint        # ruff + mypy --strict
make test-unit   # pruebas rápidas, sin docker
make check       # todo lo que corre CI
```

## Pruebas

Cada capa se prueba contra lo real: PostgreSQL real (nunca SQLite, donde no
existen los índices únicos parciales, los enums nativos ni los timestamps con
zona), el MCP server real, el Authorization Server real.

| Suite | Qué demuestra |
|---|---|
| `tests/unit` | Máquina de estados (7×7 exhaustiva + property-based), afiliación, cartera, orden de la lista de espera, manejo de tiempo, contrato de errores |
| `tests/integration` | Constraints del esquema, reversibilidad de migraciones, determinismo del seed, **dos conexiones vivas compitiendo por un cupo** |
| `tests/contract` | La superficie MCP: catálogo, esquemas, descripciones, resources, prompt, y cada tool ejecutada de punta a punta |
| `tests/security` | **La matriz completa 13 × 3 de scopes**, replay/expiración/alteración de aprobaciones, PKCE obligatorio, audiencia del JWT y `alg=none`, guardas de Host/Origin, rate limiting |
| `scripts/smoke.py` | El camino completo del cliente sobre HTTP real, ejecutado en CI |

813 pruebas: 346 unitarias, 231 de integración, 86 de contrato, 150 de seguridad.
**¿Quieres comprobarlo tú mismo?** [`docs/pruebas-manuales.md`](./docs/pruebas-manuales.md)
es un recorrido de 25 minutos con trece pruebas, cada una diciendo qué correr y
qué deberías ver. [`docs/inspector.es.md`](./docs/inspector.es.md) cubre lo mismo
desde el MCP Inspector.

CI corta con un piso de cobertura del 95% (hoy 99%), `mypy --strict`, `ruff`,
`bandit`, `pip-audit`, y un grep que rompe el build si alguna vez llega al código
un literal con forma de secreto o una llave privada.

## Estructura del repositorio

```
backend/            fuente de verdad del dominio, no sabe nada de MCP
  domain/           lógica pura: estados, cartera, afiliacion, lista_espera, tiempo, errores
  models.py         esquema SQLAlchemy 2.x · api.py  API REST interna
  seed.py           datos sintéticos deterministas (Faker, semilla fija)
mcp_server/
  tools/            read.py · write.py · clinical.py · confirmacion.py
  auth.py           verificación OAuth y scopes         (capas 1-2)
  aprobacion.py     tokens firmados de aprobación       (capa 3)
  errores.py        fallos estructurados y accionables  (capa 4)
  auditoria.py      log de auditoría · limites.py rate  (capa 5)
  oauth/            el Authorization Server propio
tests/              unit · integration · contract · security
docs/               architecture.md · security.md (bilingües)
```

## Licencia

MIT. Proyecto de portafolio, Horizonte Labs.
