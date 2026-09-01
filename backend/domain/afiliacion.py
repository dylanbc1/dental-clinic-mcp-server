"""Affiliation validation (§2.2).

Before scheduling, a Colombian IPS checks which regime the patient belongs to
and whether it is active. In production that is a call to ADRES/RUAF; here it is
a lookup against synthetic data. The consequences are modelled faithfully,
because they drive what the patient pays.

Nothing here touches the database: plain values in, a verdict out.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backend.enums import ConceptoCargo, Regimen

#: Reference tariffs in COP for a standard consultation. Mock 2026 values,
#: illustrative rather than a published fee schedule.
PRIVATE_TARIFF: dict[str, Decimal] = {
    "odontologia_general": Decimal("120000"),
    "ortodoncia": Decimal("180000"),
    "endodoncia": Decimal("350000"),
    "periodoncia": Decimal("200000"),
    "odontopediatria": Decimal("140000"),
}
DEFAULT_PRIVATE_TARIFF = Decimal("120000")

#: Contributory pays a cuota moderadora, a fixed fee bracketed by income.
#: Subsidised pays a copago proportional to the service. SOAT covers it fully.
CUOTA_MODERADORA_BY_BRACKET: dict[int, Decimal] = {
    1: Decimal("5500"),
    2: Decimal("22000"),
    3: Decimal("57500"),
}
SUBSIDIADO_COPAGO_RATE = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class AfiliacionResult:
    """The verdict returned by :func:`validar_afiliacion`."""

    regimen: Regimen
    activa: bool
    #: The regime actually billed. A lapsed affiliation falls back to
    #: `particular`: the patient is still attended, at full price.
    regimen_efectivo: Regimen
    cubierto: bool
    requiere_copago: bool
    concepto_cargo: ConceptoCargo
    mensaje: str
    sugerencia: str | None = None

    @property
    def bloquea_agendamiento(self) -> bool:
        """Affiliation never blocks scheduling, it only changes the tariff.

        Real clinic behaviour: nobody is turned away at the desk for a lapsed
        affiliation, they are quoted the private rate.
        """
        return False


def validate_afiliacion(
    regimen: Regimen,
    afiliacion_activa: bool,
    *,
    nivel_cuota_moderadora: int = 1,
) -> AfiliacionResult:
    """Resolve the billing consequences of a patient's affiliation status."""
    if regimen is Regimen.PARTICULAR:
        return AfiliacionResult(
            regimen=regimen,
            activa=True,  # a private patient is by definition "active"
            regimen_efectivo=Regimen.PARTICULAR,
            cubierto=False,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje="Private patient: pays the full tariff, no copago and no cuota moderadora.",
        )

    if not afiliacion_activa:
        return AfiliacionResult(
            regimen=regimen,
            activa=False,
            regimen_efectivo=Regimen.PARTICULAR,
            cubierto=False,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje=(
                f"Affiliation to the {regimen} régimen is inactive. "
                "The visit is billed at the private tariff."
            ),
            sugerencia=(
                "Tell the patient they can reactivate their afiliación with their EPS; "
                "meanwhile the private tariff applies."
            ),
        )

    if regimen is Regimen.SOAT:
        return AfiliacionResult(
            regimen=regimen,
            activa=True,
            regimen_efectivo=Regimen.SOAT,
            cubierto=True,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje="SOAT cover is active: care arising from the accident carries no charge.",
        )

    if regimen is Regimen.CONTRIBUTIVO:
        fee = CUOTA_MODERADORA_BY_BRACKET.get(
            nivel_cuota_moderadora, CUOTA_MODERADORA_BY_BRACKET[1]
        )
        return AfiliacionResult(
            regimen=regimen,
            activa=True,
            regimen_efectivo=Regimen.CONTRIBUTIVO,
            cubierto=True,
            requiere_copago=True,
            concepto_cargo=ConceptoCargo.CUOTA_MODERADORA,
            mensaje=(
                f"Contributivo afiliación active. A cuota moderadora of ${fee:,.0f} COP applies."
            ),
        )

    # Regimen.SUBSIDIADO
    return AfiliacionResult(
        regimen=regimen,
        activa=True,
        regimen_efectivo=Regimen.SUBSIDIADO,
        cubierto=True,
        requiere_copago=True,
        concepto_cargo=ConceptoCargo.COPAGO,
        mensaje=(
            "Subsidiado afiliación active. A copago of "
            f"{SUBSIDIADO_COPAGO_RATE:.0%} of the tariff applies."
        ),
    )


def base_tariff(especialidad: str) -> Decimal:
    """Full private tariff for a specialty, in COP."""
    return PRIVATE_TARIFF.get(especialidad, DEFAULT_PRIVATE_TARIFF)
