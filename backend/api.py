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
from backend.domain import services
from backend.domain.afiliacion import (
    CUOTA_MODERADORA_BY_BRACKET,
    PRIVATE_TARIFF,
    SUBSIDIADO_COPAGO_RATE,
)
from backend.domain.cartera import DEFAULT_POLICY
from backend.domain.errors import DomainError, ErrorCode
from backend.domain.services import PAYMENT_TERM
from backend.domain.time import now_utc, to_clinic_time
from backend.domain.waiting_list import in_queue_order
from backend.enums import Especialidad
from backend.models import Charge, Professional
from backend.schemas import (
    AfiliacionResponse,
    AppointmentDetail,
    AttendanceRequest,
    BookRequest,
    BookResponse,
    CancelRequest,
    CarteraPolicies,
    CarteraResponse,
    ChargeSummary,
    ClinicInfo,
    DayAgenda,
    ErrorResponse,
    FreeSlot,
    JoinWaitingListRequest,
    OfferSlotRequest,
    PatientSummary,
    RescheduleRequest,
    SlotOfferResponse,
    TransitionResponse,
    VisitReasonRequest,
    WaitingEntrySummary,
)

logger = logging.getLogger(__name__)

#: Identity of the caller, forwarded by the MCP server from the OAuth token.
#: The backend does not authenticate, since it is not reachable from outside,
#: but it records who asked. An audit trail with "system" in every row is not an
#: audit trail.
USUARIO_POR_DEFECTO = "mcp-server"

RESPUESTAS_ERROR: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid input or a domain rule violated"},
    403: {"model": ErrorResponse, "description": "Informed consent missing"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "State or concurrency conflict"},
}


def actor_user(
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
ActorDep = Annotated[str, Depends(actor_user)]


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@app.exception_handler(DomainError)
async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
    """Domain failures answer with their own code, message and next step."""
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _validation_envelope(exc: ValidationError | RequestValidationError) -> JSONResponse:
    """Turn a validation failure into the project's error envelope.

    FastAPI's own 422 body is ``{"detail": [...]}``, which is a *second* error
    shape. Two shapes means the caller has to branch on which one it got, so
    request validation is remapped onto the single envelope everything else
    uses, with the offending fields named so the call can be corrected.
    """
    campos = [
        {"campo": ".".join(str(p) for p in e["loc"]), "problema": e["msg"]} for e in exc.errors()
    ]
    names = ", ".join(c["campo"] for c in campos) or "the parameters"
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "codigo": str(ErrorCode.ENTRADA_INVALIDA),
            "mensaje": "The parameters received are not valid.",
            "sugerencia": f"Fix {names} and call the tool again.",
            "detalles": {"campos": campos},
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    return _validation_envelope(exc)


@app.exception_handler(ValidationError)
async def handle_validation(_: Request, exc: ValidationError) -> JSONResponse:
    return _validation_envelope(exc)


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """Unexpected failures are logged in full and answered with one opaque,
    still-structured error. A stack trace never reaches the caller."""
    logger.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "codigo": "ERROR_INTERNO",
            "mensaje": "An internal error occurred while processing the request.",
            "sugerencia": "Retry in a few seconds; if it persists, report the incident.",
        },
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@app.get("/salud", tags=["operación"])
async def health() -> dict[str, Any]:
    """Liveness: the process is up. Does not touch the database on purpose."""
    return {"estado": "ok", "momento": now_utc().isoformat()}


@app.get("/listo", tags=["operación"])
async def ready() -> JSONResponse:
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
                "codigo": str(ErrorCode.ENTRADA_INVALIDA),
                "mensaje": "The database is not available.",
                "sugerencia": "Check that the PostgreSQL container is up.",
            },
        )
    return JSONResponse(status_code=200, content={"estado": "listo"})


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #


@app.get("/clinica", tags=["lectura"], response_model=ClinicInfo)
def clinic_info_route(session: SesionDep) -> ClinicInfo:
    clinica = services.get_clinic(session)
    profesionales = list(
        session.scalars(
            select(Professional)
            .where(Professional.clinica_id == clinica.id)
            .order_by(Professional.especialidad, Professional.nombre)
        )
    )
    return ClinicInfo.of(clinica, profesionales)


@app.get("/politicas/cartera", tags=["lectura"], response_model=CarteraPolicies)
def cartera_policies_resource() -> CarteraPolicies:
    p = DEFAULT_POLICY
    return CarteraPolicies(
        cobra_no_show=p.cobra_no_show,
        monto_no_show=p.monto_no_show,
        dias_gracia=p.dias_gracia,
        umbral_alerta_mora=p.umbral_alerta_mora,
        penaliza_solo_confirmadas=p.penaliza_solo_confirmadas,
        plazo_pago_dias=PAYMENT_TERM.days,
        tarifas_particular=dict(PRIVATE_TARIFF),
        cuota_moderadora_por_nivel=dict(CUOTA_MODERADORA_BY_BRACKET),
        porcentaje_copago_subsidiado=SUBSIDIADO_COPAGO_RATE,
        nota=(
            "Overdue cartera raises a warning when booking, never a block: the clinic "
            "tells the patient and sees them anyway."
        ),
    )


@app.get(
    "/pacientes",
    tags=["lectura"],
    response_model=list[PatientSummary],
    responses=RESPUESTAS_ERROR,
)
def search_patients(
    session: SesionDep,
    documento: Annotated[str | None, Query(max_length=20)] = None,
    nombre: Annotated[str | None, Query(max_length=160)] = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[PatientSummary]:
    encontrados = services.search_patients(
        session, documento=documento, nombre=nombre, limite=limite
    )
    return [PatientSummary.of(p) for p in encontrados]


@app.get(
    "/pacientes/{paciente_id}/afiliacion",
    tags=["lectura"],
    response_model=AfiliacionResponse,
    responses=RESPUESTAS_ERROR,
)
def afiliacion(session: SesionDep, paciente_id: int) -> AfiliacionResponse:
    return AfiliacionResponse.of(
        paciente_id, services.validate_patient_afiliacion(session, paciente_id)
    )


@app.get(
    "/pacientes/{paciente_id}/cartera",
    tags=["lectura"],
    response_model=CarteraResponse,
    responses=RESPUESTAS_ERROR,
)
def cartera(session: SesionDep, paciente_id: int) -> CarteraResponse:
    resumen = services.get_cartera(session, paciente_id)
    cargos = list(
        session.scalars(
            select(Charge)
            .where(Charge.paciente_id == paciente_id, Charge.estado == "pendiente")
            .order_by(Charge.vencimiento)
        )
    )
    return CarteraResponse.of(resumen, cargos)


@app.get(
    "/pacientes/{paciente_id}/citas",
    tags=["lectura"],
    response_model=list[AppointmentDetail],
    responses=RESPUESTAS_ERROR,
)
def patient_appointments_route(
    session: SesionDep,
    paciente_id: int,
    desde: date | None = None,
    hasta: date | None = None,
    limite: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AppointmentDetail]:
    citas = services.list_patient_appointments(
        session, paciente_id, desde=desde, hasta=hasta, limite=limite
    )
    return [AppointmentDetail.of(c, incluir_historial=False) for c in citas]


@app.get(
    "/disponibilidad",
    tags=["lectura"],
    response_model=list[FreeSlot],
    responses=RESPUESTAS_ERROR,
)
def slot_availability_route(
    session: SesionDep,
    especialidad: Especialidad | None = None,
    fecha: date | None = None,
    profesional_id: int | None = None,
    limite: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[FreeSlot]:
    slots = services.list_available_slots(
        session,
        especialidad=especialidad,
        fecha=fecha,
        profesional_id=profesional_id,
        limite=limite,
    )
    return [FreeSlot.of(s) for s in slots]


@app.get(
    "/disponibilidad/{slot_id}",
    tags=["lectura"],
    response_model=FreeSlot,
    responses=RESPUESTAS_ERROR,
)
def bookable_slot(
    session: SesionDep,
    slot_id: int,
    paciente_id: int | None = None,
    especialidad_esperada: Especialidad | None = None,
    excluir_cita_id: int | None = None,
) -> FreeSlot:
    """This slot, if it can still be booked.

    Runs exactly the validation the booking path runs, so a caller can find out
    before committing to anything and gets the same structured error if it
    cannot. Passing `paciente_id` also checks that the patient has no other
    appointment at that hour, which is the other reason a booking fails.
    `excluir_cita_id` skips one appointment in that check, which is what a
    reschedule needs: the visit being moved must not conflict with itself.
    """
    slot = services.validate_booking(
        session,
        slot_id,
        paciente_id=paciente_id,
        especialidad_esperada=especialidad_esperada,
        excluir_cita_id=excluir_cita_id,
    )
    return FreeSlot.of(
        services.AvailableSlot(
            slot_id=slot.id,
            profesional_id=slot.profesional_id,
            profesional=slot.profesional.nombre,
            especialidad=slot.profesional.especialidad,
            inicio=slot.inicio,
            fin=slot.fin,
        )
    )


@app.get(
    "/citas/{cita_id}",
    tags=["lectura"],
    response_model=AppointmentDetail,
    responses=RESPUESTAS_ERROR,
)
def appointment_detail_route(session: SesionDep, cita_id: int) -> AppointmentDetail:
    return AppointmentDetail.of(services.get_appointment(session, cita_id))


@app.get("/agenda/{fecha}", tags=["lectura"], response_model=DayAgenda)
def agenda_for_date(session: SesionDep, fecha: date) -> DayAgenda:
    citas = services.agenda_for_day(session, fecha)
    por_estado: dict[str, int] = {}
    for cita in citas:
        por_estado[str(cita.estado)] = por_estado.get(str(cita.estado), 0) + 1
    return DayAgenda(
        fecha=fecha,
        total=len(citas),
        por_estado=por_estado,
        citas=[AppointmentDetail.of(c, incluir_historial=False) for c in citas],
    )


@app.get("/lista-espera", tags=["lectura"], response_model=list[WaitingEntrySummary])
def waiting_list_route(
    session: SesionDep, especialidad: Especialidad | None = None
) -> list[WaitingEntrySummary]:
    filas = services.waiting_list_entries(session, especialidad)
    order = {
        e.entrada_id: i
        for i, e in enumerate(in_queue_order([services.to_queue_entry(f) for f in filas]))
    }
    filas.sort(key=lambda f: order.get(f.id, 10**6))
    return [WaitingEntrySummary.of(f) for f in filas]


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #


def _transition_message(result: services.TransitionResult) -> str:
    parts = [
        f"Appointment {result.cita.id}: {result.effects.estado_anterior} → "
        f"{result.effects.estado_nuevo}."
    ]
    if result.effects.libera_slot:
        parts.append("The slot is free again in the agenda.")
    if result.created_charge is not None:
        parts.append(
            f"A charge of ${result.created_charge.monto:,.0f} COP was created "
            f"({result.created_charge.concepto})."
        )
    if result.siguiente_en_espera is not None:
        parts.append(
            "There is a patient on the waiting list for "
            f"{result.siguiente_en_espera.especialidad}: "
            f"{result.siguiente_en_espera.paciente.nombre}."
        )
    return " ".join(parts)


def _to_response(result: services.TransitionResult) -> TransitionResponse:
    return TransitionResponse(
        cita=AppointmentDetail.of(result.cita),
        estado_anterior=result.effects.estado_anterior,
        estado_nuevo=result.effects.estado_nuevo,
        libero_cupo=result.effects.libera_slot,
        genero_cargo=result.effects.genera_cargo,
        cargo=(
            ChargeSummary.of(result.created_charge) if result.created_charge is not None else None
        ),
        siguiente_en_lista_espera=(
            WaitingEntrySummary.of(result.siguiente_en_espera)
            if result.siguiente_en_espera is not None
            else None
        ),
        mensaje=_transition_message(result),
    )


@app.post("/citas", tags=["escritura"], response_model=BookResponse, responses=RESPUESTAS_ERROR)
def book_route(session: SesionDep, actor: ActorDep, body: BookRequest) -> BookResponse:
    result = services.book_appointment(
        session,
        paciente_id=body.paciente_id,
        slot_id=body.slot_id,
        usuario=actor,
        idempotency_key=body.idempotency_key,
        especialidad_esperada=body.especialidad_esperada,
    )
    return BookResponse(
        cita=AppointmentDetail.of(result.cita),
        afiliacion=AfiliacionResponse.of(result.cita.paciente_id, result.afiliacion),
        alerta_cartera=result.alerta_cartera,
        reutilizada=result.reutilizada,
    )


@app.post(
    "/citas/{cita_id}/confirmar",
    tags=["escritura"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def confirm_route(session: SesionDep, actor: ActorDep, cita_id: int) -> TransitionResponse:
    return _to_response(services.confirm_appointment(session, cita_id, usuario=actor))


@app.post(
    "/citas/{cita_id}/cancelar",
    tags=["escritura"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def cancel_route(
    session: SesionDep, actor: ActorDep, cita_id: int, body: CancelRequest
) -> TransitionResponse:
    return _to_response(
        services.cancel_appointment(session, cita_id, motivo=body.motivo, usuario=actor)
    )


@app.post(
    "/citas/{cita_id}/reprogramar",
    tags=["escritura"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def reschedule_route(
    session: SesionDep, actor: ActorDep, cita_id: int, body: RescheduleRequest
) -> TransitionResponse:
    return _to_response(
        services.reschedule_appointment(
            session, cita_id, body.nuevo_slot_id, usuario=actor, motivo=body.motivo
        )
    )


@app.post(
    "/citas/{cita_id}/asistencia",
    tags=["escritura"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def attendance_route(
    session: SesionDep, actor: ActorDep, cita_id: int, body: AttendanceRequest
) -> TransitionResponse:
    return _to_response(services.record_attendance(session, cita_id, body.estado, usuario=actor))


@app.post(
    "/lista-espera/ofrecer",
    tags=["escritura"],
    response_model=SlotOfferResponse,
    responses=RESPUESTAS_ERROR,
)
def offer_slot_route(
    session: SesionDep, actor: ActorDep, body: OfferSlotRequest
) -> SlotOfferResponse:
    oferta = services.offer_slot_to_waiting_list(session, body.slot_id, usuario=actor)
    inicio = f"{to_clinic_time(oferta.slot.inicio):%Y-%m-%d %H:%M}"
    return SlotOfferResponse(
        entrada_id=oferta.entry.id,
        paciente_id=oferta.paciente.id,
        paciente=oferta.paciente.nombre,
        telefono=oferta.paciente.telefono,
        especialidad=oferta.entry.especialidad,
        prioridad=oferta.entry.prioridad,
        posicion_original=oferta.posicion_original,
        slot_id=oferta.slot.id,
        inicio_local=inicio,
        mensaje=(
            f"Contact {oferta.paciente.nombre} ({oferta.paciente.telefono}) to offer "
            f"them the {inicio} slot. They were number {oferta.posicion_original} on "
            f"the {oferta.entry.especialidad} list."
        ),
    )


@app.post(
    "/lista-espera",
    tags=["escritura"],
    response_model=WaitingEntrySummary,
    responses=RESPUESTAS_ERROR,
)
def join_waiting_list_route(
    session: SesionDep, body: JoinWaitingListRequest
) -> WaitingEntrySummary:
    entry = services.join_waiting_list(
        session,
        paciente_id=body.paciente_id,
        especialidad=body.especialidad,
        prioridad=body.prioridad,
        notas=body.notas,
    )
    session.flush()
    return WaitingEntrySummary.of(entry)


@app.post(
    "/citas/{cita_id}/motivo",
    tags=["clínico"],
    response_model=AppointmentDetail,
    responses=RESPUESTAS_ERROR,
)
def record_reason_route(
    session: SesionDep, actor: ActorDep, cita_id: int, body: VisitReasonRequest
) -> AppointmentDetail:
    """Clinical data (Res. 2654/2019). Refused without recorded consent."""
    return AppointmentDetail.of(
        services.record_visit_reason(session, cita_id, body.motivo, usuario=actor)
    )
