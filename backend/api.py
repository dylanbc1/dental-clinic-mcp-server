"""Internal REST API of the domain backend.

This is the source of truth the MCP server wraps. MCP clients never reach it
directly; the tools call it server-to-server. That is what keeps the security
controls in exactly one place.

One rule is inherited by every endpoint: **every failure answers with the
structured error envelope**, never a bare 500 and never a stack trace.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import get_engine, get_session
from backend.domain import servicios
from backend.domain.afiliacion import (
    CUOTA_MODERADORA_POR_NIVEL,
    PORCENTAJE_COPAGO_SUBSIDIADO,
    TARIFA_PARTICULAR,
)
from backend.domain.cartera import POLITICA_POR_DEFECTO
from backend.domain.errores import CodigoError, ErrorDominio
from backend.domain.lista_espera import ordenar
from backend.domain.servicios import PLAZO_PAGO
from backend.domain.tiempo import a_local, ahora_utc
from backend.enums import Especialidad
from backend.models import Cargo, Profesional
from backend.schemas import (
    AfiliacionRespuesta,
    AgendaDelDia,
    AgendarRequest,
    AgendarRespuesta,
    AsistenciaRequest,
    CancelarRequest,
    CargoResumen,
    CarteraRespuesta,
    CitaDetalle,
    ClinicaInfo,
    EntradaEsperaResumen,
    InscribirEsperaRequest,
    MotivoConsultaRequest,
    OfertaCupoRespuesta,
    OfrecerCupoRequest,
    PacienteResumen,
    PoliticasCartera,
    ReprogramarRequest,
    RespuestaError,
    SlotLibre,
    TransicionRespuesta,
)

logger = logging.getLogger(__name__)

#: Identity of the caller, forwarded by the MCP server from the OAuth token.
#: The backend does not authenticate, since it is not reachable from outside,
#: but it records who asked. An audit trail with "system" in every row is not an
#: audit trail.
USUARIO_POR_DEFECTO = "mcp-server"

RESPUESTAS_ERROR: dict[int | str, dict[str, Any]] = {
    400: {"model": RespuestaError, "description": "Entrada inválida o regla de dominio violada"},
    403: {"model": RespuestaError, "description": "Falta consentimiento informado"},
    404: {"model": RespuestaError, "description": "Recurso no encontrado"},
    409: {"model": RespuestaError, "description": "Conflicto de estado o de concurrencia"},
}


def usuario_actor(
    x_actor: Annotated[str | None, Header(alias="X-Actor")] = None,
) -> str:
    """Who is performing the operation, for the audit trail."""
    return (x_actor or USUARIO_POR_DEFECTO).strip()[:120]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("backend up in %s environment", settings.app_env)
    yield
    get_engine().dispose()


app = FastAPI(
    title="Clínica Odontológica · API de dominio",
    description=(
        "Internal source of truth. The MCP server consumes this API; MCP clients "
        "never reach it directly."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

SesionDep = Annotated[Session, Depends(get_session)]
ActorDep = Annotated[str, Depends(usuario_actor)]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@app.exception_handler(ErrorDominio)
async def manejar_error_dominio(_: Request, exc: ErrorDominio) -> JSONResponse:
    """Domain failures answer with their own code, message and next step."""
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _envoltura_de_validacion(exc: ValidationError | RequestValidationError) -> JSONResponse:
    """Turn a validation failure into the project's error envelope.

    FastAPI's own 422 body is ``{"detail": [...]}``, which is a *second* error
    shape. Two shapes means the caller has to branch on which one it got, so
    request validation is remapped onto the single envelope everything else
    uses, with the offending fields named so the call can be corrected.
    """
    campos = [
        {"campo": ".".join(str(p) for p in e["loc"]), "problema": e["msg"]} for e in exc.errors()
    ]
    nombres = ", ".join(c["campo"] for c in campos) or "los parámetros"
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "codigo": str(CodigoError.ENTRADA_INVALIDA),
            "mensaje": "Los parámetros recibidos no son válidos.",
            "sugerencia": f"Corrige {nombres} y vuelve a llamar la herramienta.",
            "detalles": {"campos": campos},
        },
    )


@app.exception_handler(RequestValidationError)
async def manejar_validacion_de_peticion(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _envoltura_de_validacion(exc)


@app.exception_handler(ValidationError)
async def manejar_validacion(_: Request, exc: ValidationError) -> JSONResponse:
    return _envoltura_de_validacion(exc)


@app.exception_handler(Exception)
async def manejar_error_inesperado(_: Request, exc: Exception) -> JSONResponse:
    """Unexpected failures are logged in full and answered with one opaque,
    still-structured error. A stack trace never reaches the caller."""
    logger.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "codigo": "ERROR_INTERNO",
            "mensaje": "Ocurrió un error interno procesando la solicitud.",
            "sugerencia": "Reintenta en unos segundos; si persiste, reporta el incidente.",
        },
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@app.get("/salud", tags=["operación"])
async def salud() -> dict[str, Any]:
    """Liveness: the process is up. Does not touch the database on purpose."""
    return {"estado": "ok", "momento": ahora_utc().isoformat()}


@app.get("/listo", tags=["operación"])
async def listo() -> JSONResponse:
    """Readiness: the process can actually serve traffic (database reachable)."""
    try:
        with get_engine().connect() as conexion:
            conexion.execute(text("select 1"))
    except Exception as exc:  # readiness must never raise, whatever the cause
        logger.warning("readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": True,
                "codigo": str(CodigoError.ENTRADA_INVALIDA),
                "mensaje": "La base de datos no está disponible.",
                "sugerencia": "Verifica que el contenedor de PostgreSQL esté arriba.",
            },
        )
    return JSONResponse(status_code=200, content={"estado": "listo"})


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #


@app.get("/clinica", tags=["lectura"], response_model=ClinicaInfo)
def info_clinica(session: SesionDep) -> ClinicaInfo:
    clinica = servicios.obtener_clinica(session)
    profesionales = list(
        session.scalars(
            select(Profesional)
            .where(Profesional.clinica_id == clinica.id)
            .order_by(Profesional.especialidad, Profesional.nombre)
        )
    )
    return ClinicaInfo.desde(clinica, profesionales)


@app.get("/politicas/cartera", tags=["lectura"], response_model=PoliticasCartera)
def politicas_cartera() -> PoliticasCartera:
    p = POLITICA_POR_DEFECTO
    return PoliticasCartera(
        cobra_no_show=p.cobra_no_show,
        monto_no_show=p.monto_no_show,
        dias_gracia=p.dias_gracia,
        umbral_alerta_mora=p.umbral_alerta_mora,
        penaliza_solo_confirmadas=p.penaliza_solo_confirmadas,
        plazo_pago_dias=PLAZO_PAGO.days,
        tarifas_particular=dict(TARIFA_PARTICULAR),
        cuota_moderadora_por_nivel=dict(CUOTA_MODERADORA_POR_NIVEL),
        porcentaje_copago_subsidiado=PORCENTAJE_COPAGO_SUBSIDIADO,
        nota=(
            "Un saldo vencido genera alerta al agendar, nunca bloqueo: la clínica "
            "informa al paciente y atiende igual."
        ),
    )


@app.get(
    "/pacientes",
    tags=["lectura"],
    response_model=list[PacienteResumen],
    responses=RESPUESTAS_ERROR,
)
def buscar_pacientes(
    session: SesionDep,
    documento: Annotated[str | None, Query(max_length=20)] = None,
    nombre: Annotated[str | None, Query(max_length=160)] = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[PacienteResumen]:
    encontrados = servicios.buscar_pacientes(
        session, documento=documento, nombre=nombre, limite=limite
    )
    return [PacienteResumen.desde(p) for p in encontrados]


@app.get(
    "/pacientes/{paciente_id}/afiliacion",
    tags=["lectura"],
    response_model=AfiliacionRespuesta,
    responses=RESPUESTAS_ERROR,
)
def afiliacion(session: SesionDep, paciente_id: int) -> AfiliacionRespuesta:
    return AfiliacionRespuesta.desde(
        paciente_id, servicios.validar_afiliacion_paciente(session, paciente_id)
    )


@app.get(
    "/pacientes/{paciente_id}/cartera",
    tags=["lectura"],
    response_model=CarteraRespuesta,
    responses=RESPUESTAS_ERROR,
)
def cartera(session: SesionDep, paciente_id: int) -> CarteraRespuesta:
    resumen = servicios.consultar_cartera(session, paciente_id)
    cargos = list(
        session.scalars(
            select(Cargo)
            .where(Cargo.paciente_id == paciente_id, Cargo.estado == "pendiente")
            .order_by(Cargo.vencimiento)
        )
    )
    return CarteraRespuesta.desde(resumen, cargos)


@app.get(
    "/pacientes/{paciente_id}/citas",
    tags=["lectura"],
    response_model=list[CitaDetalle],
    responses=RESPUESTAS_ERROR,
)
def citas_de_paciente(
    session: SesionDep,
    paciente_id: int,
    desde: date | None = None,
    hasta: date | None = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[CitaDetalle]:
    citas = servicios.listar_citas_paciente(
        session, paciente_id, desde=desde, hasta=hasta, limite=limite
    )
    return [CitaDetalle.desde(c, incluir_historial=False) for c in citas]


@app.get(
    "/disponibilidad",
    tags=["lectura"],
    response_model=list[SlotLibre],
    responses=RESPUESTAS_ERROR,
)
def disponibilidad(
    session: SesionDep,
    especialidad: Especialidad | None = None,
    fecha: date | None = None,
    profesional_id: int | None = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[SlotLibre]:
    slots = servicios.consultar_disponibilidad(
        session,
        especialidad=especialidad,
        fecha=fecha,
        profesional_id=profesional_id,
        limite=limite,
    )
    return [SlotLibre.desde(s) for s in slots]


@app.get(
    "/disponibilidad/{slot_id}",
    tags=["lectura"],
    response_model=SlotLibre,
    responses=RESPUESTAS_ERROR,
)
def slot_reservable(
    session: SesionDep,
    slot_id: int,
    paciente_id: int | None = None,
    especialidad_esperada: Especialidad | None = None,
    excluir_cita_id: int | None = None,
) -> SlotLibre:
    """This slot, if it can still be booked.

    Runs exactly the validation the booking path runs, so a caller can find out
    before committing to anything and gets the same structured error if it
    cannot. Passing `paciente_id` also checks that the patient has no other
    appointment at that hour, which is the other reason a booking fails.
    `excluir_cita_id` skips one appointment in that check, which is what a
    reschedule needs: the visit being moved must not conflict with itself.
    """
    slot = servicios.validar_reserva(
        session,
        slot_id,
        paciente_id=paciente_id,
        especialidad_esperada=especialidad_esperada,
        excluir_cita_id=excluir_cita_id,
    )
    return SlotLibre.desde(
        servicios.SlotDisponible(
            slot_id=slot.id,
            profesional_id=slot.profesional_id,
            profesional=slot.profesional.nombre,
            especialidad=slot.profesional.especialidad,
            inicio=slot.inicio,
            fin=slot.fin,
        )
    )


@app.get(
    "/citas/{cita_id}", tags=["lectura"], response_model=CitaDetalle, responses=RESPUESTAS_ERROR
)
def detalle_cita(session: SesionDep, cita_id: int) -> CitaDetalle:
    return CitaDetalle.desde(servicios.obtener_cita(session, cita_id))


@app.get("/agenda/{fecha}", tags=["lectura"], response_model=AgendaDelDia)
def agenda_dia(session: SesionDep, fecha: date) -> AgendaDelDia:
    citas = servicios.agenda_del_dia(session, fecha)
    por_estado: dict[str, int] = {}
    for cita in citas:
        por_estado[str(cita.estado)] = por_estado.get(str(cita.estado), 0) + 1
    return AgendaDelDia(
        fecha=fecha,
        total=len(citas),
        por_estado=por_estado,
        citas=[CitaDetalle.desde(c, incluir_historial=False) for c in citas],
    )


@app.get("/lista-espera", tags=["lectura"], response_model=list[EntradaEsperaResumen])
def lista_espera(
    session: SesionDep, especialidad: Especialidad | None = None
) -> list[EntradaEsperaResumen]:
    filas = servicios.entradas_lista_espera(session, especialidad)
    orden = {
        e.entrada_id: i
        for i, e in enumerate(ordenar([servicios.a_entrada_dominio(f) for f in filas]))
    }
    filas.sort(key=lambda f: orden.get(f.id, 10**6))
    return [EntradaEsperaResumen.desde(f) for f in filas]


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #


def _mensaje_transicion(resultado: servicios.ResultadoTransicion) -> str:
    partes = [
        f"Cita {resultado.cita.id}: {resultado.efectos.estado_anterior} → "
        f"{resultado.efectos.estado_nuevo}."
    ]
    if resultado.efectos.libera_slot:
        partes.append("El cupo quedó libre en la agenda.")
    if resultado.cargo_generado is not None:
        partes.append(
            f"Se generó un cargo de ${resultado.cargo_generado.monto:,.0f} COP "
            f"({resultado.cargo_generado.concepto})."
        )
    if resultado.siguiente_en_espera is not None:
        partes.append(
            "Hay un paciente en lista de espera para "
            f"{resultado.siguiente_en_espera.especialidad}: "
            f"{resultado.siguiente_en_espera.paciente.nombre}."
        )
    return " ".join(partes)


def _a_respuesta(resultado: servicios.ResultadoTransicion) -> TransicionRespuesta:
    return TransicionRespuesta(
        cita=CitaDetalle.desde(resultado.cita),
        estado_anterior=resultado.efectos.estado_anterior,
        estado_nuevo=resultado.efectos.estado_nuevo,
        libero_cupo=resultado.efectos.libera_slot,
        genero_cargo=resultado.efectos.genera_cargo,
        cargo=(
            CargoResumen.desde(resultado.cargo_generado)
            if resultado.cargo_generado is not None
            else None
        ),
        siguiente_en_lista_espera=(
            EntradaEsperaResumen.desde(resultado.siguiente_en_espera)
            if resultado.siguiente_en_espera is not None
            else None
        ),
        mensaje=_mensaje_transicion(resultado),
    )


@app.post("/citas", tags=["escritura"], response_model=AgendarRespuesta, responses=RESPUESTAS_ERROR)
def agendar(session: SesionDep, actor: ActorDep, cuerpo: AgendarRequest) -> AgendarRespuesta:
    resultado = servicios.agendar_cita(
        session,
        paciente_id=cuerpo.paciente_id,
        slot_id=cuerpo.slot_id,
        usuario=actor,
        idempotency_key=cuerpo.idempotency_key,
        especialidad_esperada=cuerpo.especialidad_esperada,
    )
    return AgendarRespuesta(
        cita=CitaDetalle.desde(resultado.cita),
        afiliacion=AfiliacionRespuesta.desde(resultado.cita.paciente_id, resultado.afiliacion),
        alerta_cartera=resultado.alerta_cartera,
        reutilizada=resultado.reutilizada,
    )


@app.post(
    "/citas/{cita_id}/confirmar",
    tags=["escritura"],
    response_model=TransicionRespuesta,
    responses=RESPUESTAS_ERROR,
)
def confirmar(session: SesionDep, actor: ActorDep, cita_id: int) -> TransicionRespuesta:
    return _a_respuesta(servicios.confirmar_cita(session, cita_id, usuario=actor))


@app.post(
    "/citas/{cita_id}/cancelar",
    tags=["escritura"],
    response_model=TransicionRespuesta,
    responses=RESPUESTAS_ERROR,
)
def cancelar(
    session: SesionDep, actor: ActorDep, cita_id: int, cuerpo: CancelarRequest
) -> TransicionRespuesta:
    return _a_respuesta(
        servicios.cancelar_cita(session, cita_id, motivo=cuerpo.motivo, usuario=actor)
    )


@app.post(
    "/citas/{cita_id}/reprogramar",
    tags=["escritura"],
    response_model=TransicionRespuesta,
    responses=RESPUESTAS_ERROR,
)
def reprogramar(
    session: SesionDep, actor: ActorDep, cita_id: int, cuerpo: ReprogramarRequest
) -> TransicionRespuesta:
    return _a_respuesta(
        servicios.reprogramar_cita(
            session, cita_id, cuerpo.nuevo_slot_id, usuario=actor, motivo=cuerpo.motivo
        )
    )


@app.post(
    "/citas/{cita_id}/asistencia",
    tags=["escritura"],
    response_model=TransicionRespuesta,
    responses=RESPUESTAS_ERROR,
)
def asistencia(
    session: SesionDep, actor: ActorDep, cita_id: int, cuerpo: AsistenciaRequest
) -> TransicionRespuesta:
    return _a_respuesta(
        servicios.registrar_asistencia(session, cita_id, cuerpo.estado, usuario=actor)
    )


@app.post(
    "/lista-espera/ofrecer",
    tags=["escritura"],
    response_model=OfertaCupoRespuesta,
    responses=RESPUESTAS_ERROR,
)
def ofrecer_cupo(
    session: SesionDep, actor: ActorDep, cuerpo: OfrecerCupoRequest
) -> OfertaCupoRespuesta:
    oferta = servicios.ofrecer_cupo_lista_espera(session, cuerpo.slot_id, usuario=actor)
    inicio = f"{a_local(oferta.slot.inicio):%Y-%m-%d %H:%M}"
    return OfertaCupoRespuesta(
        entrada_id=oferta.entrada.id,
        paciente_id=oferta.paciente.id,
        paciente=oferta.paciente.nombre,
        telefono=oferta.paciente.telefono,
        especialidad=oferta.entrada.especialidad,
        prioridad=oferta.entrada.prioridad,
        posicion_original=oferta.posicion_original,
        slot_id=oferta.slot.id,
        inicio_local=inicio,
        mensaje=(
            f"Contacta a {oferta.paciente.nombre} ({oferta.paciente.telefono}) para "
            f"ofrecerle el cupo del {inicio}. Era el número {oferta.posicion_original} "
            f"de la lista de {oferta.entrada.especialidad}."
        ),
    )


@app.post(
    "/lista-espera",
    tags=["escritura"],
    response_model=EntradaEsperaResumen,
    responses=RESPUESTAS_ERROR,
)
def inscribir_espera(session: SesionDep, cuerpo: InscribirEsperaRequest) -> EntradaEsperaResumen:
    entrada = servicios.inscribir_en_lista_espera(
        session,
        paciente_id=cuerpo.paciente_id,
        especialidad=cuerpo.especialidad,
        prioridad=cuerpo.prioridad,
        notas=cuerpo.notas,
    )
    session.flush()
    return EntradaEsperaResumen.desde(entrada)


@app.post(
    "/citas/{cita_id}/motivo",
    tags=["clínico"],
    response_model=CitaDetalle,
    responses=RESPUESTAS_ERROR,
)
def registrar_motivo(
    session: SesionDep, actor: ActorDep, cita_id: int, cuerpo: MotivoConsultaRequest
) -> CitaDetalle:
    """Clinical data (Res. 2654/2019). Refused without recorded consent."""
    return CitaDetalle.desde(
        servicios.registrar_motivo_consulta(session, cita_id, cuerpo.motivo, usuario=actor)
    )
