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
    CUOTA_MODERADORA_BY_BRACKET,
    SUBSIDIADO_COPAGO_RATE,
    AfiliacionResult,
    base_tariff,
)
from backend.enums import CarteraState, ChargeConcept, ChargeState, Regimen

#: Ageing buckets the sector uses to prioritise collections.
AGEING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("corriente", -10_000, 0),  # not due yet
    ("1_30", 1, 30),
    ("31_60", 31, 60),
    ("61_90", 61, 90),
    ("mas_90", 91, None),
)


@dataclass(frozen=True, slots=True)
class CarteraPolicy:
    """Clinic-configurable collection policy."""

    cobra_no_show: bool = True
    monto_no_show: Decimal = Decimal("40000")
    #: Days before a charge counts as in arrears.
    dias_gracia: int = 0
    #: Debt above this amount raises a warning when scheduling. It never blocks.
    umbral_alerta_mora: Decimal = Decimal("50000")
    #: Only a patient who had *confirmed* is penalised for not showing up.
    penaliza_solo_confirmadas: bool = True


DEFAULT_POLICY = CarteraPolicy()


@dataclass(frozen=True, slots=True)
class CalculatedCharge:
    """A charge the domain decided to create, before it is persisted."""

    concepto: ChargeConcept
    monto: Decimal
    descripcion: str


@dataclass(frozen=True, slots=True)
class PendingCharge:
    """Read model of a single outstanding charge."""

    cargo_id: int
    concepto: ChargeConcept
    monto: Decimal
    vencimiento: date
    estado: ChargeState

    def days_overdue(self, hoy: date) -> int:
        """Positive when overdue, negative when it is not due yet."""
        return (hoy - self.vencimiento).days


@dataclass(frozen=True, slots=True)
class CarteraSummary:
    """What `check_cartera` returns and what a collections agent acts on."""

    paciente_id: int
    estado: CarteraState
    total_pendiente: Decimal
    total_vencido: Decimal
    dias_mora_maximo: int
    cantidad_cargos: int
    antiguedad: dict[str, Decimal] = field(default_factory=dict)
    supera_umbral_alerta: bool = False
    mensaje: str = ""


def _round(monto: Decimal) -> Decimal:
    """COP has no cents in practice; round to the peso."""
    return monto.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def charge_for_visit(
    afiliacion: AfiliacionResult,
    especialidad: str,
    *,
    nivel_cuota_moderadora: int = 1,
) -> CalculatedCharge | None:
    """Charge produced when an appointment reaches `atendida`.

    ``None`` when the service is fully covered (SOAT). That is a legitimate
    outcome, not an error.
    """
    tariff = base_tariff(especialidad)

    if afiliacion.regimen_efectivo is Regimen.SOAT:
        return None

    if afiliacion.regimen_efectivo is Regimen.PARTICULAR:
        return CalculatedCharge(
            concepto=ChargeConcept.PARTICULAR,
            monto=_round(tariff),
            descripcion=f"Private tariff · {especialidad}",
        )

    if afiliacion.regimen_efectivo is Regimen.CONTRIBUTIVO:
        fee = CUOTA_MODERADORA_BY_BRACKET.get(
            nivel_cuota_moderadora, CUOTA_MODERADORA_BY_BRACKET[1]
        )
        return CalculatedCharge(
            concepto=ChargeConcept.CUOTA_MODERADORA,
            monto=_round(fee),
            descripcion=f"Cuota moderadora · {especialidad}",
        )

    # Subsidised regime: percentage copayment over the reference tariff.
    return CalculatedCharge(
        concepto=ChargeConcept.COPAGO,
        monto=_round(tariff * SUBSIDIADO_COPAGO_RATE),
        descripcion=f"Copago, subsidiado régimen · {especialidad}",
    )


def charge_for_no_show(
    *,
    estaba_confirmada: bool,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CalculatedCharge | None:
    """Penalty charge for a missed appointment, if the policy enables it."""
    if not policy.cobra_no_show:
        return None
    if policy.penaliza_solo_confirmadas and not estaba_confirmada:
        return None
    return CalculatedCharge(
        concepto=ChargeConcept.NO_SHOW,
        monto=_round(policy.monto_no_show),
        descripcion="No-show penalty, no prior cancellation",
    )


def _bucket(dias: int) -> str:
    for nombre, desde, hasta in AGEING_BUCKETS:
        if dias >= desde and (hasta is None or dias <= hasta):
            return nombre
    return "corriente"


def summarise_cartera(
    paciente_id: int,
    cargos: list[PendingCharge],
    *,
    hoy: date,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CarteraSummary:
    """Aggregate a patient's outstanding charges into a collections view."""
    outstanding = [c for c in cargos if c.estado is ChargeState.PENDING]

    total_pendiente = _round(sum((c.monto for c in outstanding), Decimal("0")))
    overdue = [c for c in outstanding if c.days_overdue(hoy) > policy.dias_gracia]
    total_vencido = _round(sum((c.monto for c in overdue), Decimal("0")))
    dias_mora_maximo = max((c.days_overdue(hoy) for c in overdue), default=0)

    antiguedad: dict[str, Decimal] = {nombre: Decimal("0") for nombre, _, _ in AGEING_BUCKETS}
    for cargo in outstanding:
        antiguedad[_bucket(cargo.days_overdue(hoy))] += cargo.monto
    antiguedad = {k: _round(v) for k, v in antiguedad.items()}

    estado = CarteraState.EN_MORA if overdue else CarteraState.AL_DIA
    supera_umbral = total_vencido >= policy.umbral_alerta_mora

    if estado is CarteraState.AL_DIA:
        mensaje = (
            "Cartera up to date."
            if not outstanding
            else f"Cartera up to date. ${total_pendiente:,.0f} COP not yet due."
        )
    else:
        mensaje = (
            f"Cartera in arrears: ${total_vencido:,.0f} COP overdue, "
            f"up to {dias_mora_maximo} days late."
        )
        if supera_umbral:
            mensaje += " Above the clinic's alert threshold."

    return CarteraSummary(
        paciente_id=paciente_id,
        estado=estado,
        total_pendiente=total_pendiente,
        total_vencido=total_vencido,
        dias_mora_maximo=dias_mora_maximo,
        cantidad_cargos=len(outstanding),
        antiguedad=antiguedad,
        supera_umbral_alerta=supera_umbral,
        mensaje=mensaje,
    )


def booking_warning(resumen: CarteraSummary) -> str | None:
    """Warning shown when scheduling a patient in arrears, never a block.

    Returning a string rather than raising is the point: the tool layer passes
    it to the model as context and the appointment still goes through.
    """
    if resumen.estado is CarteraState.AL_DIA or not resumen.supera_umbral_alerta:
        return None
    return (
        f"Heads-up: the patient has ${resumen.total_vencido:,.0f} COP of overdue "
        f"cartera ({resumen.dias_mora_maximo} days). The appointment can still be "
        "booked; tell the patient about the outstanding balance when confirming."
    )
