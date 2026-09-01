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

from backend.enums import ChargeConcept, Regimen

#: Reference tariffs in COP for a standard consultation. Mock 2026 values,
#: illustrative rather than a published fee schedule.
PRIVATE_TARIFF: dict[str, Decimal] = {
    "general_dentistry": Decimal("120000"),
    "orthodontics": Decimal("180000"),
    "endodontics": Decimal("350000"),
    "periodontics": Decimal("200000"),
    "pediatric_dentistry": Decimal("140000"),
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
    """The verdict returned by :func:`validate_afiliacion`."""

    regimen: Regimen
    active: bool
    #: The regime actually billed. A lapsed affiliation falls back to
    #: `particular`: the patient is still attended, at full price.
    effective_regimen: Regimen
    covered: bool
    requires_copago: bool
    charge_concept: ChargeConcept
    message: str
    suggestion: str | None = None

    @property
    def blocks_booking(self) -> bool:
        """Affiliation never blocks scheduling, it only changes the tariff.

        Real clinic behaviour: nobody is turned away at the desk for a lapsed
        affiliation, they are quoted the private rate.
        """
        return False


def validate_afiliacion(
    regimen: Regimen,
    afiliacion_active: bool,
    *,
    cuota_moderadora_level: int = 1,
) -> AfiliacionResult:
    """Resolve the billing consequences of a patient's affiliation status."""
    if regimen is Regimen.PARTICULAR:
        return AfiliacionResult(
            regimen=regimen,
            active=True,  # a private patient is by definition "active"
            effective_regimen=Regimen.PARTICULAR,
            covered=False,
            requires_copago=False,
            charge_concept=ChargeConcept.PARTICULAR,
            message="Private patient: pays the full tariff, no copago and no cuota moderadora.",
        )

    if not afiliacion_active:
        return AfiliacionResult(
            regimen=regimen,
            active=False,
            effective_regimen=Regimen.PARTICULAR,
            covered=False,
            requires_copago=False,
            charge_concept=ChargeConcept.PARTICULAR,
            message=(
                f"Affiliation to the {regimen} régimen is inactive. "
                "The visit is billed at the private tariff."
            ),
            suggestion=(
                "Tell the patient they can reactivate their afiliación with their EPS; "
                "meanwhile the private tariff applies."
            ),
        )

    if regimen is Regimen.SOAT:
        return AfiliacionResult(
            regimen=regimen,
            active=True,
            effective_regimen=Regimen.SOAT,
            covered=True,
            requires_copago=False,
            charge_concept=ChargeConcept.PARTICULAR,
            message="SOAT cover is active: care arising from the accident carries no charge.",
        )

    if regimen is Regimen.CONTRIBUTIVO:
        fee = CUOTA_MODERADORA_BY_BRACKET.get(
            cuota_moderadora_level, CUOTA_MODERADORA_BY_BRACKET[1]
        )
        return AfiliacionResult(
            regimen=regimen,
            active=True,
            effective_regimen=Regimen.CONTRIBUTIVO,
            covered=True,
            requires_copago=True,
            charge_concept=ChargeConcept.CUOTA_MODERADORA,
            message=(
                f"Contributivo afiliación active. A cuota moderadora of ${fee:,.0f} COP applies."
            ),
        )

    # Regimen.SUBSIDIADO
    return AfiliacionResult(
        regimen=regimen,
        active=True,
        effective_regimen=Regimen.SUBSIDIADO,
        covered=True,
        requires_copago=True,
        charge_concept=ChargeConcept.COPAGO,
        message=(
            "Subsidiado afiliación active. A copago of "
            f"{SUBSIDIADO_COPAGO_RATE:.0%} of the tariff applies."
        ),
    )


def base_tariff(specialty: str) -> Decimal:
    """Full private tariff for a specialty, in COP."""
    return PRIVATE_TARIFF.get(specialty, DEFAULT_PRIVATE_TARIFF)
