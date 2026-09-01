"""Internal REST API of the domain backend.

This is the source of truth the MCP server wraps. MCP clients never reach it
directly; the tools call it server-to-server. That is what keeps the security
controls in exactly one place.

One rule is inherited by every endpoint: **every failure answers with the
structured error envelope**, never a bare 500 and never a stack trace.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.base import BaseHTTPMiddleware

from backend import internal_auth
from backend.config import get_settings
from backend.database import get_engine, get_session
from backend.domain import services
from backend.domain.affiliation import (
    CUOTA_MODERADORA_BY_BRACKET,
    PRIVATE_TARIFF,
    SUBSIDIADO_COPAGO_RATE,
)
from backend.domain.cartera import DEFAULT_POLICY
from backend.domain.errors import AppointmentNotFound, DomainError, ErrorCode
from backend.domain.services import PAYMENT_TERM
from backend.domain.time import now_utc, to_clinic_time
from backend.domain.waiting_list import in_queue_order
from backend.enums import ChargeState, Specialty
from backend.models import Appointment, Charge, Professional
from backend.schemas import (
    AffiliationResponse,
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
DEFAULT_USER = "mcp-server"

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid input or a domain rule violated"},
    403: {"model": ErrorResponse, "description": "Informed consent missing"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "State or concurrency conflict"},
}


def actor_user(
    x_actor: Annotated[str | None, Header(alias="X-Actor")] = None,
) -> str:
    """Who is performing the operation, for the audit trail.

    Safe to read straight off the header because `SignedCallerOnly` has already
    refused anything whose signature does not cover this exact value. Before
    that middleware existed the header was simply believed.
    """
    return (x_actor or DEFAULT_USER).strip()[:120]


#: Reachable without a signature. Both are liveness probes carrying no data: an
#: orchestrator has to call them before it can hold a key, and a deployment that
#: cannot answer them never starts.
UNSIGNED_PATHS: frozenset[str] = frozenset({"/health", "/ready"})


class SignedCallerOnly(BaseHTTPMiddleware):
    """Refuse anything the MCP server did not sign.

    This API has exactly one legitimate caller and no login of its own. Until
    now it also had no way to tell that caller apart from anything else that
    could open a socket to it, which made `X-Actor` an assertion rather than a
    fact. See `backend/internal_auth.py` for why this is a signature and not a
    bearer token.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if request.url.path in UNSIGNED_PATHS:
            return await call_next(request)

        settings = get_settings()
        presented = request.headers.get(internal_auth.SIGNATURE_HEADER, "")
        raw_timestamp = request.headers.get(internal_auth.TIMESTAMP_HEADER, "")
        actor = request.headers.get(internal_auth.ACTOR_HEADER, "")
        try:
            timestamp = int(raw_timestamp)
        except ValueError:
            return _unsigned()

        body = await request.body()
        message = internal_auth.canonical_request(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            actor=actor,
            body=body,
            timestamp=timestamp,
        )
        if not internal_auth.verify(
            settings.internal_api_keys,
            message,
            presented,
            timestamp=timestamp,
            now=time.time(),
            skew_seconds=settings.internal_request_skew_seconds,
        ):
            return _unsigned()
        return await call_next(request)


def _unsigned() -> JSONResponse:
    """One answer for every way a signature can be wrong.

    Saying which part failed tells an attacker whether they have the key, the
    clock or the canonical string wrong, and narrows the search for them.
    """
    return JSONResponse(
        status_code=401,
        content={
            "error": True,
            "code": str(ErrorCode.NOT_AUTHENTICATED),
            "message": "This API only answers requests signed by the MCP server.",
            "suggestion": (
                "Sign the request with internal_auth.sign_request, or use "
                "scripts/call_api.py. MCP clients reach the tools, never this API."
            ),
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("backend up in %s environment", settings.app_env)
    yield
    get_engine().dispose()


app = FastAPI(
    title="Dental clinic · domain API",
    description=(
        "Internal source of truth. The MCP server consumes this API; MCP clients "
        "never reach it directly."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(SignedCallerOnly)

SessionDep = Annotated[Session, Depends(get_session)]
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


@app.exception_handler(DataError)
async def handle_unusable_value(_: Request, exc: DataError) -> JSONResponse:
    """A value the database cannot hold is the caller's problem, not a fault.

    Two of these reached the caller as INTERNAL_ERROR, which reads as "the
    server broke" and invites a retry of the same doomed call: an id larger than
    PostgreSQL's four-byte integer, and a NUL byte inside a text field. Both are
    input, both are fixable by whoever sent them, and neither is worth a 500.

    Deliberately says nothing about the column or the type. Schema shape is free
    reconnaissance, and the caller does not need it to fix the call.
    """
    logger.info("unusable value mapped to invalid input", exc_info=exc)
    return JSONResponse(
        status_code=400,
        content={
            "error": True,
            "code": str(ErrorCode.INVALID_INPUT),
            "message": "One of the values sent cannot be stored as given.",
            "suggestion": (
                "Check the identifiers are ordinary positive integers and the text "
                "carries no control characters, then call again."
            ),
        },
    )


@app.exception_handler(StaleDataError)
@app.exception_handler(IntegrityError)
async def handle_lost_race(_: Request, exc: Exception) -> JSONResponse:
    """The net under every path that races, present and future.

    `backend/domain/services.py` maps these where it knows a slot is involved,
    which is where the message can name the slot and suggest another. This
    catches the ones nobody thought about: a race is a conflict the caller can
    act on, and answering it with a 500 tells an agent to retry the same call
    forever. Ten concurrent bookings produced six of those before this existed.
    """
    logger.info("lost race mapped to a conflict", exc_info=exc)
    return JSONResponse(
        status_code=409,
        content={
            "error": True,
            "code": str(ErrorCode.CONCURRENCY_CONFLICT),
            "message": "Another process changed the same record first.",
            "suggestion": (
                "Read the current state and decide again; do not repeat the call blindly."
            ),
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    """Unexpected failures are logged in full and answered with one opaque,
    still-structured error. A stack trace never reaches the caller."""
    logger.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "code": str(ErrorCode.INTERNAL_ERROR),
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
        with get_engine().connect() as connection:
            connection.execute(text("select 1"))
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
def clinic_info_route(session: SessionDep) -> ClinicInfo:
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
    responses=ERROR_RESPONSES,
)
def search_patients(
    session: SessionDep,
    document_number: Annotated[str | None, Query(max_length=20)] = None,
    name: Annotated[str | None, Query(max_length=160)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[PatientSummary]:
    found = services.search_patients(
        session, document_number=document_number, name=name, limit=limit
    )
    return [PatientSummary.of(p) for p in found]


@app.get(
    "/patients/{patient_id}/affiliation",
    tags=["read"],
    response_model=AffiliationResponse,
    responses=ERROR_RESPONSES,
)
def affiliation(session: SessionDep, patient_id: int) -> AffiliationResponse:
    return AffiliationResponse.of(
        patient_id, services.validate_patient_affiliation(session, patient_id)
    )


@app.get(
    "/patients/{patient_id}/cartera",
    tags=["read"],
    response_model=CarteraResponse,
    responses=ERROR_RESPONSES,
)
def cartera(session: SessionDep, patient_id: int) -> CarteraResponse:
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
    responses=ERROR_RESPONSES,
)
def patient_appointments_route(
    session: SessionDep,
    patient_id: int,
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AppointmentDetail]:
    appointments = services.list_patient_appointments(
        session, patient_id, since=since, until=until, limit=limit
    )
    return [AppointmentDetail.of(c, include_history=False) for c in appointments]


@app.get(
    "/availability",
    tags=["read"],
    response_model=list[FreeSlot],
    responses=ERROR_RESPONSES,
)
def slot_availability_route(
    session: SessionDep,
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
    responses=ERROR_RESPONSES,
)
def bookable_slot(
    session: SessionDep,
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
    "/appointments/by-key/{idempotency_key}",
    tags=["read"],
    response_model=AppointmentDetail,
    responses=ERROR_RESPONSES,
)
def appointment_by_key_route(session: SessionDep, idempotency_key: str) -> AppointmentDetail:
    """The appointment a booking key already produced, if any.

    Declared before `/appointments/{appointment_id}` on purpose: FastAPI matches
    in order, and `by-key` would otherwise be read as an appointment id.

    It exists so the MCP layer can tell a retry from a new request *before* it
    validates the slot. Without it, retrying a booking with the same key was
    refused with SLOT_UNAVAILABLE, because the slot the first call took is no
    longer free. The idempotency the backend has always implemented was
    unreachable through the only path an agent actually has.
    """
    appointment = session.scalar(
        select(Appointment).where(Appointment.idempotency_key == idempotency_key)
    )
    if appointment is None:
        raise AppointmentNotFound(
            f"No appointment was created with the key {idempotency_key}.",
            suggestion="The key is unused, so this is a first attempt, not a retry.",
            details={"idempotency_key": idempotency_key},
        )
    return AppointmentDetail.of(appointment)


@app.get(
    "/appointments/{appointment_id}",
    tags=["read"],
    response_model=AppointmentDetail,
    responses=ERROR_RESPONSES,
)
def appointment_detail_route(session: SessionDep, appointment_id: int) -> AppointmentDetail:
    return AppointmentDetail.of(services.get_appointment(session, appointment_id))


@app.get("/agenda/{day}", tags=["read"], response_model=DayAgenda)
def agenda_for_date(session: SessionDep, day: date) -> DayAgenda:
    appointments = services.agenda_for_day(session, day)
    by_status: dict[str, int] = {}
    for appointment in appointments:
        by_status[str(appointment.status)] = by_status.get(str(appointment.status), 0) + 1
    return DayAgenda(
        day=day,
        total=len(appointments),
        by_status=by_status,
        appointments=[AppointmentDetail.of(c, include_history=False) for c in appointments],
    )


@app.get("/waiting-list", tags=["read"], response_model=list[WaitingEntrySummary])
def waiting_list_route(
    session: SessionDep, specialty: Specialty | None = None
) -> list[WaitingEntrySummary]:
    rows = services.waiting_list_entries(session, specialty)
    order = {
        e.entry_id: i
        for i, e in enumerate(in_queue_order([services.to_queue_entry(f) for f in rows]))
    }
    rows.sort(key=lambda f: order.get(f.id, 10**6))
    return [WaitingEntrySummary.of(f) for f in rows]


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #


def _transition_message(result: services.TransitionResult) -> str:
    parts = [
        f"Appointment {result.appointment.id}: {result.effects.previous_status} → "
        f"{result.effects.new_status}."
    ]
    if result.effects.releases_slot:
        parts.append("The slot is free again in the agenda.")
    if result.created_charge is not None:
        parts.append(
            f"A charge of ${result.created_charge.amount:,.0f} COP was created "
            f"({result.created_charge.concept})."
        )
    if result.next_in_queue is not None:
        parts.append(
            "There is a patient on the waiting list for "
            f"{result.next_in_queue.specialty}: "
            f"{result.next_in_queue.patient.name}."
        )
    return " ".join(parts)


def _to_response(result: services.TransitionResult) -> TransitionResponse:
    return TransitionResponse(
        appointment=AppointmentDetail.of(result.appointment),
        previous_status=result.effects.previous_status,
        new_status=result.effects.new_status,
        freed_slot=result.effects.releases_slot,
        created_charge=result.effects.genera_cargo,
        charge=(
            ChargeSummary.of(result.created_charge) if result.created_charge is not None else None
        ),
        next_in_waiting_list=(
            WaitingEntrySummary.of(result.next_in_queue)
            if result.next_in_queue is not None
            else None
        ),
        message=_transition_message(result),
    )


@app.post("/appointments", tags=["write"], response_model=BookResponse, responses=ERROR_RESPONSES)
def book_route(session: SessionDep, actor: ActorDep, body: BookRequest) -> BookResponse:
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
        affiliation=AffiliationResponse.of(result.appointment.patient_id, result.affiliation),
        cartera_alert=result.cartera_alert,
        reused=result.reused,
    )


@app.post(
    "/appointments/{appointment_id}/confirm",
    tags=["write"],
    response_model=TransitionResponse,
    responses=ERROR_RESPONSES,
)
def confirm_route(session: SessionDep, actor: ActorDep, appointment_id: int) -> TransitionResponse:
    return _to_response(services.confirm_appointment(session, appointment_id, user=actor))


@app.post(
    "/appointments/{appointment_id}/cancel",
    tags=["write"],
    response_model=TransitionResponse,
    responses=ERROR_RESPONSES,
)
def cancel_route(
    session: SessionDep, actor: ActorDep, appointment_id: int, body: CancelRequest
) -> TransitionResponse:
    return _to_response(
        services.cancel_appointment(session, appointment_id, reason=body.reason, user=actor)
    )


@app.post(
    "/appointments/{appointment_id}/reschedule",
    tags=["write"],
    response_model=TransitionResponse,
    responses=ERROR_RESPONSES,
)
def reschedule_route(
    session: SessionDep, actor: ActorDep, appointment_id: int, body: RescheduleRequest
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
    responses=ERROR_RESPONSES,
)
def attendance_route(
    session: SessionDep, actor: ActorDep, appointment_id: int, body: AttendanceRequest
) -> TransitionResponse:
    return _to_response(
        services.record_attendance(session, appointment_id, body.status, user=actor)
    )


@app.post(
    "/waiting-list/offer",
    tags=["write"],
    response_model=SlotOfferResponse,
    responses=ERROR_RESPONSES,
)
def offer_slot_route(
    session: SessionDep, actor: ActorDep, body: OfferSlotRequest
) -> SlotOfferResponse:
    offer = services.offer_slot_to_waiting_list(session, body.slot_id, user=actor)
    start = f"{to_clinic_time(offer.slot.start):%Y-%m-%d %H:%M}"
    return SlotOfferResponse(
        entry_id=offer.entry.id,
        patient_id=offer.patient.id,
        patient=offer.patient.name,
        phone=offer.patient.phone,
        specialty=offer.entry.specialty,
        priority=offer.entry.priority,
        original_position=offer.original_position,
        slot_id=offer.slot.id,
        start_local=start,
        message=(
            f"Contact {offer.patient.name} ({offer.patient.phone}) to offer "
            f"them the {start} slot. They were number {offer.original_position} on "
            f"the {offer.entry.specialty} list."
        ),
    )


@app.post(
    "/waiting-list",
    tags=["write"],
    response_model=WaitingEntrySummary,
    responses=ERROR_RESPONSES,
)
def join_waiting_list_route(
    session: SessionDep, body: JoinWaitingListRequest
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
    responses=ERROR_RESPONSES,
)
def record_reason_route(
    session: SessionDep, actor: ActorDep, appointment_id: int, body: VisitReasonRequest
) -> AppointmentDetail:
    """Clinical data (Res. 2654/2019). Refused without recorded consent."""
    return AppointmentDetail.of(
        services.record_visit_reason(session, appointment_id, body.reason, user=actor)
    )
