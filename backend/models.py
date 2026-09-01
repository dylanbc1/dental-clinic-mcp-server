"""SQLAlchemy 2.x models, the domain's source of truth (§4).

Two choices separate a schema that demos well from one that survives concurrent
agents:

* **Double-booking is impossible in the database.** A partial unique index on
  ``cita.slot_id``, restricted to the states that hold a slot, means two agents
  racing end with one success and one clean 409. An application check alone
  always loses that race.
* **Every state change is auditable.** ``cita_historial`` is append-only and is
  written in the same transaction as the change, so an audit gap cannot happen.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.enums import (
    AppointmentState,
    ChargeConcept,
    ChargeState,
    DocumentType,
    Regimen,
    SlotState,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)


def _enum(enum_cls: type, name: str) -> SAEnum:
    """Native PostgreSQL enum storing StrEnum values, not member names, so the
    database reads cleanly in plain SQL."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Declarative base with the columns every table carries."""

    type_annotation_map = {  # noqa: RUF012
        Decimal: Numeric(12, 2),
        datetime: TS,
        date: Date,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Organisation
# --------------------------------------------------------------------------- #


class Clinic(Base, TimestampMixin):
    __tablename__ = "clinic"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    nit: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    specialty: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(30))
    city: Mapped[str] = mapped_column(String(80), default="Bogotá", nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(50), default="America/Bogota", nullable=False)

    professionals: Mapped[list[Professional]] = relationship(
        back_populates="clinic", cascade="all, delete-orphan"
    )


class Professional(Base, TimestampMixin):
    __tablename__ = "professional"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinic.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Colombian professional registration number (registro profesional).
    license_number: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    specialty: Mapped[Specialty] = mapped_column(
        _enum(Specialty, "specialty_enum"), nullable=False, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    clinic: Mapped[Clinic] = relationship(back_populates="professionals")
    slots: Mapped[list[AgendaSlot]] = relationship(
        back_populates="professional", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #


class Patient(Base, TimestampMixin):
    __tablename__ = "patient"
    __table_args__ = (
        UniqueConstraint("document_type", "document_number", name="uq_patient_document"),
        Index("ix_patient_name_lower", func.lower(text("name"))),
        CheckConstraint(
            "cuota_moderadora_level between 1 and 3", name="ck_patient_cuota_moderadora_level"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_type: Mapped[DocumentType] = mapped_column(
        _enum(DocumentType, "document_type_enum"),
        default=DocumentType.CC,
        nullable=False,
    )
    document_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(160))
    birth_date: Mapped[date | None] = mapped_column(Date)

    regimen: Mapped[Regimen] = mapped_column(
        _enum(Regimen, "regimen_enum"), nullable=False, index=True
    )
    affiliation_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    eps: Mapped[str | None] = mapped_column(String(120))
    #: Income bracket driving the cuota moderadora (1-3).
    cuota_moderadora_level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Informed consent for clinical data (Res. 2654/2019). Gates the
    #: `record_visit_reason` tool, the only one that touches it.
    clinical_data_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_granted_at: Mapped[datetime | None] = mapped_column(TS)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")
    charges: Mapped[list[Charge]] = relationship(back_populates="patient")


# --------------------------------------------------------------------------- #
# Agenda
# --------------------------------------------------------------------------- #


class AgendaSlot(Base, TimestampMixin):
    __tablename__ = "agenda_slot"
    __table_args__ = (
        UniqueConstraint("professional_id", "starts_at", name="uq_slot_professional_start"),
        CheckConstraint("ends_at > starts_at", name="ck_slot_valid_range"),
        Index("ix_slot_search", "day", "status", "professional_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    professional_id: Mapped[int] = mapped_column(
        ForeignKey("professional.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised clinic-local date, so "free slots on Tuesday" is one index
    #: scan instead of a timezone conversion per row.
    day: Mapped[date] = mapped_column(Date, nullable=False)
    #: `end` is a reserved word in PostgreSQL, so both columns carry an
    #: explicit name. The Python attribute stays `start`/`end`, which is what
    #: reads well at the call site; the table gets `starts_at`/`ends_at`, which
    #: is what a hand-written query can use without quoting.
    start: Mapped[datetime] = mapped_column("starts_at", TS, nullable=False)
    end: Mapped[datetime] = mapped_column("ends_at", TS, nullable=False)
    status: Mapped[SlotState] = mapped_column(
        _enum(SlotState, "slot_state_enum"), default=SlotState.FREE, nullable=False
    )
    #: Optimistic-locking counter, bumped and checked on every UPDATE so two
    #: concurrent bookings cannot both believe they won.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    professional: Mapped[Professional] = relationship(back_populates="slots")

    __mapper_args__ = {"version_id_col": version_id}  # noqa: RUF012


class Appointment(Base, TimestampMixin):
    __tablename__ = "appointment"
    __table_args__ = (
        # The anti-double-booking guarantee. Only states that hold the slot
        # participate, so a cancelled appointment releases it.
        Index(
            "uq_appointment_slot_active",
            "slot_id",
            unique=True,
            postgresql_where=text("status in ('scheduled','confirmed','waiting','attended')"),
        ),
        # An agent that resends the same booking gets the same appointment back
        # instead of a duplicate.
        UniqueConstraint("idempotency_key", name="uq_appointment_idempotency"),
        Index("ix_appointment_patient_status", "patient_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False
    )
    professional_id: Mapped[int] = mapped_column(
        ForeignKey("professional.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("agenda_slot.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[AppointmentState] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum"),
        default=AppointmentState.SCHEDULED,
        nullable=False,
        index=True,
    )

    #: Clinical data (Res. 2654/2019). Written only through the `clinical`
    #: scope, only with consent on file, and always audited.
    reason: Mapped[str | None] = mapped_column(Text)
    reason_recorded_at: Mapped[datetime | None] = mapped_column(TS)
    reason_recorded_by: Mapped[str | None] = mapped_column(String(120))

    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    #: Set when this appointment came from rescheduling another.
    source_appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment.id", ondelete="SET NULL")
    )

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    professional: Mapped[Professional] = relationship()
    slot: Mapped[AgendaSlot] = relationship()
    history: Mapped[list[AppointmentHistory]] = relationship(
        back_populates="appointment",
        cascade="all, delete-orphan",
        order_by="AppointmentHistory.occurred_at",
    )
    charges: Mapped[list[Charge]] = relationship(back_populates="appointment")


class AppointmentHistory(Base):
    """Append-only audit trail of state changes (security layer 5).

    No ``actualizada_en``: rows here are never updated. That is the point.
    """

    __tablename__ = "appointment_history"
    __table_args__ = (Index("ix_history_appointment_occurred_at", "appointment_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointment.id", ondelete="CASCADE"), nullable=False
    )
    previous_status: Mapped[AppointmentState | None] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum")
    )
    new_status: Mapped[AppointmentState] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum"), nullable=False
    )
    #: `user` is reserved in PostgreSQL, so the column carries an explicit
    #: name. `changed_by` also says more in an audit table than `user` does.
    user: Mapped[str] = mapped_column("changed_by", String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    appointment: Mapped[Appointment] = relationship(back_populates="history")


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


class Charge(Base, TimestampMixin):
    __tablename__ = "charge"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_charge_amount_not_negative"),
        Index("ix_charge_patient_status", "patient_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False
    )
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointment.id", ondelete="SET NULL")
    )
    concept: Mapped[ChargeConcept] = mapped_column(
        _enum(ChargeConcept, "charge_concept_enum"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ChargeState] = mapped_column(
        _enum(ChargeState, "charge_state_enum"), default=ChargeState.PENDING, nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(TS)

    patient: Mapped[Patient] = relationship(back_populates="charges")
    appointment: Mapped[Appointment | None] = relationship(back_populates="charges")


# --------------------------------------------------------------------------- #
# Waiting list
# --------------------------------------------------------------------------- #


class WaitingList(Base, TimestampMixin):
    __tablename__ = "waiting_list"
    __table_args__ = (
        # One active wait per specialty. Partial, so a retired entry does not
        # block re-enrolling later.
        Index(
            "uq_waiting_list_active",
            "patient_id",
            "specialty",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_waiting_list_queue", "specialty", "status", "priority", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patient.id", ondelete="CASCADE"), nullable=False
    )
    specialty: Mapped[Specialty] = mapped_column(_enum(Specialty, "specialty_enum"), nullable=False)
    priority: Mapped[WaitingListPriority] = mapped_column(
        _enum(WaitingListPriority, "waiting_list_priority_enum"),
        default=WaitingListPriority.SENIORITY,
        nullable=False,
    )
    status: Mapped[WaitingListState] = mapped_column(
        _enum(WaitingListState, "waiting_list_state_enum"),
        default=WaitingListState.ACTIVE,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(String(300))
    offered_at: Mapped[datetime | None] = mapped_column(TS)
    offered_slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("agenda_slot.id", ondelete="SET NULL")
    )

    patient: Mapped[Patient] = relationship()
