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

from backend.domain.afiliacion import AfiliacionResult, validate_afiliacion
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
    ConceptoCargo,
    Especialidad,
    EstadoCargo,
    EstadoCita,
    EstadoListaEspera,
    EstadoSlot,
    PrioridadListaEspera,
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
    profesional_id: int
    profesional: str
    especialidad: Especialidad
    inicio: datetime
    fin: datetime

    @property
    def label(self) -> str:
        """Human-facing label in clinic local time, which is what the model reads."""
        local = to_clinic_time(self.inicio)
        return f"{local:%Y-%m-%d %H:%M} ({self.profesional})"


@dataclass(frozen=True, slots=True)
class BookingResult:
    cita: Appointment
    afiliacion: AfiliacionResult
    alerta_cartera: str | None
    #: True when an identical idempotency key had already created this
    #: appointment. The caller reports success, not a duplicate.
    reutilizada: bool = False


@dataclass(frozen=True, slots=True)
class TransitionResult:
    cita: Appointment
    effects: TransitionEffects
    created_charge: Charge | None = None
    siguiente_en_espera: WaitingList | None = None


@dataclass(frozen=True, slots=True)
class SlotOffer:
    entry: WaitingList
    paciente: Patient
    slot: AgendaSlot
    posicion_original: int


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


def get_patient(session: Session, paciente_id: int) -> Patient:
    paciente = session.get(Patient, paciente_id)
    if paciente is None:
        raise PatientNotFound(
            f"There is no patient with id {paciente_id}.",
            sugerencia="Look them up first with buscar_paciente, by documento or by name.",
            detalles={"paciente_id": paciente_id},
        )
    return paciente


def get_appointment(session: Session, cita_id: int) -> Appointment:
    cita = session.scalar(
        select(Appointment)
        .where(Appointment.id == cita_id)
        .options(
            selectinload(Appointment.paciente),
            selectinload(Appointment.profesional),
            selectinload(Appointment.slot),
            selectinload(Appointment.historial),
        )
    )
    if cita is None:
        raise AppointmentNotFound(
            f"There is no appointment with id {cita_id}.",
            sugerencia="List the patient's appointments with listar_citas_paciente.",
            detalles={"cita_id": cita_id},
        )
    return cita


def get_professional(session: Session, profesional_id: int) -> Professional:
    profesional = session.get(Professional, profesional_id)
    if profesional is None:
        raise ProfessionalNotFound(
            f"There is no professional with id {profesional_id}.",
            sugerencia="See the list in the clinica://info resource.",
            detalles={"profesional_id": profesional_id},
        )
    return profesional


def get_clinic(session: Session) -> Clinic:
    clinica = session.scalar(select(Clinic).order_by(Clinic.id).limit(1))
    if clinica is None:  # pragma: no cover - only on an unseeded database
        raise PatientNotFound(
            "The database has no clinic configured.",
            sugerencia="Run `make seed` to load the synthetic data.",
        )
    return clinica


def search_patients(
    session: Session,
    *,
    documento: str | None = None,
    nombre: str | None = None,
    limite: int = 10,
) -> list[Patient]:
    """Search by document (exact) or name (case-insensitive substring).

    Document match is exact on purpose: a partial document number is how you
    hand the wrong person's record to an agent.
    """
    query = select(Patient)
    if documento:
        query = query.where(Patient.documento == documento.strip())
    elif nombre:
        patron = f"%{nombre.strip().lower()}%"
        query = query.where(func.lower(Patient.nombre).like(patron))
    else:
        raise PatientNotFound(
            "You must give a documento or a name to search by.",
            sugerencia="Call buscar_paciente with 'documento' or with 'nombre'.",
        )
    return list(session.scalars(query.order_by(Patient.nombre).limit(limite)))


def list_available_slots(
    session: Session,
    *,
    especialidad: Especialidad | None = None,
    fecha: date | None = None,
    profesional_id: int | None = None,
    limite: int = 20,
    now: datetime | None = None,
) -> list[AvailableSlot]:
    """Free slots, never in the past, ordered chronologically."""
    reference = now or now_utc()
    query = (
        select(AgendaSlot, Professional)
        .join(Professional, AgendaSlot.profesional_id == Professional.id)
        .where(
            AgendaSlot.estado == EstadoSlot.LIBRE,
            AgendaSlot.inicio > reference,
            Professional.activo.is_(True),
        )
    )
    if especialidad is not None:
        query = query.where(Professional.especialidad == especialidad)
    if fecha is not None:
        query = query.where(AgendaSlot.fecha == fecha)
    if profesional_id is not None:
        get_professional(session, profesional_id)
        query = query.where(AgendaSlot.profesional_id == profesional_id)

    filas = session.execute(query.order_by(AgendaSlot.inicio).limit(limite)).all()
    return [
        AvailableSlot(
            slot_id=slot.id,
            profesional_id=profesional.id,
            profesional=profesional.nombre,
            especialidad=profesional.especialidad,
            inicio=slot.inicio,
            fin=slot.fin,
        )
        for slot, profesional in filas
    ]


def list_patient_appointments(
    session: Session,
    paciente_id: int,
    *,
    desde: date | None = None,
    hasta: date | None = None,
    limite: int = 50,
) -> list[Appointment]:
    get_patient(session, paciente_id)
    query = (
        select(Appointment)
        .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
        .where(Appointment.paciente_id == paciente_id)
        .options(selectinload(Appointment.slot), selectinload(Appointment.profesional))
    )
    if desde is not None:
        query = query.where(AgendaSlot.fecha >= desde)
    if hasta is not None:
        query = query.where(AgendaSlot.fecha <= hasta)
    return list(session.scalars(query.order_by(AgendaSlot.inicio.desc()).limit(limite)))


def validate_patient_afiliacion(session: Session, paciente_id: int) -> AfiliacionResult:
    paciente = get_patient(session, paciente_id)
    return validate_afiliacion(
        paciente.regimen,
        paciente.afiliacion_activa,
        nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
    )


def _pending_charges(session: Session, paciente_id: int) -> list[PendingCharge]:
    filas = session.scalars(
        select(Charge).where(
            Charge.paciente_id == paciente_id, Charge.estado == EstadoCargo.PENDIENTE
        )
    )
    return [
        PendingCharge(
            cargo_id=c.id,
            concepto=c.concepto,
            monto=c.monto,
            vencimiento=c.vencimiento,
            estado=c.estado,
        )
        for c in filas
    ]


def get_cartera(
    session: Session,
    paciente_id: int,
    *,
    hoy: date | None = None,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CarteraSummary:
    get_patient(session, paciente_id)
    return summarise_cartera(
        paciente_id,
        _pending_charges(session, paciente_id),
        hoy=hoy or to_clinic_time(now_utc()).date(),
        policy=policy,
    )


def agenda_for_day(session: Session, fecha: date) -> list[Appointment]:
    """Every appointment of a day, whatever its state. The front desk view."""
    return list(
        session.scalars(
            select(Appointment)
            .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
            .where(AgendaSlot.fecha == fecha)
            .options(
                selectinload(Appointment.slot),
                selectinload(Appointment.paciente),
                selectinload(Appointment.profesional),
            )
            .order_by(AgendaSlot.inicio)
        )
    )


def waiting_list_entries(
    session: Session, especialidad: Especialidad | None = None
) -> list[WaitingList]:
    query = select(WaitingList).where(WaitingList.estado == EstadoListaEspera.ACTIVA)
    if especialidad is not None:
        query = query.where(WaitingList.especialidad == especialidad)
    return list(session.scalars(query.options(selectinload(WaitingList.paciente))))


# --------------------------------------------------------------------------- #
# Write side
# --------------------------------------------------------------------------- #


def _audit(
    session: Session,
    cita: Appointment,
    *,
    estado_anterior: EstadoCita | None,
    estado_nuevo: EstadoCita,
    usuario: str,
    motivo: str | None = None,
) -> AppointmentHistory:
    """Append one audit row. Same unit of work as the change it describes."""
    registro = AppointmentHistory(
        cita_id=cita.id,
        estado_anterior=estado_anterior,
        estado_nuevo=estado_nuevo,
        usuario=usuario,
        motivo=motivo,
        momento=now_utc(),
    )
    session.add(registro)
    return registro


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
            sugerencia="Check current slots with consultar_disponibilidad.",
            detalles={"slot_id": slot_id},
        )
    if slot.inicio <= now:
        raise SlotInThePast(
            f"The slot at {to_clinic_time(slot.inicio):%Y-%m-%d %H:%M} is in the past.",
            sugerencia="Ask for future availability with consultar_disponibilidad.",
            detalles={"slot_id": slot_id, "inicio": slot.inicio.isoformat()},
        )
    if slot.estado is not EstadoSlot.LIBRE:
        alternativas = list_available_slots(
            session,
            especialidad=slot.profesional.especialidad,
            limite=SUGGESTED_ALTERNATIVES,
            now=now,
        )
        raise SlotUnavailable(
            f"The slot at {to_clinic_time(slot.inicio):%Y-%m-%d %H:%M} is no longer free.",
            sugerencia=(
                "The closest free slots are: " + ", ".join(a.label for a in alternativas) + "."
                if alternativas
                else "There are no free slots coming up for that specialty."
            ),
            detalles={
                "slot_id": slot_id,
                "alternativas": [
                    {"slot_id": a.slot_id, "inicio": a.inicio.isoformat()} for a in alternativas
                ],
            },
        )
    return slot


def validate_booking(
    session: Session,
    slot_id: int,
    *,
    paciente_id: int | None = None,
    especialidad_esperada: Especialidad | None = None,
    excluir_cita_id: int | None = None,
    now: datetime | None = None,
) -> AgendaSlot:
    """Everything that must hold for a booking to succeed, in one place.

    Called twice: once by the tool layer before proposing, so a human is never
    asked to approve an operation that cannot work, and once by `agendar_cita`
    at the moment of effect, because the state can change in between. Sharing
    the function is what guarantees both refuse for the same reasons.
    """
    reference = now or now_utc()
    slot = bookable_slot(session, slot_id, now=reference)

    if especialidad_esperada is not None and slot.profesional.especialidad != especialidad_esperada:
        raise SpecialtyMismatch(
            f"The slot is for {slot.profesional.especialidad}, not {especialidad_esperada}.",
            sugerencia=(
                f"Ask for availability with especialidad='{especialidad_esperada}' and book again."
            ),
            detalles={
                "especialidad_del_cupo": str(slot.profesional.especialidad),
                "especialidad_pedida": str(especialidad_esperada),
            },
        )

    if paciente_id is not None:
        query = (
            select(Appointment)
            .join(AgendaSlot, Appointment.slot_id == AgendaSlot.id)
            .where(
                Appointment.paciente_id == paciente_id,
                Appointment.estado.in_(STATES_HOLDING_SLOT),
                AgendaSlot.inicio < slot.fin,
                AgendaSlot.fin > slot.inicio,
            )
        )
        if excluir_cita_id is not None:
            query = query.where(Appointment.id != excluir_cita_id)
        overlapping = session.scalar(query)
        if overlapping is not None:
            raise PatientAlreadyBooked(
                "The patient already has an appointment that overlaps that hour.",
                sugerencia=(
                    f"Cancel or reschedule appointment {overlapping.id} before booking "
                    "another one at the same time."
                ),
                detalles={"cita_existente_id": overlapping.id},
            )

    return slot


def book_appointment(
    session: Session,
    *,
    paciente_id: int,
    slot_id: int,
    usuario: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
    especialidad_esperada: Especialidad | None = None,
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
                cita=existing,
                afiliacion=validate_patient_afiliacion(session, existing.paciente_id),
                alerta_cartera=None,
                reutilizada=True,
            )

    paciente = get_patient(session, paciente_id)
    slot = validate_booking(
        session,
        slot_id,
        paciente_id=paciente_id,
        especialidad_esperada=especialidad_esperada,
        now=reference,
    )

    afiliacion = validate_afiliacion(
        paciente.regimen,
        paciente.afiliacion_activa,
        nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
    )
    alerta = booking_warning(get_cartera(session, paciente_id))

    cita = Appointment(
        paciente_id=paciente_id,
        profesional_id=slot.profesional_id,
        slot_id=slot.id,
        estado=EstadoCita.AGENDADA,
        creada_por=usuario,
        idempotency_key=idempotency_key,
    )
    slot.estado = EstadoSlot.OCUPADO
    session.add(cita)

    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the race against another agent between the check and the write.
        # The database is what turns that into a conflict rather than a
        # duplicate. The caller's transaction scope performs the rollback.
        raise ConcurrencyConflict(
            "Another process took that slot while the appointment was being created.",
            sugerencia="Check availability again and book a different slot.",
            detalles={"slot_id": slot_id},
        ) from exc

    _audit(
        session,
        cita,
        estado_anterior=None,
        estado_nuevo=EstadoCita.AGENDADA,
        usuario=usuario,
    )
    session.flush()
    return BookingResult(cita=cita, afiliacion=afiliacion, alerta_cartera=alerta)


def _create_charge(
    session: Session,
    cita: Appointment,
    *,
    concepto: ConceptoCargo,
    monto: Decimal,
    descripcion: str,
    hoy: date,
) -> Charge:
    cargo = Charge(
        paciente_id=cita.paciente_id,
        cita_id=cita.id,
        concepto=concepto,
        monto=monto,
        descripcion=descripcion,
        estado=EstadoCargo.PENDIENTE,
        vencimiento=hoy + PAYMENT_TERM,
    )
    session.add(cargo)
    return cargo


def change_state(
    session: Session,
    cita_id: int,
    nuevo_estado: EstadoCita,
    *,
    usuario: str,
    motivo: str | None = None,
    policy: CarteraPolicy = DEFAULT_POLICY,
    hoy: date | None = None,
) -> TransitionResult:
    """The single entry point for every appointment state change.

    Validates, audits, and applies the derived effects: releasing the slot,
    creating the charge, surfacing the next patient on the waiting list.
    """
    cita = get_appointment(session, cita_id)
    previous = cita.estado
    effects = validate_transition(previous, nuevo_estado, motivo=motivo)

    cita.estado = nuevo_estado
    if nuevo_estado is EstadoCita.CANCELADA:
        cita.motivo_cancelacion = motivo

    if effects.libera_slot:
        cita.slot.estado = EstadoSlot.LIBRE

    _audit(
        session,
        cita,
        estado_anterior=previous,
        estado_nuevo=nuevo_estado,
        usuario=usuario,
        motivo=motivo,
    )

    cargo: Charge | None = None
    if effects.genera_cargo:
        fecha_cargo = hoy or to_clinic_time(cita.slot.inicio).date()
        calculated = None
        if nuevo_estado is EstadoCita.ATENDIDA:
            afiliacion = validate_afiliacion(
                cita.paciente.regimen,
                cita.paciente.afiliacion_activa,
                nivel_cuota_moderadora=cita.paciente.nivel_cuota_moderadora,
            )
            calculated = charge_for_visit(
                afiliacion,
                str(cita.profesional.especialidad),
                nivel_cuota_moderadora=cita.paciente.nivel_cuota_moderadora,
            )
        else:  # EstadoCita.NO_ASISTIO
            calculated = charge_for_no_show(
                estaba_confirmada=previous is EstadoCita.CONFIRMADA, policy=policy
            )
        if calculated is not None:
            cargo = _create_charge(
                session,
                cita,
                concepto=calculated.concepto,
                monto=calculated.monto,
                descripcion=calculated.descripcion,
                hoy=fecha_cargo,
            )

    next_up: WaitingList | None = None
    if effects.dispara_lista_espera:
        next_up = _next_candidate(session, cita.profesional.especialidad, excluir=cita.paciente_id)

    session.flush()
    return TransitionResult(
        cita=cita, effects=effects, created_charge=cargo, siguiente_en_espera=next_up
    )


def _next_candidate(
    session: Session, especialidad: Especialidad, *, excluir: int | None = None
) -> WaitingList | None:
    """Peek at the head of the waiting list. Returns ``None`` when empty.

    Peeking must not fail: a cancellation with nobody waiting is a perfectly
    normal outcome, not an error the caller has to handle.
    """
    entries = waiting_list_entries(session, especialidad)
    if not entries:
        return None
    as_entries = [to_queue_entry(e) for e in entries]
    try:
        chosen = next_in_queue(
            as_entries,
            especialidad,
            excluir_pacientes=frozenset({excluir}) if excluir is not None else frozenset(),
        )
    except WaitingListEmpty:
        return None
    return next(e for e in entries if e.id == chosen.entrada_id)


def to_queue_entry(fila: WaitingList) -> WaitingListEntry:
    """Adapt a persisted row to the pure ordering type of `lista_espera`."""
    return WaitingListEntry(
        entrada_id=fila.id,
        paciente_id=fila.paciente_id,
        especialidad=fila.especialidad,
        prioridad=fila.prioridad,
        creada_en=fila.creada_en,
        estado=fila.estado,
    )


def confirm_appointment(session: Session, cita_id: int, *, usuario: str) -> TransitionResult:
    return change_state(session, cita_id, EstadoCita.CONFIRMADA, usuario=usuario)


def cancel_appointment(
    session: Session, cita_id: int, *, motivo: str, usuario: str
) -> TransitionResult:
    return change_state(session, cita_id, EstadoCita.CANCELADA, usuario=usuario, motivo=motivo)


def record_attendance(
    session: Session, cita_id: int, estado: EstadoCita, *, usuario: str
) -> TransitionResult:
    return change_state(session, cita_id, estado, usuario=usuario)


def reschedule_appointment(
    session: Session,
    cita_id: int,
    nuevo_slot_id: int,
    *,
    usuario: str,
    motivo: str | None = None,
    now: datetime | None = None,
) -> TransitionResult:
    """Move an appointment to another slot.

    Two effects in one operation, which is exactly why it needs a human gate:
    the old slot is freed and a new one is taken. The new appointment keeps a
    pointer back to the original so the chain stays auditable.
    """
    reference = now or now_utc()
    original = get_appointment(session, cita_id)
    nuevo_slot = validate_booking(
        session,
        nuevo_slot_id,
        paciente_id=original.paciente_id,
        excluir_cita_id=original.id,
        now=reference,
    )

    result = change_state(
        session,
        cita_id,
        EstadoCita.REPROGRAMADA,
        usuario=usuario,
        motivo=motivo or "Reschedule requested",
    )

    nueva = Appointment(
        paciente_id=original.paciente_id,
        profesional_id=nuevo_slot.profesional_id,
        slot_id=nuevo_slot.id,
        estado=EstadoCita.AGENDADA,
        creada_por=usuario,
        cita_origen_id=original.id,
    )
    nuevo_slot.estado = EstadoSlot.OCUPADO
    session.add(nueva)
    session.flush()

    _audit(
        session,
        nueva,
        estado_anterior=None,
        estado_nuevo=EstadoCita.AGENDADA,
        usuario=usuario,
        motivo=f"Rescheduled from appointment {original.id}",
    )
    session.flush()
    return TransitionResult(
        cita=nueva, effects=result.effects, siguiente_en_espera=result.siguiente_en_espera
    )


def offer_slot_to_waiting_list(
    session: Session, slot_id: int, *, usuario: str, now: datetime | None = None
) -> SlotOffer:
    """Offer a freed slot to the next patient in the queue.

    This does not book anything: it records the offer and returns who to
    contact. Booking is a separate, separately-approved decision.
    """
    reference = now or now_utc()
    slot = bookable_slot(session, slot_id, now=reference)
    especialidad = slot.profesional.especialidad

    entries = waiting_list_entries(session, especialidad)
    as_entries = [to_queue_entry(e) for e in entries]
    chosen = next_in_queue(as_entries, especialidad)
    fila = next(e for e in entries if e.id == chosen.entrada_id)
    position = [d.entrada_id for d in sorted(as_entries, key=lambda d: d.sort_key)].index(
        chosen.entrada_id
    ) + 1

    fila.estado = EstadoListaEspera.OFRECIDA
    fila.ofrecida_en = reference
    fila.slot_ofrecido_id = slot.id
    session.flush()

    return SlotOffer(
        entry=fila,
        paciente=fila.paciente,
        slot=slot,
        posicion_original=position,
    )


def join_waiting_list(
    session: Session,
    *,
    paciente_id: int,
    especialidad: Especialidad,
    prioridad: PrioridadListaEspera = PrioridadListaEspera.ANTIGUEDAD,
    notas: str | None = None,
) -> WaitingList:
    get_patient(session, paciente_id)
    existing = session.scalar(
        select(WaitingList).where(
            WaitingList.paciente_id == paciente_id,
            WaitingList.especialidad == especialidad,
            WaitingList.estado == EstadoListaEspera.ACTIVA,
        )
    )
    if existing is not None:
        raise AlreadyOnWaitingList(
            f"The patient is already on the waiting list for {especialidad}.",
            sugerencia="Check their current position before enrolling them again.",
            detalles={"entrada_id": existing.id},
        )
    entry = WaitingList(
        paciente_id=paciente_id,
        especialidad=especialidad,
        prioridad=prioridad,
        estado=EstadoListaEspera.ACTIVA,
        notas=notas,
    )
    session.add(entry)
    session.flush()
    return entry


def record_visit_reason(
    session: Session, cita_id: int, motivo: str, *, usuario: str
) -> Appointment:
    """Attach a reason for consultation to an appointment. **Clinical data.**

    The one operation that crosses from administrative into clinical territory
    (Res. 2654/2019), so it refuses without recorded informed consent whatever
    the caller's scope. The scope check is necessary but not sufficient.
    """
    cita = get_appointment(session, cita_id)
    paciente = cita.paciente

    if not paciente.consentimiento_datos_clinicos:
        raise ConsentRequired(
            (
                f"Patient {paciente.nombre} has no informed consent on file for the "
                "handling of clinical data."
            ),
            sugerencia=(
                "Obtain and record informed consent before writing the reason for "
                "consultation. This is a requirement of Resolución 2654/2019, not a "
                "validation of this system."
            ),
            detalles={"paciente_id": paciente.id, "cita_id": cita_id},
        )

    cita.motivo = motivo.strip()
    cita.motivo_registrado_en = now_utc()
    cita.motivo_registrado_por = usuario

    # Clinical writes are audited even though no state changed: the regulation
    # cares about who touched clinical data, not about the state machine.
    _audit(
        session,
        cita,
        estado_anterior=cita.estado,
        estado_nuevo=cita.estado,
        usuario=usuario,
        motivo="Reason for consultation recorded (clinical data)",
    )
    session.flush()
    return cita
