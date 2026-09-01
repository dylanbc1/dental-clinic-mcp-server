"""Service layer: domain rules applied to persisted state.

Everything under `backend/domain/` up to here is pure. This module is where
that pure logic meets the database, and it is deliberately the *only* place
where an appointment changes state. Three invariants hold for every mutation:

1. The transition is validated by :mod:`backend.domain.states` first. There is
   no code path that writes ``cita.estado`` without going through it.
2. The audit row is written in the same unit of work as the change it records,
   so an audit gap is not possible.
3. Slot release, charge creation and waiting-list triggering are derived from
   :class:`EfectosTransicion`, never re-decided by the caller.

The MCP tool layer calls the REST API, which calls these functions. It never
reaches the ORM directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.exc import StaleDataError

from backend.domain.affiliation import AffiliationResult, validate_affiliation
from backend.domain.cartera import (
    DEFAULT_POLICY,
    CarteraPolicy,
    CarteraSummary,
    PendingCharge,
    booking_warning,
    charge_for_no_show,
    charge_for_visit,
    summarise_cartera,
)
from backend.domain.errors import (
    AlreadyOnWaitingList,
    AppointmentNotFound,
    ConcurrencyConflict,
    ConsentRequired,
    PatientAlreadyBooked,
    PatientNotFound,
    ProfessionalNotFound,
    SlotInThePast,
    SlotNotFound,
    SlotUnavailable,
    SpecialtyMismatch,
    WaitingListEmpty,
)
from backend.domain.states import TransitionEffects, validate_transition
from backend.domain.time import now_utc, to_clinic_time
from backend.domain.waiting_list import WaitingListEntry, next_in_queue
from backend.enums import (
    STATES_HOLDING_SLOT,
    AppointmentState,
    ChargeConcept,
    ChargeState,
    SlotState,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)
from backend.models import (
    AgendaSlot,
    Appointment,
    AppointmentHistory,
    Charge,
    Clinic,
    Patient,
    Professional,
    WaitingList,
)

#: How long a patient has to settle a charge before it counts as arrears.
PAYMENT_TERM = timedelta(days=30)

#: How many alternative slots an error offers when the requested one is taken.
SUGGESTED_ALTERNATIVES = 3


# --------------------------------------------------------------------------- #
# Read-side result objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    slot_id: int
    professional_id: int
    professional: str
    specialty: Specialty
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        """Human-facing label in clinic local time, which is what the model reads."""
        local = to_clinic_time(self.start)
        return f"{local:%Y-%m-%d %H:%M} ({self.professional})"


@dataclass(frozen=True, slots=True)
class BookingResult:
    appointment: Appointment
    affiliation: AffiliationResult
    cartera_alert: str | None
    #: True when an identical idempotency key had already created this
    #: appointment. The caller reports success, not a duplicate.
    reused: bool = False


@dataclass(frozen=True, slots=True)
class TransitionResult:
    appointment: Appointment
    effects: TransitionEffects
    created_charge: Charge | None = None
    next_in_queue: WaitingList | None = None


@dataclass(frozen=True, slots=True)
class SlotOffer:
    entry: WaitingList
    patient: Patient
    slot: AgendaSlot
    original_position: int
    #: True when the call returned an offer that already stood, rather than
    #: making a new one. Mirrors `BookingResult.reused`.
    reused: bool = False


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


def get_patient(session: Session, patient_id: int) -> Patient:
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise PatientNotFound(
            f"There is no patient with id {patient_id}.",
            suggestion="Look them up first with search_patients, by documento or by name.",
            details={"patient_id": patient_id},
        )
    return patient


def get_appointment(session: Session, appointment_id: int) -> Appointment:
    appointment = session.scalar(
        select(Appointment)
        .where(Appointment.id == appointment_id)
        .options(
            selectinload(Appointment.patient),
            selectinload(Appointment.professional),
            selectinload(Appointment.slot),
            selectinload(Appointment.history),
        )
    )
    if appointment is None:
        raise AppointmentNotFound(
            f"There is no appointment with id {appointment_id}.",
            suggestion="List the patient's appointments with list_patient_appointments.",
            details={"appointment_id": appointment_id},
        )
    return appointment


def get_professional(session: Session, professional_id: int) -> Professional:
    professional = session.get(Professional, professional_id)
    if professional is None:
        raise ProfessionalNotFound(
            f"There is no professional with id {professional_id}.",
            suggestion="See the list in the clinic://info resource.",
            details={"professional_id": professional_id},
        )
    return professional


def get_clinic(session: Session) -> Clinic:
    clinic = session.scalar(select(Clinic).order_by(Clinic.id).limit(1))
    if clinic is None:  # pragma: no cover - only on an unseeded database
        raise PatientNotFound(
            "The database has no clinic configured.",
            suggestion="Run `make seed` to load the synthetic data.",
        )
    return clinic


def search_patients(
    session: Session,
    *,
    document_number: str | None = None,
    name: str | None = None,
    limit: int = 10,
) -> list[Patient]:
    """Search by document (exact) or name (case-insensitive substring).

    Document match is exact on purpose: a partial document number is how you
    hand the wrong person's record to an agent.
    """
    query = select(Patient)
    if document_number:
        query = query.where(Patient.document_number == document_number.strip())
    elif name:
        patron = f"%{name.strip().lower()}%"
        query = query.where(func.lower(Patient.name).like(patron))
    else:
        raise PatientNotFound(
            "You must give a documento or a name to search by.",
            suggestion="Call search_patients with 'documento' or with 'nombre'.",
        )
    return list(session.scalars(query.order_by(Patient.name).limit(limit)))


def list_available_slots(
    session: Session,
    *,
    specialty: Specialty | None = None,
    day: date | None = None,
    professional_id: int | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> list[AvailableSlot]:
    """Free slots, never in the past, ordered chronologically."""
    reference = now or now_utc()
    query = (
        select(AgendaSlot, Professional)
        .join(Professional, AgendaSlot.professional_id == Professional.id)
        .where(
            AgendaSlot.status == SlotState.FREE,
            AgendaSlot.start > reference,
            Professional.active.is_(True),
        )
    )
    if specialty is not None:
        query = query.where(Professional.specialty == specialty)
    if day is not None:
        query = query.where(AgendaSlot.day == day)
    if professional_id is not None:
        get_professional(session, professional_id)
        query = query.where(AgendaSlot.professional_id == professional_id)

    rows = session.execute(query.order_by(AgendaSlot.start).limit(limit)).all()
    return [
        AvailableSlot(
            slot_id=slot.id,
            professional_id=professional.id,
            professional=professional.name,
            specialty=professional.specialty,
            start=slot.start,
            end=slot.end,
        )
        for slot, professional in rows
    ]


def list_patient_appointments(
    session: Session,
    patient_id: int,
    *,
    since: date | None = None,
    until: date | None = None,
    limit: int = 50,
) -> list[Appointment]:
    get_patient(session, patient_id)
    query = (
        select(Appointment)
        .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
        .where(Appointment.patient_id == patient_id)
        .options(selectinload(Appointment.slot), selectinload(Appointment.professional))
    )
    if since is not None:
        query = query.where(AgendaSlot.day >= since)
    if until is not None:
        query = query.where(AgendaSlot.day <= until)
    return list(session.scalars(query.order_by(AgendaSlot.start.desc()).limit(limit)))


def validate_patient_affiliation(session: Session, patient_id: int) -> AffiliationResult:
    patient = get_patient(session, patient_id)
    return validate_affiliation(
        patient.regimen,
        patient.affiliation_active,
        cuota_moderadora_level=patient.cuota_moderadora_level,
    )


def _pending_charges(session: Session, patient_id: int) -> list[PendingCharge]:
    rows = session.scalars(
        select(Charge).where(Charge.patient_id == patient_id, Charge.status == ChargeState.PENDING)
    )
    return [
        PendingCharge(
            charge_id=c.id,
            concept=c.concept,
            amount=c.amount,
            due_date=c.due_date,
            status=c.status,
        )
        for c in rows
    ]


def get_cartera(
    session: Session,
    patient_id: int,
    *,
    hoy: date | None = None,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CarteraSummary:
    get_patient(session, patient_id)
    return summarise_cartera(
        patient_id,
        _pending_charges(session, patient_id),
        hoy=hoy or to_clinic_time(now_utc()).date(),
        policy=policy,
    )


def agenda_for_day(session: Session, day: date) -> list[Appointment]:
    """Every appointment of a day, whatever its state. The front desk view."""
    return list(
        session.scalars(
            select(Appointment)
            .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
            .where(AgendaSlot.day == day)
            .options(
                selectinload(Appointment.slot),
                selectinload(Appointment.patient),
                selectinload(Appointment.professional),
            )
            .order_by(AgendaSlot.start)
        )
    )


def waiting_list_entries(session: Session, specialty: Specialty | None = None) -> list[WaitingList]:
    query = select(WaitingList).where(WaitingList.status == WaitingListState.ACTIVE)
    if specialty is not None:
        query = query.where(WaitingList.specialty == specialty)
    return list(session.scalars(query.options(selectinload(WaitingList.patient))))


# --------------------------------------------------------------------------- #
# Write side
# --------------------------------------------------------------------------- #


def _audit(
    session: Session,
    appointment: Appointment,
    *,
    previous_status: AppointmentState | None,
    new_status: AppointmentState,
    user: str,
    reason: str | None = None,
) -> AppointmentHistory:
    """Append one audit row. Same unit of work as the change it describes."""
    license_number = AppointmentHistory(
        appointment_id=appointment.id,
        previous_status=previous_status,
        new_status=new_status,
        user=user,
        reason=reason,
        occurred_at=now_utc(),
    )
    session.add(license_number)
    return license_number


def bookable_slot(session: Session, slot_id: int, *, now: datetime | None = None) -> AgendaSlot:
    """The slot, if it can still be booked. Raises the structured error if not.

    Shared by the booking path and by the read endpoint the tool layer calls
    before proposing, so both refuse for exactly the same reasons.
    """
    now = now or now_utc()
    slot = session.get(AgendaSlot, slot_id)
    if slot is None:
        raise SlotNotFound(
            f"There is no slot with id {slot_id}.",
            suggestion="Check current slots with check_availability.",
            details={"slot_id": slot_id},
        )
    if slot.start <= now:
        raise SlotInThePast(
            f"The slot at {to_clinic_time(slot.start):%Y-%m-%d %H:%M} is in the past.",
            suggestion="Ask for future availability with check_availability.",
            details={"slot_id": slot_id, "start": slot.start.isoformat()},
        )
    if slot.status is not SlotState.FREE:
        raise _slot_is_taken(session, slot, now=now)
    return slot


def _slot_is_taken(session: Session, slot: AgendaSlot, *, now: datetime) -> SlotUnavailable:
    """The refusal for a slot somebody else holds, with somewhere else to go.

    Raised from two places that are the same situation seen at two moments: the
    check before booking, and losing the race at flush time. Answering the racer
    with a bare conflict made one fact have two shapes, and only one of them
    carried the alternatives that let an agent recover on its own turn. Timing
    is the caller's problem to survive, not to interpret.
    """
    alternatives = list_available_slots(
        session,
        specialty=slot.professional.specialty,
        limit=SUGGESTED_ALTERNATIVES,
        now=now,
    )
    return SlotUnavailable(
        f"The slot at {to_clinic_time(slot.start):%Y-%m-%d %H:%M} is no longer free.",
        suggestion=(
            "The closest free slots are: " + ", ".join(a.label for a in alternatives) + "."
            if alternatives
            else "There are no free slots coming up for that specialty."
        ),
        details={
            "slot_id": slot.id,
            "alternatives": [
                {"slot_id": a.slot_id, "start": a.start.isoformat()} for a in alternatives
            ],
        },
    )


def validate_booking(
    session: Session,
    slot_id: int,
    *,
    patient_id: int | None = None,
    expected_specialty: Specialty | None = None,
    exclude_appointment_id: int | None = None,
    now: datetime | None = None,
) -> AgendaSlot:
    """Everything that must hold for a booking to succeed, in one place.

    Called twice: once by the tool layer before proposing, so a human is never
    asked to approve an operation that cannot work, and once by `book_appointment`
    at the moment of effect, because the state can change in between. Sharing
    the function is what guarantees both refuse for the same reasons.
    """
    reference = now or now_utc()
    slot = bookable_slot(session, slot_id, now=reference)

    if expected_specialty is not None and slot.professional.specialty != expected_specialty:
        raise SpecialtyMismatch(
            f"The slot is for {slot.professional.specialty}, not {expected_specialty}.",
            suggestion=(
                f"Ask for availability with especialidad='{expected_specialty}' and book again."
            ),
            details={
                "slot_specialty": str(slot.professional.specialty),
                "requested_specialty": str(expected_specialty),
            },
        )

    if patient_id is not None:
        query = (
            select(Appointment)
            .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.status.in_(STATES_HOLDING_SLOT),
                AgendaSlot.start < slot.end,
                AgendaSlot.end > slot.start,
            )
        )
        if exclude_appointment_id is not None:
            query = query.where(Appointment.id != exclude_appointment_id)
        overlapping = session.scalar(query)
        if overlapping is not None:
            raise PatientAlreadyBooked(
                "The patient already has an appointment that overlaps that hour.",
                suggestion=(
                    f"Cancel or reschedule appointment {overlapping.id} before booking "
                    "another one at the same time."
                ),
                details={"existing_appointment_id": overlapping.id},
            )

    return slot


def _flush_or_conflict(
    session: Session, *, slot_id: int, slot: AgendaSlot | None = None, now: datetime | None = None
) -> None:
    """Flush a slot mutation, turning a lost race into a typed conflict.

    Two different exceptions mean the same thing here and only one of them used
    to be caught. `IntegrityError` is the partial unique index refusing a second
    live appointment on the slot; `StaleDataError` is the optimistic
    `version_id` on `agenda_slot` finding the row already moved. Ten concurrent
    bookings over HTTP produced one success, two clean SLOT_UNAVAILABLE and six
    `500`s, because the second exception escaped as an unhandled error. A
    project whose first promise is "never a mute 500" cannot answer a race that
    way.

    The caller's transaction scope performs the rollback.
    """
    try:
        session.flush()
    except (IntegrityError, StaleDataError) as exc:
        if slot is not None:
            # The same answer a caller who arrived a second later would get,
            # alternatives included. `session.rollback()` first: the failed
            # flush leaves the session unusable for the query behind it.
            session.rollback()
            fresh = session.get(AgendaSlot, slot_id)
            if fresh is not None:
                raise _slot_is_taken(session, fresh, now=now or now_utc()) from exc
        raise ConcurrencyConflict(
            "Another process took that slot while the appointment was being created.",
            suggestion="Check availability again and book a different slot.",
            details={"slot_id": slot_id},
        ) from exc


def book_appointment(
    session: Session,
    *,
    patient_id: int,
    slot_id: int,
    user: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
    expected_specialty: Specialty | None = None,
) -> BookingResult:
    """Book a free slot for a patient.

    Outstanding debt produces a *warning*, never a refusal: clinics do not turn
    patients away over an unpaid copayment (§2.3).
    """
    reference = now or now_utc()

    if idempotency_key:
        existing = session.scalar(
            select(Appointment).where(Appointment.idempotency_key == idempotency_key)
        )
        if existing is not None:
            # The retry of a call that already succeeded is a success.
            return BookingResult(
                appointment=existing,
                affiliation=validate_patient_affiliation(session, existing.patient_id),
                cartera_alert=None,
                reused=True,
            )

    patient = get_patient(session, patient_id)
    slot = validate_booking(
        session,
        slot_id,
        patient_id=patient_id,
        expected_specialty=expected_specialty,
        now=reference,
    )

    affiliation = validate_affiliation(
        patient.regimen,
        patient.affiliation_active,
        cuota_moderadora_level=patient.cuota_moderadora_level,
    )
    warning = booking_warning(get_cartera(session, patient_id))

    appointment = Appointment(
        patient_id=patient_id,
        professional_id=slot.professional_id,
        slot_id=slot.id,
        status=AppointmentState.SCHEDULED,
        created_by=user,
        idempotency_key=idempotency_key,
    )
    slot.status = SlotState.BUSY
    session.add(appointment)

    _flush_or_conflict(session, slot_id=slot_id, slot=slot, now=reference)

    _audit(
        session,
        appointment,
        previous_status=None,
        new_status=AppointmentState.SCHEDULED,
        user=user,
    )
    session.flush()
    return BookingResult(appointment=appointment, affiliation=affiliation, cartera_alert=warning)


def _create_charge(
    session: Session,
    appointment: Appointment,
    *,
    concept: ChargeConcept,
    amount: Decimal,
    description: str,
    hoy: date,
) -> Charge:
    charge = Charge(
        patient_id=appointment.patient_id,
        appointment_id=appointment.id,
        concept=concept,
        amount=amount,
        description=description,
        status=ChargeState.PENDING,
        due_date=hoy + PAYMENT_TERM,
    )
    session.add(charge)
    return charge


def change_state(
    session: Session,
    appointment_id: int,
    new_state: AppointmentState,
    *,
    user: str,
    reason: str | None = None,
    policy: CarteraPolicy = DEFAULT_POLICY,
    hoy: date | None = None,
) -> TransitionResult:
    """The single entry point for every appointment state change.

    Validates, audits, and applies the derived effects: releasing the slot,
    creating the charge, surfacing the next patient on the waiting list.
    """
    appointment = get_appointment(session, appointment_id)
    previous = appointment.status
    effects = validate_transition(previous, new_state, reason=reason)

    appointment.status = new_state
    if new_state is AppointmentState.CANCELLED:
        appointment.cancellation_reason = reason

    if effects.releases_slot:
        appointment.slot.status = SlotState.FREE

    _audit(
        session,
        appointment,
        previous_status=previous,
        new_status=new_state,
        user=user,
        reason=reason,
    )

    charge: Charge | None = None
    if effects.genera_cargo:
        charge_date = hoy or to_clinic_time(appointment.slot.start).date()
        calculated = None
        if new_state is AppointmentState.ATTENDED:
            affiliation = validate_affiliation(
                appointment.patient.regimen,
                appointment.patient.affiliation_active,
                cuota_moderadora_level=appointment.patient.cuota_moderadora_level,
            )
            calculated = charge_for_visit(
                affiliation,
                str(appointment.professional.specialty),
                cuota_moderadora_level=appointment.patient.cuota_moderadora_level,
            )
        else:  # EstadoCita.NO_ASISTIO
            calculated = charge_for_no_show(
                was_confirmed=previous is AppointmentState.CONFIRMED, policy=policy
            )
        if calculated is not None:
            charge = _create_charge(
                session,
                appointment,
                concept=calculated.concept,
                amount=calculated.amount,
                description=calculated.description,
                hoy=charge_date,
            )

    next_up: WaitingList | None = None
    if effects.triggers_waiting_list:
        next_up = _next_candidate(
            session, appointment.professional.specialty, exclude=appointment.patient_id
        )

    # A transition can free or hold the slot, so it moves the versioned row too.
    _flush_or_conflict(session, slot_id=appointment.slot_id)
    return TransitionResult(
        appointment=appointment, effects=effects, created_charge=charge, next_in_queue=next_up
    )


def _next_candidate(
    session: Session, specialty: Specialty, *, exclude: int | None = None
) -> WaitingList | None:
    """Peek at the head of the waiting list. Returns ``None`` when empty.

    Peeking must not fail: a cancellation with nobody waiting is a perfectly
    normal outcome, not an error the caller has to handle.
    """
    entries = waiting_list_entries(session, specialty)
    if not entries:
        return None
    as_entries = [to_queue_entry(e) for e in entries]
    try:
        chosen = next_in_queue(
            as_entries,
            specialty,
            exclude_patients=frozenset({exclude}) if exclude is not None else frozenset(),
        )
    except WaitingListEmpty:
        return None
    return next(e for e in entries if e.id == chosen.entry_id)


def to_queue_entry(row: WaitingList) -> WaitingListEntry:
    """Adapt a persisted row to the pure ordering type of `lista_espera`."""
    return WaitingListEntry(
        entry_id=row.id,
        patient_id=row.patient_id,
        specialty=row.specialty,
        priority=row.priority,
        created_at=row.created_at,
        status=row.status,
    )


def confirm_appointment(session: Session, appointment_id: int, *, user: str) -> TransitionResult:
    return change_state(session, appointment_id, AppointmentState.CONFIRMED, user=user)


def cancel_appointment(
    session: Session, appointment_id: int, *, reason: str, user: str
) -> TransitionResult:
    return change_state(
        session, appointment_id, AppointmentState.CANCELLED, user=user, reason=reason
    )


def record_attendance(
    session: Session, appointment_id: int, status: AppointmentState, *, user: str
) -> TransitionResult:
    return change_state(session, appointment_id, status, user=user)


def reschedule_appointment(
    session: Session,
    appointment_id: int,
    new_slot_id: int,
    *,
    user: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> TransitionResult:
    """Move an appointment to another slot.

    Two effects in one operation, which is exactly why it needs a human gate:
    the old slot is freed and a new one is taken. The new appointment keeps a
    pointer back to the original so the chain stays auditable.
    """
    reference = now or now_utc()
    original = get_appointment(session, appointment_id)
    new_slot = validate_booking(
        session,
        new_slot_id,
        patient_id=original.patient_id,
        exclude_appointment_id=original.id,
        now=reference,
    )

    result = change_state(
        session,
        appointment_id,
        AppointmentState.RESCHEDULED,
        user=user,
        reason=reason or "Reschedule requested",
    )

    replacement = Appointment(
        patient_id=original.patient_id,
        professional_id=new_slot.professional_id,
        slot_id=new_slot.id,
        status=AppointmentState.SCHEDULED,
        created_by=user,
        source_appointment_id=original.id,
    )
    new_slot.status = SlotState.BUSY
    session.add(replacement)
    # Same race as booking: rescheduling takes a slot, so it can lose it.
    _flush_or_conflict(session, slot_id=new_slot.id)

    _audit(
        session,
        replacement,
        previous_status=None,
        new_status=AppointmentState.SCHEDULED,
        user=user,
        reason=f"Rescheduled from appointment {original.id}",
    )
    session.flush()
    return TransitionResult(
        appointment=replacement, effects=result.effects, next_in_queue=result.next_in_queue
    )


def offer_slot_to_waiting_list(
    session: Session, slot_id: int, *, user: str, now: datetime | None = None
) -> SlotOffer:
    """Offer a freed slot to the next patient in the queue.

    This does not book anything: it records the offer and returns who to
    contact. Booking is a separate, separately-approved decision.
    """
    reference = now or now_utc()
    slot = bookable_slot(session, slot_id, now=reference)
    specialty = slot.professional.specialty

    standing = session.scalar(
        select(WaitingList).where(
            WaitingList.offered_slot_id == slot.id,
            WaitingList.status == WaitingListState.OFFERED,
        )
    )
    if standing is not None:
        # A slot can only be promised to one person. Offering it twice was
        # possible and observable: replaying an approved confirmation offered
        # the same freed slot to a second patient, so two people were told to
        # come in for one appointment. Repeating the call now returns the
        # standing offer, which is what the domain meant all along and is also
        # what makes the operation safe to retry.
        return SlotOffer(
            entry=standing,
            patient=standing.patient,
            slot=slot,
            original_position=1,
            reused=True,
        )

    entries = waiting_list_entries(session, specialty)
    as_entries = [to_queue_entry(e) for e in entries]
    chosen = next_in_queue(as_entries, specialty)
    row = next(e for e in entries if e.id == chosen.entry_id)
    position = [d.entry_id for d in sorted(as_entries, key=lambda d: d.sort_key)].index(
        chosen.entry_id
    ) + 1

    row.status = WaitingListState.OFFERED
    row.offered_at = reference
    row.offered_slot_id = slot.id
    session.flush()

    return SlotOffer(
        entry=row,
        patient=row.patient,
        slot=slot,
        original_position=position,
    )


def join_waiting_list(
    session: Session,
    *,
    patient_id: int,
    specialty: Specialty,
    priority: WaitingListPriority = WaitingListPriority.SENIORITY,
    notes: str | None = None,
) -> WaitingList:
    get_patient(session, patient_id)
    existing = session.scalar(
        select(WaitingList).where(
            WaitingList.patient_id == patient_id,
            WaitingList.specialty == specialty,
            WaitingList.status == WaitingListState.ACTIVE,
        )
    )
    if existing is not None:
        raise AlreadyOnWaitingList(
            f"The patient is already on the waiting list for {specialty}.",
            suggestion="Check their current position before enrolling them again.",
            details={"entry_id": existing.id},
        )
    entry = WaitingList(
        patient_id=patient_id,
        specialty=specialty,
        priority=priority,
        status=WaitingListState.ACTIVE,
        notes=notes,
    )
    session.add(entry)
    session.flush()
    return entry


def record_visit_reason(
    session: Session, appointment_id: int, reason: str, *, user: str
) -> Appointment:
    """Attach a reason for consultation to an appointment. **Clinical data.**

    The one operation that crosses from administrative into clinical territory
    (Res. 2654/2019), so it refuses without recorded informed consent whatever
    the caller's scope. The scope check is necessary but not sufficient.
    """
    appointment = get_appointment(session, appointment_id)
    patient = appointment.patient

    if not patient.clinical_data_consent:
        raise ConsentRequired(
            (
                f"Patient {patient.name} has no informed consent on file for the "
                "handling of clinical data."
            ),
            suggestion=(
                "Obtain and record informed consent before writing the reason for "
                "consultation. This is a requirement of Resolución 2654/2019, not a "
                "validation of this system."
            ),
            details={"patient_id": patient.id, "appointment_id": appointment_id},
        )

    appointment.reason = reason.strip()
    appointment.reason_recorded_at = now_utc()
    appointment.reason_recorded_by = user

    # Clinical writes are audited even though no state changed: the regulation
    # cares about who touched clinical data, not about the state machine.
    _audit(
        session,
        appointment,
        previous_status=appointment.status,
        new_status=appointment.status,
        user=user,
        reason="Reason for consultation recorded (clinical data)",
    )
    session.flush()
    return appointment
