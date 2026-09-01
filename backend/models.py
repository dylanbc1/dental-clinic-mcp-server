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


def _enum(enum_cls: type, nombre: str) -> SAEnum:
    """Native PostgreSQL enum storing StrEnum values, not member names, so the
    database reads cleanly in plain SQL."""
    return SAEnum(
        enum_cls,
        name=nombre,
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
    creada_en: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    actualizada_en: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Organisation
# --------------------------------------------------------------------------- #


class Clinic(Base, TimestampMixin):
    __tablename__ = "clinica"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    nit: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    especialidad: Mapped[str] = mapped_column(String(80), nullable=False)
    direccion: Mapped[str | None] = mapped_column(String(200))
    telefono: Mapped[str | None] = mapped_column(String(30))
    ciudad: Mapped[str] = mapped_column(String(80), default="Bogotá", nullable=False)
    zona_horaria: Mapped[str] = mapped_column(String(50), default="America/Bogota", nullable=False)

    profesionales: Mapped[list[Professional]] = relationship(
        back_populates="clinica", cascade="all, delete-orphan"
    )


class Professional(Base, TimestampMixin):
    __tablename__ = "profesional"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinica_id: Mapped[int] = mapped_column(
        ForeignKey("clinica.id", ondelete="CASCADE"), index=True, nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Colombian professional registration number (registro profesional).
    registro: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    especialidad: Mapped[Specialty] = mapped_column(
        _enum(Specialty, "specialty_enum"), nullable=False, index=True
    )
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    clinica: Mapped[Clinic] = relationship(back_populates="profesionales")
    slots: Mapped[list[AgendaSlot]] = relationship(
        back_populates="profesional", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #


class Patient(Base, TimestampMixin):
    __tablename__ = "paciente"
    __table_args__ = (
        UniqueConstraint("tipo_documento", "documento", name="uq_paciente_documento"),
        Index("ix_paciente_nombre_lower", func.lower(text("nombre"))),
        CheckConstraint("nivel_cuota_moderadora between 1 and 3", name="ck_paciente_nivel_cuota"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tipo_documento: Mapped[DocumentType] = mapped_column(
        _enum(DocumentType, "tipo_documento_enum"),
        default=DocumentType.CC,
        nullable=False,
    )
    documento: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    telefono: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str | None] = mapped_column(String(160))
    fecha_nacimiento: Mapped[date | None] = mapped_column(Date)

    regimen: Mapped[Regimen] = mapped_column(
        _enum(Regimen, "regimen_enum"), nullable=False, index=True
    )
    afiliacion_activa: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    eps: Mapped[str | None] = mapped_column(String(120))
    #: Income bracket driving the cuota moderadora (1-3).
    nivel_cuota_moderadora: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Informed consent for clinical data (Res. 2654/2019). Gates the
    #: `record_visit_reason` tool, the only one that touches it.
    consentimiento_datos_clinicos: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    consentimiento_otorgado_en: Mapped[datetime | None] = mapped_column(TS)

    citas: Mapped[list[Appointment]] = relationship(back_populates="paciente")
    cargos: Mapped[list[Charge]] = relationship(back_populates="paciente")


# --------------------------------------------------------------------------- #
# Agenda
# --------------------------------------------------------------------------- #


class AgendaSlot(Base, TimestampMixin):
    __tablename__ = "agenda_slot"
    __table_args__ = (
        UniqueConstraint("profesional_id", "inicio", name="uq_slot_profesional_inicio"),
        CheckConstraint("fin > inicio", name="ck_slot_rango_valido"),
        Index("ix_slot_busqueda", "fecha", "estado", "profesional_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    profesional_id: Mapped[int] = mapped_column(
        ForeignKey("profesional.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised clinic-local date, so "free slots on Tuesday" is one index
    #: scan instead of a timezone conversion per row.
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    inicio: Mapped[datetime] = mapped_column(TS, nullable=False)
    fin: Mapped[datetime] = mapped_column(TS, nullable=False)
    estado: Mapped[SlotState] = mapped_column(
        _enum(SlotState, "slot_state_enum"), default=SlotState.FREE, nullable=False
    )
    #: Optimistic-locking counter, bumped and checked on every UPDATE so two
    #: concurrent bookings cannot both believe they won.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    profesional: Mapped[Professional] = relationship(back_populates="slots")

    __mapper_args__ = {"version_id_col": version_id}  # noqa: RUF012


class Appointment(Base, TimestampMixin):
    __tablename__ = "cita"
    __table_args__ = (
        # The anti-double-booking guarantee. Only states that hold the slot
        # participate, so a cancelled appointment releases it.
        Index(
            "uq_cita_slot_activa",
            "slot_id",
            unique=True,
            postgresql_where=text("estado in ('scheduled','confirmed','waiting','attended')"),
        ),
        # An agent that resends the same booking gets the same appointment back
        # instead of a duplicate.
        UniqueConstraint("idempotency_key", name="uq_cita_idempotency"),
        Index("ix_cita_paciente_estado", "paciente_id", "estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(
        ForeignKey("paciente.id", ondelete="RESTRICT"), nullable=False
    )
    profesional_id: Mapped[int] = mapped_column(
        ForeignKey("profesional.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("agenda_slot.id", ondelete="RESTRICT"), nullable=False
    )
    estado: Mapped[AppointmentState] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum"),
        default=AppointmentState.SCHEDULED,
        nullable=False,
        index=True,
    )

    #: Clinical data (Res. 2654/2019). Written only through the `clinical`
    #: scope, only with consent on file, and always audited.
    motivo: Mapped[str | None] = mapped_column(Text)
    motivo_registrado_en: Mapped[datetime | None] = mapped_column(TS)
    motivo_registrado_por: Mapped[str | None] = mapped_column(String(120))

    motivo_cancelacion: Mapped[str | None] = mapped_column(Text)
    creada_por: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80))
    #: Set when this appointment came from rescheduling another.
    cita_origen_id: Mapped[int | None] = mapped_column(ForeignKey("cita.id", ondelete="SET NULL"))

    paciente: Mapped[Patient] = relationship(back_populates="citas")
    profesional: Mapped[Professional] = relationship()
    slot: Mapped[AgendaSlot] = relationship()
    historial: Mapped[list[AppointmentHistory]] = relationship(
        back_populates="cita",
        cascade="all, delete-orphan",
        order_by="AppointmentHistory.momento",
    )
    cargos: Mapped[list[Charge]] = relationship(back_populates="cita")


class AppointmentHistory(Base):
    """Append-only audit trail of state changes (security layer 5).

    No ``actualizada_en``: rows here are never updated. That is the point.
    """

    __tablename__ = "cita_historial"
    __table_args__ = (Index("ix_historial_cita_momento", "cita_id", "momento"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cita_id: Mapped[int] = mapped_column(ForeignKey("cita.id", ondelete="CASCADE"), nullable=False)
    estado_anterior: Mapped[AppointmentState | None] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum")
    )
    estado_nuevo: Mapped[AppointmentState] = mapped_column(
        _enum(AppointmentState, "appointment_state_enum"), nullable=False
    )
    usuario: Mapped[str] = mapped_column(String(120), nullable=False)
    motivo: Mapped[str | None] = mapped_column(Text)
    momento: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    cita: Mapped[Appointment] = relationship(back_populates="historial")


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


class Charge(Base, TimestampMixin):
    __tablename__ = "cargo"
    __table_args__ = (
        CheckConstraint("monto >= 0", name="ck_cargo_monto_no_negativo"),
        Index("ix_cargo_paciente_estado", "paciente_id", "estado"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(
        ForeignKey("paciente.id", ondelete="RESTRICT"), nullable=False
    )
    cita_id: Mapped[int | None] = mapped_column(ForeignKey("cita.id", ondelete="SET NULL"))
    concepto: Mapped[ChargeConcept] = mapped_column(
        _enum(ChargeConcept, "concepto_cargo_enum"), nullable=False
    )
    monto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(200))
    estado: Mapped[ChargeState] = mapped_column(
        _enum(ChargeState, "charge_state_enum"), default=ChargeState.PENDING, nullable=False
    )
    vencimiento: Mapped[date] = mapped_column(Date, nullable=False)
    pagado_en: Mapped[datetime | None] = mapped_column(TS)

    paciente: Mapped[Patient] = relationship(back_populates="cargos")
    cita: Mapped[Appointment | None] = relationship(back_populates="cargos")


# --------------------------------------------------------------------------- #
# Waiting list
# --------------------------------------------------------------------------- #


class WaitingList(Base, TimestampMixin):
    __tablename__ = "lista_espera"
    __table_args__ = (
        # One active wait per specialty. Partial, so a retired entry does not
        # block re-enrolling later.
        Index(
            "uq_lista_espera_activa",
            "paciente_id",
            "especialidad",
            unique=True,
            postgresql_where=text("estado = 'active'"),
        ),
        Index("ix_lista_espera_cola", "especialidad", "estado", "prioridad", "creada_en"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(
        ForeignKey("paciente.id", ondelete="CASCADE"), nullable=False
    )
    especialidad: Mapped[Specialty] = mapped_column(
        _enum(Specialty, "specialty_enum"), nullable=False
    )
    prioridad: Mapped[WaitingListPriority] = mapped_column(
        _enum(WaitingListPriority, "waiting_list_priority_enum"),
        default=WaitingListPriority.SENIORITY,
        nullable=False,
    )
    estado: Mapped[WaitingListState] = mapped_column(
        _enum(WaitingListState, "waiting_list_state_enum"),
        default=WaitingListState.ACTIVE,
        nullable=False,
    )
    notas: Mapped[str | None] = mapped_column(String(300))
    ofrecida_en: Mapped[datetime | None] = mapped_column(TS)
    slot_ofrecido_id: Mapped[int | None] = mapped_column(
        ForeignKey("agenda_slot.id", ondelete="SET NULL")
    )

    paciente: Mapped[Patient] = relationship()
