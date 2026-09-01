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
from backend.enums import ChargeState, Specialty
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
    fields = [
        {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
    ]
    names = ", ".join(c["field"] for c in fields) or "the parameters"
    return JSONResponse(
        status_code=422,
        content={
            "error": True,
            "code": str(ErrorCode.INVALID_INPUT),
            "message": "The parameters received are not valid.",
            "suggestion": f"Fix {names} and call the tool again.",
            "details": {"fields": fields},
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
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred while processing the request.",
            "suggestion": "Retry in a few seconds; if it persists, report the incident.",
        },
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["operations"])
async def health() -> dict[str, Any]:
    """Liveness: the process is up. Does not touch the database on purpose."""
    return {"status": "ok", "time": now_utc().isoformat()}


@app.get("/ready", tags=["operations"])
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
                "code": str(ErrorCode.INVALID_INPUT),
                "message": "The database is not available.",
                "suggestion": "Check that the PostgreSQL container is up.",
            },
        )
    return JSONResponse(status_code=200, content={"status": "ready"})


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #


@app.get("/clinic", tags=["read"], response_model=ClinicInfo)
def clinic_info_route(session: SesionDep) -> ClinicInfo:
    clinic = services.get_clinic(session)
    professionals = list(
        session.scalars(
            select(Professional)
            .where(Professional.clinic_id == clinic.id)
            .order_by(Professional.specialty, Professional.name)
        )
    )
    return ClinicInfo.of(clinic, professionals)


@app.get("/policies/cartera", tags=["read"], response_model=CarteraPolicies)
def cartera_policies_resource() -> CarteraPolicies:
    p = DEFAULT_POLICY
    return CarteraPolicies(
        charges_no_show=p.charges_no_show,
        no_show_amount=p.no_show_amount,
        grace_days=p.grace_days,
        overdue_alert_threshold=p.overdue_alert_threshold,
        penalises_only_confirmed=p.penalises_only_confirmed,
        payment_term_days=PAYMENT_TERM.days,
        particular_tariffs=dict(PRIVATE_TARIFF),
        cuota_moderadora_by_level=dict(CUOTA_MODERADORA_BY_BRACKET),
        subsidiado_copago_rate=SUBSIDIADO_COPAGO_RATE,
        note=(
            "Overdue cartera raises a warning when booking, never a block: the clinic "
            "tells the patient and sees them anyway."
        ),
    )


@app.get(
    "/patients",
    tags=["read"],
    response_model=list[PatientSummary],
    responses=RESPUESTAS_ERROR,
)
def search_patients(
    session: SesionDep,
    document_number: Annotated[str | None, Query(max_length=20)] = None,
    name: Annotated[str | None, Query(max_length=160)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[PatientSummary]:
    encontrados = services.search_patients(
        session, document_number=document_number, name=name, limit=limit
    )
    return [PatientSummary.of(p) for p in encontrados]


@app.get(
    "/patients/{patient_id}/afiliacion",
    tags=["read"],
    response_model=AfiliacionResponse,
    responses=RESPUESTAS_ERROR,
)
def afiliacion(session: SesionDep, patient_id: int) -> AfiliacionResponse:
    return AfiliacionResponse.of(
        patient_id, services.validate_patient_afiliacion(session, patient_id)
    )


@app.get(
    "/patients/{patient_id}/cartera",
    tags=["read"],
    response_model=CarteraResponse,
    responses=RESPUESTAS_ERROR,
)
def cartera(session: SesionDep, patient_id: int) -> CarteraResponse:
    summary = services.get_cartera(session, patient_id)
    charges = list(
        session.scalars(
            select(Charge)
            .where(Charge.patient_id == patient_id, Charge.status == ChargeState.PENDING)
            .order_by(Charge.due_date)
        )
    )
    return CarteraResponse.of(summary, charges)


@app.get(
    "/patients/{patient_id}/appointments",
    tags=["read"],
    response_model=list[AppointmentDetail],
    responses=RESPUESTAS_ERROR,
)
def patient_appointments_route(
    session: SesionDep,
    patient_id: int,
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AppointmentDetail]:
    appointments = services.list_patient_appointments(
        session, patient_id, since=since, until=until, limit=limit
    )
    return [AppointmentDetail.of(c, incluir_historial=False) for c in appointments]


@app.get(
    "/availability",
    tags=["read"],
    response_model=list[FreeSlot],
    responses=RESPUESTAS_ERROR,
)
def slot_availability_route(
    session: SesionDep,
    specialty: Specialty | None = None,
    day: date | None = None,
    professional_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[FreeSlot]:
    slots = services.list_available_slots(
        session,
        specialty=specialty,
        day=day,
        professional_id=professional_id,
        limit=limit,
    )
    return [FreeSlot.of(s) for s in slots]


@app.get(
    "/availability/{slot_id}",
    tags=["read"],
    response_model=FreeSlot,
    responses=RESPUESTAS_ERROR,
)
def bookable_slot(
    session: SesionDep,
    slot_id: int,
    patient_id: int | None = None,
    expected_specialty: Specialty | None = None,
    exclude_appointment_id: int | None = None,
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
        patient_id=patient_id,
        expected_specialty=expected_specialty,
        exclude_appointment_id=exclude_appointment_id,
    )
    return FreeSlot.of(
        services.AvailableSlot(
            slot_id=slot.id,
            professional_id=slot.professional_id,
            professional=slot.professional.name,
            specialty=slot.professional.specialty,
            start=slot.start,
            end=slot.end,
        )
    )


@app.get(
    "/appointments/{appointment_id}",
    tags=["read"],
    response_model=AppointmentDetail,
    responses=RESPUESTAS_ERROR,
)
def appointment_detail_route(session: SesionDep, appointment_id: int) -> AppointmentDetail:
    return AppointmentDetail.of(services.get_appointment(session, appointment_id))


@app.get("/agenda/{day}", tags=["read"], response_model=DayAgenda)
def agenda_for_date(session: SesionDep, day: date) -> DayAgenda:
    appointments = services.agenda_for_day(session, day)
    by_status: dict[str, int] = {}
    for appointment in appointments:
        by_status[str(appointment.status)] = by_status.get(str(appointment.status), 0) + 1
    return DayAgenda(
        day=day,
        total=len(appointments),
        by_status=by_status,
        appointments=[AppointmentDetail.of(c, incluir_historial=False) for c in appointments],
    )


@app.get("/waiting-list", tags=["read"], response_model=list[WaitingEntrySummary])
def waiting_list_route(
    session: SesionDep, specialty: Specialty | None = None
) -> list[WaitingEntrySummary]:
    filas = services.waiting_list_entries(session, specialty)
    order = {
        e.entry_id: i
        for i, e in enumerate(in_queue_order([services.to_queue_entry(f) for f in filas]))
    }
    filas.sort(key=lambda f: order.get(f.id, 10**6))
    return [WaitingEntrySummary.of(f) for f in filas]


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #


def _transition_message(result: services.TransitionResult) -> str:
    parts = [
        f"Appointment {result.appointment.id}: {result.effects.previous_status} → "
        f"{result.effects.new_status}."
    ]
    if result.effects.libera_slot:
        parts.append("The slot is free again in the agenda.")
    if result.created_charge is not None:
        parts.append(
            f"A charge of ${result.created_charge.amount:,.0f} COP was created "
            f"({result.created_charge.concept})."
        )
    if result.siguiente_en_espera is not None:
        parts.append(
            "There is a patient on the waiting list for "
            f"{result.siguiente_en_espera.specialty}: "
            f"{result.siguiente_en_espera.patient.name}."
        )
    return " ".join(parts)


def _to_response(result: services.TransitionResult) -> TransitionResponse:
    return TransitionResponse(
        appointment=AppointmentDetail.of(result.appointment),
        previous_status=result.effects.previous_status,
        new_status=result.effects.new_status,
        freed_slot=result.effects.libera_slot,
        created_charge=result.effects.genera_cargo,
        charge=(
            ChargeSummary.of(result.created_charge) if result.created_charge is not None else None
        ),
        next_in_waiting_list=(
            WaitingEntrySummary.of(result.siguiente_en_espera)
            if result.siguiente_en_espera is not None
            else None
        ),
        message=_transition_message(result),
    )


@app.post("/appointments", tags=["write"], response_model=BookResponse, responses=RESPUESTAS_ERROR)
def book_route(session: SesionDep, actor: ActorDep, body: BookRequest) -> BookResponse:
    result = services.book_appointment(
        session,
        patient_id=body.patient_id,
        slot_id=body.slot_id,
        user=actor,
        idempotency_key=body.idempotency_key,
        expected_specialty=body.expected_specialty,
    )
    return BookResponse(
        appointment=AppointmentDetail.of(result.appointment),
        afiliacion=AfiliacionResponse.of(result.appointment.patient_id, result.afiliacion),
        cartera_alert=result.cartera_alert,
        reused=result.reused,
    )


@app.post(
    "/appointments/{appointment_id}/confirm",
    tags=["write"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def confirm_route(session: SesionDep, actor: ActorDep, appointment_id: int) -> TransitionResponse:
    return _to_response(services.confirm_appointment(session, appointment_id, user=actor))


@app.post(
    "/appointments/{appointment_id}/cancel",
    tags=["write"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def cancel_route(
    session: SesionDep, actor: ActorDep, appointment_id: int, body: CancelRequest
) -> TransitionResponse:
    return _to_response(
        services.cancel_appointment(session, appointment_id, reason=body.reason, user=actor)
    )


@app.post(
    "/appointments/{appointment_id}/reschedule",
    tags=["write"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def reschedule_route(
    session: SesionDep, actor: ActorDep, appointment_id: int, body: RescheduleRequest
) -> TransitionResponse:
    return _to_response(
        services.reschedule_appointment(
            session, appointment_id, body.new_slot_id, user=actor, reason=body.reason
        )
    )


@app.post(
    "/appointments/{appointment_id}/attendance",
    tags=["write"],
    response_model=TransitionResponse,
    responses=RESPUESTAS_ERROR,
)
def attendance_route(
    session: SesionDep, actor: ActorDep, appointment_id: int, body: AttendanceRequest
) -> TransitionResponse:
    return _to_response(
        services.record_attendance(session, appointment_id, body.status, user=actor)
    )


@app.post(
    "/waiting-list/offer",
    tags=["write"],
    response_model=SlotOfferResponse,
    responses=RESPUESTAS_ERROR,
)
def offer_slot_route(
    session: SesionDep, actor: ActorDep, body: OfferSlotRequest
) -> SlotOfferResponse:
    oferta = services.offer_slot_to_waiting_list(session, body.slot_id, user=actor)
    start = f"{to_clinic_time(oferta.slot.start):%Y-%m-%d %H:%M}"
    return SlotOfferResponse(
        entry_id=oferta.entry.id,
        patient_id=oferta.patient.id,
        patient=oferta.patient.name,
        phone=oferta.patient.phone,
        specialty=oferta.entry.specialty,
        priority=oferta.entry.priority,
        original_position=oferta.original_position,
        slot_id=oferta.slot.id,
        start_local=start,
        message=(
            f"Contact {oferta.patient.name} ({oferta.patient.phone}) to offer "
            f"them the {start} slot. They were number {oferta.original_position} on "
            f"the {oferta.entry.specialty} list."
        ),
    )


@app.post(
    "/waiting-list",
    tags=["write"],
    response_model=WaitingEntrySummary,
    responses=RESPUESTAS_ERROR,
)
def join_waiting_list_route(
    session: SesionDep, body: JoinWaitingListRequest
) -> WaitingEntrySummary:
    entry = services.join_waiting_list(
        session,
        patient_id=body.patient_id,
        specialty=body.specialty,
        priority=body.priority,
        notes=body.notes,
    )
    session.flush()
    return WaitingEntrySummary.of(entry)


@app.post(
    "/appointments/{appointment_id}/reason",
    tags=["clinical"],
    response_model=AppointmentDetail,
    responses=RESPUESTAS_ERROR,
)
def record_reason_route(
    session: SesionDep, actor: ActorDep, appointment_id: int, body: VisitReasonRequest
) -> AppointmentDetail:
    """Clinical data (Res. 2654/2019). Refused without recorded consent."""
    return AppointmentDetail.of(
        services.record_visit_reason(session, appointment_id, body.reason, user=actor)
    )
