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

from backend.domain.afiliacion import ResultadoAfiliacion
from backend.domain.cartera import ResumenCartera
from backend.domain.servicios import SlotDisponible
from backend.domain.tiempo import a_local
from backend.enums import (
    ConceptoCargo,
    Especialidad,
    EstadoCargo,
    EstadoCartera,
    EstadoCita,
    EstadoListaEspera,
    PrioridadListaEspera,
    Regimen,
    TipoDocumento,
)
from backend.models import Cargo, Cita, Clinica, ListaEspera, Paciente, Profesional

Documento = Annotated[str, Field(min_length=4, max_length=20, pattern=r"^[0-9A-Za-z\-]+$")]
Motivo = Annotated[str, Field(min_length=3, max_length=500)]


class Modelo(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=False)


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #


class PacienteResumen(Modelo):
    id: int
    tipo_documento: TipoDocumento
    documento: str
    nombre: str
    telefono: str
    regimen: Regimen
    afiliacion_activa: bool
    eps: str | None = None

    @classmethod
    def desde(cls, paciente: Paciente) -> PacienteResumen:
        return cls.model_validate(paciente)


class ProfesionalResumen(Modelo):
    id: int
    nombre: str
    registro: str
    especialidad: Especialidad
    activo: bool


class ClinicaInfo(Modelo):
    id: int
    nombre: str
    nit: str
    especialidad: str
    direccion: str | None = None
    telefono: str | None = None
    ciudad: str
    zona_horaria: str
    profesionales: list[ProfesionalResumen] = Field(default_factory=list)

    @classmethod
    def desde(cls, clinica: Clinica, profesionales: list[Profesional]) -> ClinicaInfo:
        datos = cls.model_validate(clinica).model_dump()
        datos["profesionales"] = [ProfesionalResumen.model_validate(p) for p in profesionales]
        return cls.model_validate(datos)


class SlotLibre(Modelo):
    slot_id: int
    profesional_id: int
    profesional: str
    especialidad: Especialidad
    inicio_utc: datetime
    #: Same instant in clinic local time. Both are returned on purpose: the
    #: model reasons in local time, the system stores UTC.
    inicio_local: str
    fin_local: str

    @classmethod
    def desde(cls, slot: SlotDisponible) -> SlotLibre:
        return cls(
            slot_id=slot.slot_id,
            profesional_id=slot.profesional_id,
            profesional=slot.profesional,
            especialidad=slot.especialidad,
            inicio_utc=slot.inicio,
            inicio_local=f"{a_local(slot.inicio):%Y-%m-%d %H:%M}",
            fin_local=f"{a_local(slot.fin):%H:%M}",
        )


class HistorialItem(Modelo):
    estado_anterior: EstadoCita | None
    estado_nuevo: EstadoCita
    usuario: str
    motivo: str | None
    momento: datetime


class CitaDetalle(Modelo):
    id: int
    estado: EstadoCita
    paciente_id: int
    paciente: str
    profesional_id: int
    profesional: str
    especialidad: Especialidad
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
    historial: list[HistorialItem] = Field(default_factory=list)

    @classmethod
    def desde(cls, cita: Cita, *, incluir_historial: bool = True) -> CitaDetalle:
        return cls(
            id=cita.id,
            estado=cita.estado,
            paciente_id=cita.paciente_id,
            paciente=cita.paciente.nombre,
            profesional_id=cita.profesional_id,
            profesional=cita.profesional.nombre,
            especialidad=cita.profesional.especialidad,
            slot_id=cita.slot_id,
            inicio_local=f"{a_local(cita.slot.inicio):%Y-%m-%d %H:%M}",
            fin_local=f"{a_local(cita.slot.fin):%H:%M}",
            creada_por=cita.creada_por,
            creada_en=cita.creada_en,
            motivo_cancelacion=cita.motivo_cancelacion,
            motivo=cita.motivo,
            cita_origen_id=cita.cita_origen_id,
            historial=(
                [HistorialItem.model_validate(h) for h in cita.historial]
                if incluir_historial
                else []
            ),
        )


class AfiliacionRespuesta(Modelo):
    paciente_id: int
    regimen: Regimen
    activa: bool
    regimen_efectivo: Regimen
    cubierto: bool
    requiere_copago: bool
    concepto_cargo: ConceptoCargo
    mensaje: str
    sugerencia: str | None = None
    bloquea_agendamiento: bool

    @classmethod
    def desde(cls, paciente_id: int, resultado: ResultadoAfiliacion) -> AfiliacionRespuesta:
        return cls(
            paciente_id=paciente_id,
            regimen=resultado.regimen,
            activa=resultado.activa,
            regimen_efectivo=resultado.regimen_efectivo,
            cubierto=resultado.cubierto,
            requiere_copago=resultado.requiere_copago,
            concepto_cargo=resultado.concepto_cargo,
            mensaje=resultado.mensaje,
            sugerencia=resultado.sugerencia,
            bloquea_agendamiento=resultado.bloquea_agendamiento,
        )


class CargoResumen(Modelo):
    id: int
    concepto: ConceptoCargo
    monto: Decimal
    descripcion: str | None
    estado: EstadoCargo
    vencimiento: date
    cita_id: int | None

    @classmethod
    def desde(cls, cargo: Cargo) -> CargoResumen:
        return cls.model_validate(cargo)


class CarteraRespuesta(Modelo):
    paciente_id: int
    estado: EstadoCartera
    total_pendiente: Decimal
    total_vencido: Decimal
    dias_mora_maximo: int
    cantidad_cargos: int
    antiguedad: dict[str, Decimal]
    supera_umbral_alerta: bool
    mensaje: str
    cargos: list[CargoResumen] = Field(default_factory=list)

    @classmethod
    def desde(cls, resumen: ResumenCartera, cargos: list[Cargo]) -> CarteraRespuesta:
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
            cargos=[CargoResumen.desde(c) for c in cargos],
        )


class EntradaEsperaResumen(Modelo):
    id: int
    paciente_id: int
    paciente: str
    especialidad: Especialidad
    prioridad: PrioridadListaEspera
    estado: EstadoListaEspera
    creada_en: datetime
    notas: str | None = None

    @classmethod
    def desde(cls, entrada: ListaEspera) -> EntradaEsperaResumen:
        return cls(
            id=entrada.id,
            paciente_id=entrada.paciente_id,
            paciente=entrada.paciente.nombre,
            especialidad=entrada.especialidad,
            prioridad=entrada.prioridad,
            estado=entrada.estado,
            creada_en=entrada.creada_en,
            notas=entrada.notas,
        )


class PoliticasCartera(Modelo):
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


class AgendarRequest(BaseModel):
    paciente_id: int = Field(gt=0)
    slot_id: int = Field(gt=0)
    especialidad_esperada: Especialidad | None = None
    idempotency_key: str | None = Field(default=None, max_length=80)


class CancelarRequest(BaseModel):
    motivo: Motivo


class ReprogramarRequest(BaseModel):
    nuevo_slot_id: int = Field(gt=0)
    motivo: str | None = Field(default=None, max_length=500)


class AsistenciaRequest(BaseModel):
    estado: EstadoCita

    @model_validator(mode="after")
    def _solo_estados_de_asistencia(self) -> AsistenciaRequest:
        permitidos = {EstadoCita.EN_ESPERA, EstadoCita.ATENDIDA, EstadoCita.NO_ASISTIO}
        if self.estado not in permitidos:
            raise ValueError(
                "registrar_asistencia solo acepta: " + ", ".join(sorted(str(e) for e in permitidos))
            )
        return self


class MotivoConsultaRequest(BaseModel):
    motivo: Motivo


class OfrecerCupoRequest(BaseModel):
    slot_id: int = Field(gt=0)


class InscribirEsperaRequest(BaseModel):
    paciente_id: int = Field(gt=0)
    especialidad: Especialidad
    prioridad: PrioridadListaEspera = PrioridadListaEspera.ANTIGUEDAD
    notas: str | None = Field(default=None, max_length=300)


# --------------------------------------------------------------------------- #
# Write responses
# --------------------------------------------------------------------------- #


class AgendarRespuesta(Modelo):
    cita: CitaDetalle
    afiliacion: AfiliacionRespuesta
    #: Present when the patient is in arrears. Informational: the appointment
    #: was created regardless (§2.3).
    alerta_cartera: str | None = None
    reutilizada: bool = False


class TransicionRespuesta(Modelo):
    cita: CitaDetalle
    estado_anterior: EstadoCita
    estado_nuevo: EstadoCita
    libero_cupo: bool
    genero_cargo: bool
    cargo: CargoResumen | None = None
    siguiente_en_lista_espera: EntradaEsperaResumen | None = None
    mensaje: str


class OfertaCupoRespuesta(Modelo):
    entrada_id: int
    paciente_id: int
    paciente: str
    telefono: str
    especialidad: Especialidad
    prioridad: PrioridadListaEspera
    posicion_original: int
    slot_id: int
    inicio_local: str
    mensaje: str


class AgendaDelDia(Modelo):
    fecha: date
    total: int
    por_estado: dict[str, int]
    citas: list[CitaDetalle]


class RespuestaError(BaseModel):
    """Documented shape of every failure, for the OpenAPI schema."""

    error: bool = True
    codigo: str
    mensaje: str
    sugerencia: str | None = None
    detalles: dict[str, Any] | None = None
