"""Accounts receivable and collections (§2.3).

Every attended appointment, and depending on policy every no-show, produces a
charge. A patient's outstanding charges are their cartera, and its ageing is
what a collections agent acts on.

Two rules are load-bearing and easy to get wrong:

1. Debt **warns, it does not block**. Clinics do not refuse care over an unpaid
   copayment. Configurable via :class:`PoliticaCartera` because it varies.
2. The no-show penalty is policy, not law. It applies only when the clinic
   enables it and only when the patient had confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from backend.domain.afiliacion import (
    CUOTA_MODERADORA_POR_NIVEL,
    PORCENTAJE_COPAGO_SUBSIDIADO,
    ResultadoAfiliacion,
    tarifa_base,
)
from backend.enums import ConceptoCargo, EstadoCargo, EstadoCartera, Regimen

#: Ageing buckets the sector uses to prioritise collections.
BUCKETS_ANTIGUEDAD: tuple[tuple[str, int, int | None], ...] = (
    ("corriente", -10_000, 0),  # not due yet
    ("1_30", 1, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("mas_90", 91, None),
)


@dataclass(frozen=True, slots=True)
class PoliticaCartera:
    """Clinic-configurable collection policy."""

    cobra_no_show: bool = True
    monto_no_show: Decimal = Decimal("40000")
    #: Days before a charge counts as in arrears.
    dias_gracia: int = 0
    #: Debt above this amount raises a warning when scheduling. It never blocks.
    umbral_alerta_mora: Decimal = Decimal("50000")
    #: Only a patient who had *confirmed* is penalised for not showing up.
    penaliza_solo_confirmadas: bool = True


POLITICA_POR_DEFECTO = PoliticaCartera()


@dataclass(frozen=True, slots=True)
class CargoCalculado:
    """A charge the domain decided to create, before it is persisted."""

    concepto: ConceptoCargo
    monto: Decimal
    descripcion: str


@dataclass(frozen=True, slots=True)
class CargoPendiente:
    """Read model of a single outstanding charge."""

    cargo_id: int
    concepto: ConceptoCargo
    monto: Decimal
    vencimiento: date
    estado: EstadoCargo

    def dias_vencidos(self, hoy: date) -> int:
        """Positive when overdue, negative when it is not due yet."""
        return (hoy - self.vencimiento).days


@dataclass(frozen=True, slots=True)
class ResumenCartera:
    """What `consultar_cartera` returns and what a collections agent acts on."""

    paciente_id: int
    estado: EstadoCartera
    total_pendiente: Decimal
    total_vencido: Decimal
    dias_mora_maximo: int
    cantidad_cargos: int
    antiguedad: dict[str, Decimal] = field(default_factory=dict)
    supera_umbral_alerta: bool = False
    mensaje: str = ""


def _redondear(monto: Decimal) -> Decimal:
    """COP has no cents in practice; round to the peso."""
    return monto.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def calcular_cargo_por_atencion(
    afiliacion: ResultadoAfiliacion,
    especialidad: str,
    *,
    nivel_cuota_moderadora: int = 1,
) -> CargoCalculado | None:
    """Charge produced when an appointment reaches `atendida`.

    ``None`` when the service is fully covered (SOAT). That is a legitimate
    outcome, not an error.
    """
    tarifa = tarifa_base(especialidad)

    if afiliacion.regimen_efectivo is Regimen.SOAT:
        return None

    if afiliacion.regimen_efectivo is Regimen.PARTICULAR:
        return CargoCalculado(
            concepto=ConceptoCargo.PARTICULAR,
            monto=_redondear(tarifa),
            descripcion=f"Private tariff · {especialidad}",
        )

    if afiliacion.regimen_efectivo is Regimen.CONTRIBUTIVO:
        cuota = CUOTA_MODERADORA_POR_NIVEL.get(
            nivel_cuota_moderadora, CUOTA_MODERADORA_POR_NIVEL[1]
        )
        return CargoCalculado(
            concepto=ConceptoCargo.CUOTA_MODERADORA,
            monto=_redondear(cuota),
            descripcion=f"Cuota moderadora · {especialidad}",
        )

    # Subsidised regime: percentage copayment over the reference tariff.
    return CargoCalculado(
        concepto=ConceptoCargo.COPAGO,
        monto=_redondear(tarifa * PORCENTAJE_COPAGO_SUBSIDIADO),
        descripcion=f"Copago, subsidiado régimen · {especialidad}",
    )


def calcular_cargo_por_no_show(
    *,
    estaba_confirmada: bool,
    politica: PoliticaCartera = POLITICA_POR_DEFECTO,
) -> CargoCalculado | None:
    """Penalty charge for a missed appointment, if the policy enables it."""
    if not politica.cobra_no_show:
        return None
    if politica.penaliza_solo_confirmadas and not estaba_confirmada:
        return None
    return CargoCalculado(
        concepto=ConceptoCargo.NO_SHOW,
        monto=_redondear(politica.monto_no_show),
        descripcion="No-show penalty, no prior cancellation",
    )


def _bucket(dias: int) -> str:
    for nombre, desde, hasta in BUCKETS_ANTIGUEDAD:
        if dias >= desde and (hasta is None or dias <= hasta):
            return nombre
    return "corriente"


def resumir_cartera(
    paciente_id: int,
    cargos: list[CargoPendiente],
    *,
    hoy: date,
    politica: PoliticaCartera = POLITICA_POR_DEFECTO,
) -> ResumenCartera:
    """Aggregate a patient's outstanding charges into a collections view."""
    pendientes = [c for c in cargos if c.estado is EstadoCargo.PENDIENTE]

    total_pendiente = _redondear(sum((c.monto for c in pendientes), Decimal("0")))
    vencidos = [c for c in pendientes if c.dias_vencidos(hoy) > politica.dias_gracia]
    total_vencido = _redondear(sum((c.monto for c in vencidos), Decimal("0")))
    dias_mora_maximo = max((c.dias_vencidos(hoy) for c in vencidos), default=0)

    antiguedad: dict[str, Decimal] = {nombre: Decimal("0") for nombre, _, _ in BUCKETS_ANTIGUEDAD}
    for cargo in pendientes:
        antiguedad[_bucket(cargo.dias_vencidos(hoy))] += cargo.monto
    antiguedad = {k: _redondear(v) for k, v in antiguedad.items()}

    estado = EstadoCartera.EN_MORA if vencidos else EstadoCartera.AL_DIA
    supera_umbral = total_vencido >= politica.umbral_alerta_mora

    if estado is EstadoCartera.AL_DIA:
        mensaje = (
            "Cartera up to date."
            if not pendientes
            else f"Cartera up to date. ${total_pendiente:,.0f} COP not yet due."
        )
    else:
        mensaje = (
            f"Cartera in arrears: ${total_vencido:,.0f} COP overdue, "
            f"up to {dias_mora_maximo} days late."
        )
        if supera_umbral:
            mensaje += " Above the clinic's alert threshold."

    return ResumenCartera(
        paciente_id=paciente_id,
        estado=estado,
        total_pendiente=total_pendiente,
        total_vencido=total_vencido,
        dias_mora_maximo=dias_mora_maximo,
        cantidad_cargos=len(pendientes),
        antiguedad=antiguedad,
        supera_umbral_alerta=supera_umbral,
        mensaje=mensaje,
    )


def alerta_al_agendar(resumen: ResumenCartera) -> str | None:
    """Warning shown when scheduling a patient in arrears, never a block.

    Returning a string rather than raising is the point: the tool layer passes
    it to the model as context and the appointment still goes through.
    """
    if resumen.estado is EstadoCartera.AL_DIA or not resumen.supera_umbral_alerta:
        return None
    return (
        f"Heads-up: the patient has ${resumen.total_vencido:,.0f} COP of overdue "
        f"cartera ({resumen.dias_mora_maximo} days). The appointment can still be "
        "booked; tell the patient about the outstanding balance when confirming."
    )
