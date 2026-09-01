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
    tipo_documento: DocumentType
    documento: str
    nombre: str
    telefono: str
    regimen: Regimen
    afiliacion_activa: bool
    eps: str | None = None

    @classmethod
    def of(cls, paciente: Patient) -> PatientSummary:
        return cls.model_validate(paciente)


class ProfessionalSummary(Model):
    id: int
    nombre: str
    registro: str
    especialidad: Specialty
    activo: bool


class ClinicInfo(Model):
    id: int
    nombre: str
    nit: str
    especialidad: str
    direccion: str | None = None
    telefono: str | None = None
    ciudad: str
    zona_horaria: str
    profesionales: list[ProfessionalSummary] = Field(default_factory=list)

    @classmethod
    def of(cls, clinica: Clinic, profesionales: list[Professional]) -> ClinicInfo:
        payload = cls.model_validate(clinica).model_dump()
        payload["profesionales"] = [ProfessionalSummary.model_validate(p) for p in profesionales]
        return cls.model_validate(payload)


class FreeSlot(Model):
    slot_id: int
    profesional_id: int
    profesional: str
    especialidad: Specialty
    inicio_utc: datetime
    #: Same instant in clinic local time. Both are returned on purpose: the
    #: model reasons in local time, the system stores UTC.
    inicio_local: str
    fin_local: str

    @classmethod
    def of(cls, slot: AvailableSlot) -> FreeSlot:
        return cls(
            slot_id=slot.slot_id,
            profesional_id=slot.profesional_id,
            profesional=slot.profesional,
            especialidad=slot.especialidad,
            inicio_utc=slot.inicio,
            inicio_local=f"{to_clinic_time(slot.inicio):%Y-%m-%d %H:%M}",
            fin_local=f"{to_clinic_time(slot.fin):%H:%M}",
        )


class HistoryItem(Model):
    estado_anterior: AppointmentState | None
    estado_nuevo: AppointmentState
    usuario: str
    motivo: str | None
    momento: datetime


class AppointmentDetail(Model):
    id: int
    estado: AppointmentState
    paciente_id: int
    paciente: str
    profesional_id: int
    profesional: str
    especialidad: Specialty
    slot_id: int
    inicio_local: str
    fin_local: str
    creada_por: str
    creada_en: datetime
    motivo_cancelacion: str | None = None
    #: Clinical data. Present only when it has been recorded through the
    #: `clinical` scope with consent on file.
    motivo: str | None = None
    cita_origen_id: int | None = None
    #: What this appointment can legally become next. Returned so the model can
    #: pick the right tool, and so a write tool can refuse to propose a
    #: transition that would fail on confirmation.
    transiciones_validas: list[AppointmentState] = Field(default_factory=list)
    historial: list[HistoryItem] = Field(default_factory=list)

    @classmethod
    def of(cls, cita: Appointment, *, incluir_historial: bool = True) -> AppointmentDetail:
        return cls(
            id=cita.id,
            estado=cita.estado,
            paciente_id=cita.paciente_id,
            paciente=cita.paciente.nombre,
            profesional_id=cita.profesional_id,
            profesional=cita.profesional.nombre,
            especialidad=cita.profesional.especialidad,
            slot_id=cita.slot_id,
            inicio_local=f"{to_clinic_time(cita.slot.inicio):%Y-%m-%d %H:%M}",
            fin_local=f"{to_clinic_time(cita.slot.fin):%H:%M}",
            creada_por=cita.creada_por,
            creada_en=cita.creada_en,
            motivo_cancelacion=cita.motivo_cancelacion,
            motivo=cita.motivo,
            cita_origen_id=cita.cita_origen_id,
            transiciones_validas=sorted(reachable_states(cita.estado)),
            historial=(
                [HistoryItem.model_validate(h) for h in cita.historial] if incluir_historial else []
            ),
        )


class AfiliacionResponse(Model):
    paciente_id: int
    regimen: Regimen
    activa: bool
    regimen_efectivo: Regimen
    cubierto: bool
    requiere_copago: bool
    concepto_cargo: ChargeConcept
    mensaje: str
    sugerencia: str | None = None
    bloquea_agendamiento: bool

    @classmethod
    def of(cls, paciente_id: int, result: AfiliacionResult) -> AfiliacionResponse:
        return cls(
            paciente_id=paciente_id,
            regimen=result.regimen,
            activa=result.activa,
            regimen_efectivo=result.regimen_efectivo,
            cubierto=result.cubierto,
            requiere_copago=result.requiere_copago,
            concepto_cargo=result.concepto_cargo,
            mensaje=result.mensaje,
            sugerencia=result.sugerencia,
            bloquea_agendamiento=result.bloquea_agendamiento,
        )


class ChargeSummary(Model):
    id: int
    concepto: ChargeConcept
    monto: Decimal
    descripcion: str | None
    estado: ChargeState
    vencimiento: date
    cita_id: int | None

    @classmethod
    def of(cls, cargo: Charge) -> ChargeSummary:
        return cls.model_validate(cargo)


class CarteraResponse(Model):
    paciente_id: int
    estado: CarteraState
    total_pendiente: Decimal
    total_vencido: Decimal
    dias_mora_maximo: int
    cantidad_cargos: int
    antiguedad: dict[str, Decimal]
    supera_umbral_alerta: bool
    mensaje: str
    cargos: list[ChargeSummary] = Field(default_factory=list)

    @classmethod
    def of(cls, resumen: CarteraSummary, cargos: list[Charge]) -> CarteraResponse:
        return cls(
            paciente_id=resumen.paciente_id,
            estado=resumen.estado,
            total_pendiente=resumen.total_pendiente,
            total_vencido=resumen.total_vencido,
            dias_mora_maximo=resumen.dias_mora_maximo,
            cantidad_cargos=resumen.cantidad_cargos,
            antiguedad=resumen.antiguedad,
            supera_umbral_alerta=resumen.supera_umbral_alerta,
            mensaje=resumen.mensaje,
            cargos=[ChargeSummary.of(c) for c in cargos],
        )


class WaitingEntrySummary(Model):
    id: int
    paciente_id: int
    paciente: str
    especialidad: Specialty
    prioridad: WaitingListPriority
    estado: WaitingListState
    creada_en: datetime
    notas: str | None = None

    @classmethod
    def of(cls, entry: WaitingList) -> WaitingEntrySummary:
        return cls(
            id=entry.id,
            paciente_id=entry.paciente_id,
            paciente=entry.paciente.nombre,
            especialidad=entry.especialidad,
            prioridad=entry.prioridad,
            estado=entry.estado,
            creada_en=entry.creada_en,
            notas=entry.notas,
        )


class CarteraPolicies(Model):
    cobra_no_show: bool
    monto_no_show: Decimal
    dias_gracia: int
    umbral_alerta_mora: Decimal
    penaliza_solo_confirmadas: bool
    plazo_pago_dias: int
    tarifas_particular: dict[str, Decimal]
    cuota_moderadora_por_nivel: dict[int, Decimal]
    porcentaje_copago_subsidiado: Decimal
    nota: str


# --------------------------------------------------------------------------- #
# Write requests
# --------------------------------------------------------------------------- #


class BookRequest(BaseModel):
    paciente_id: int = Field(gt=0)
    slot_id: int = Field(gt=0)
    especialidad_esperada: Specialty | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class CancelRequest(BaseModel):
    motivo: Reason


class RescheduleRequest(BaseModel):
    nuevo_slot_id: int = Field(gt=0)
    motivo: str | None = Field(default=None, max_length=500)


class AttendanceRequest(BaseModel):
    estado: AppointmentState

    @model_validator(mode="after")
    def _attendance_states_only(self) -> AttendanceRequest:
        permitidos = {AppointmentState.WAITING, AppointmentState.ATTENDED, AppointmentState.NO_SHOW}
        if self.estado not in permitidos:
            raise ValueError(
                "record_attendance solo acepta: " + ", ".join(sorted(str(e) for e in permitidos))
            )
        return self


class VisitReasonRequest(BaseModel):
    motivo: Reason


class OfferSlotRequest(BaseModel):
    slot_id: int = Field(gt=0)


class JoinWaitingListRequest(BaseModel):
    paciente_id: int = Field(gt=0)
    especialidad: Specialty
    prioridad: WaitingListPriority = WaitingListPriority.SENIORITY
    notas: str | None = Field(default=None, max_length=300)


# --------------------------------------------------------------------------- #
# Write responses
# --------------------------------------------------------------------------- #


class BookResponse(Model):
    cita: AppointmentDetail
    afiliacion: AfiliacionResponse
    #: Present when the patient is in arrears. Informational: the appointment
    #: was created regardless (§2.3).
    alerta_cartera: str | None = None
    reutilizada: bool = False


class TransitionResponse(Model):
    cita: AppointmentDetail
    estado_anterior: AppointmentState
    estado_nuevo: AppointmentState
    libero_cupo: bool
    genero_cargo: bool
    cargo: ChargeSummary | None = None
    siguiente_en_lista_espera: WaitingEntrySummary | None = None
    mensaje: str


class SlotOfferResponse(Model):
    entrada_id: int
    paciente_id: int
    paciente: str
    telefono: str
    especialidad: Specialty
    prioridad: WaitingListPriority
    posicion_original: int
    slot_id: int
    inicio_local: str
    mensaje: str


class DayAgenda(Model):
    fecha: date
    total: int
    por_estado: dict[str, int]
    citas: list[AppointmentDetail]


class ErrorResponse(BaseModel):
    """Documented shape of every failure, for the OpenAPI schema."""

    error: bool = True
    codigo: str
    mensaje: str
    sugerencia: str | None = None
    detalles: dict[str, Any] | None = None
