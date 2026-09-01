"""Pydantic v2 request and response models for the internal REST API.

These are also, indirectly, the contract the MCP tools expose to the model: the
tool schemas are generated from the typed signatures in `mcp_server/tools/`,
and those mirror what this module accepts and returns. Keeping the shapes tight
here is what makes the tool descriptions precise.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.afiliacion import AfiliacionResult
from backend.domain.cartera import CarteraSummary
from backend.domain.services import AvailableSlot
from backend.domain.states import reachable_states
from backend.domain.time import to_clinic_time
from backend.enums import (
    AppointmentState,
    CarteraState,
    ChargeConcept,
    ChargeState,
    DocumentType,
    Regimen,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)
from backend.models import Appointment, Charge, Clinic, Patient, Professional, WaitingList

DocumentNumber = Annotated[str, Field(min_length=4, max_length=20, pattern=r"^[0-9A-Za-z\-]+$")]
Reason = Annotated[str, Field(min_length=3, max_length=500)]


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=False)


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #


class PatientSummary(Model):
    id: int
    document_type: DocumentType
    document_number: str
    name: str
    phone: str
    regimen: Regimen
    afiliacion_active: bool
    eps: str | None = None

    @classmethod
    def of(cls, patient: Patient) -> PatientSummary:
        return cls.model_validate(patient)


class ProfessionalSummary(Model):
    id: int
    name: str
    license_number: str
    specialty: Specialty
    active: bool


class ClinicInfo(Model):
    id: int
    name: str
    nit: str
    specialty: str
    address: str | None = None
    phone: str | None = None
    city: str
    timezone_name: str
    professionals: list[ProfessionalSummary] = Field(default_factory=list)

    @classmethod
    def of(cls, clinic: Clinic, professionals: list[Professional]) -> ClinicInfo:
        payload = cls.model_validate(clinic).model_dump()
        payload["professionals"] = [ProfessionalSummary.model_validate(p) for p in professionals]
        return cls.model_validate(payload)


class FreeSlot(Model):
    slot_id: int
    professional_id: int
    professional: str
    specialty: Specialty
    start_utc: datetime
    #: Same instant in clinic local time. Both are returned on purpose: the
    #: model reasons in local time, the system stores UTC.
    start_local: str
    end_local: str

    @classmethod
    def of(cls, slot: AvailableSlot) -> FreeSlot:
        return cls(
            slot_id=slot.slot_id,
            professional_id=slot.professional_id,
            professional=slot.professional,
            specialty=slot.specialty,
            start_utc=slot.start,
            start_local=f"{to_clinic_time(slot.start):%Y-%m-%d %H:%M}",
            end_local=f"{to_clinic_time(slot.end):%H:%M}",
        )


class HistoryItem(Model):
    previous_status: AppointmentState | None
    new_status: AppointmentState
    user: str
    reason: str | None
    occurred_at: datetime


class AppointmentDetail(Model):
    id: int
    status: AppointmentState
    patient_id: int
    patient: str
    professional_id: int
    professional: str
    specialty: Specialty
    slot_id: int
    start_local: str
    end_local: str
    created_by: str
    created_at: datetime
    cancellation_reason: str | None = None
    #: Clinical data. Present only when it has been recorded through the
    #: `clinical` scope with consent on file.
    reason: str | None = None
    source_appointment_id: int | None = None
    #: What this appointment can legally become next. Returned so the model can
    #: pick the right tool, and so a write tool can refuse to propose a
    #: transition that would fail on confirmation.
    valid_transitions: list[AppointmentState] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)

    @classmethod
    def of(cls, appointment: Appointment, *, incluir_historial: bool = True) -> AppointmentDetail:
        return cls(
            id=appointment.id,
            status=appointment.status,
            patient_id=appointment.patient_id,
            patient=appointment.patient.name,
            professional_id=appointment.professional_id,
            professional=appointment.professional.name,
            specialty=appointment.professional.specialty,
            slot_id=appointment.slot_id,
            start_local=f"{to_clinic_time(appointment.slot.start):%Y-%m-%d %H:%M}",
            end_local=f"{to_clinic_time(appointment.slot.end):%H:%M}",
            created_by=appointment.created_by,
            created_at=appointment.created_at,
            cancellation_reason=appointment.cancellation_reason,
            reason=appointment.reason,
            source_appointment_id=appointment.source_appointment_id,
            valid_transitions=sorted(reachable_states(appointment.status)),
            history=(
                [HistoryItem.model_validate(h) for h in appointment.history]
                if incluir_historial
                else []
            ),
        )


class AfiliacionResponse(Model):
    patient_id: int
    regimen: Regimen
    active: bool
    effective_regimen: Regimen
    covered: bool
    requires_copago: bool
    charge_concept: ChargeConcept
    message: str
    suggestion: str | None = None
    blocks_booking: bool

    @classmethod
    def of(cls, patient_id: int, result: AfiliacionResult) -> AfiliacionResponse:
        return cls(
            patient_id=patient_id,
            regimen=result.regimen,
            active=result.active,
            effective_regimen=result.effective_regimen,
            covered=result.covered,
            requires_copago=result.requires_copago,
            charge_concept=result.charge_concept,
            message=result.message,
            suggestion=result.suggestion,
            blocks_booking=result.blocks_booking,
        )


class ChargeSummary(Model):
    id: int
    concept: ChargeConcept
    amount: Decimal
    description: str | None
    status: ChargeState
    due_date: date
    appointment_id: int | None

    @classmethod
    def of(cls, charge: Charge) -> ChargeSummary:
        return cls.model_validate(charge)


class CarteraResponse(Model):
    patient_id: int
    status: CarteraState
    pending_total: Decimal
    overdue_total: Decimal
    max_overdue_days: int
    charge_count: int
    ageing: dict[str, Decimal]
    above_alert_threshold: bool
    message: str
    charges: list[ChargeSummary] = Field(default_factory=list)

    @classmethod
    def of(cls, summary: CarteraSummary, charges: list[Charge]) -> CarteraResponse:
        return cls(
            patient_id=summary.patient_id,
            status=summary.status,
            pending_total=summary.pending_total,
            overdue_total=summary.overdue_total,
            max_overdue_days=summary.max_overdue_days,
            charge_count=summary.charge_count,
            ageing=summary.ageing,
            above_alert_threshold=summary.above_alert_threshold,
            message=summary.message,
            charges=[ChargeSummary.of(c) for c in charges],
        )


class WaitingEntrySummary(Model):
    id: int
    patient_id: int
    patient: str
    specialty: Specialty
    priority: WaitingListPriority
    status: WaitingListState
    created_at: datetime
    notes: str | None = None

    @classmethod
    def of(cls, entry: WaitingList) -> WaitingEntrySummary:
        return cls(
            id=entry.id,
            patient_id=entry.patient_id,
            patient=entry.patient.name,
            specialty=entry.specialty,
            priority=entry.priority,
            status=entry.status,
            created_at=entry.created_at,
            notes=entry.notes,
        )


class CarteraPolicies(Model):
    charges_no_show: bool
    no_show_amount: Decimal
    grace_days: int
    overdue_alert_threshold: Decimal
    penalises_only_confirmed: bool
    payment_term_days: int
    particular_tariffs: dict[str, Decimal]
    cuota_moderadora_by_level: dict[int, Decimal]
    subsidiado_copago_rate: Decimal
    note: str


# --------------------------------------------------------------------------- #
# Write requests
# --------------------------------------------------------------------------- #


class BookRequest(BaseModel):
    patient_id: int = Field(gt=0)
    slot_id: int = Field(gt=0)
    expected_specialty: Specialty | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class CancelRequest(BaseModel):
    reason: Reason


class RescheduleRequest(BaseModel):
    new_slot_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class AttendanceRequest(BaseModel):
    status: AppointmentState

    @model_validator(mode="after")
    def _attendance_states_only(self) -> AttendanceRequest:
        permitidos = {AppointmentState.WAITING, AppointmentState.ATTENDED, AppointmentState.NO_SHOW}
        if self.status not in permitidos:
            raise ValueError(
                "record_attendance solo acepta: " + ", ".join(sorted(str(e) for e in permitidos))
            )
        return self


class VisitReasonRequest(BaseModel):
    reason: Reason


class OfferSlotRequest(BaseModel):
    slot_id: int = Field(gt=0)


class JoinWaitingListRequest(BaseModel):
    patient_id: int = Field(gt=0)
    specialty: Specialty
    priority: WaitingListPriority = WaitingListPriority.SENIORITY
    notes: str | None = Field(default=None, max_length=300)


# --------------------------------------------------------------------------- #
# Write responses
# --------------------------------------------------------------------------- #


class BookResponse(Model):
    appointment: AppointmentDetail
    afiliacion: AfiliacionResponse
    #: Present when the patient is in arrears. Informational: the appointment
    #: was created regardless (§2.3).
    cartera_alert: str | None = None
    reused: bool = False


class TransitionResponse(Model):
    appointment: AppointmentDetail
    previous_status: AppointmentState
    new_status: AppointmentState
    freed_slot: bool
    created_charge: bool
    charge: ChargeSummary | None = None
    next_in_waiting_list: WaitingEntrySummary | None = None
    message: str


class SlotOfferResponse(Model):
    entry_id: int
    patient_id: int
    patient: str
    phone: str
    specialty: Specialty
    priority: WaitingListPriority
    original_position: int
    slot_id: int
    start_local: str
    message: str


class DayAgenda(Model):
    day: date
    total: int
    by_status: dict[str, int]
    appointments: list[AppointmentDetail]


class ErrorResponse(BaseModel):
    """Documented shape of every failure, for the OpenAPI schema."""

    error: bool = True
    code: str
    message: str
    suggestion: str | None = None
    details: dict[str, Any] | None = None
