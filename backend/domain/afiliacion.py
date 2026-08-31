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
TARIFA_PARTICULAR: dict[str, Decimal] = {
    "odontologia_general": Decimal("120000"),
    "ortodoncia": Decimal("180000"),
    "endodoncia": Decimal("350000"),
    "periodoncia": Decimal("200000"),
    "odontopediatria": Decimal("140000"),
}
TARIFA_PARTICULAR_DEFECTO = Decimal("120000")

#: Contributory pays a cuota moderadora, a fixed fee bracketed by income.
#: Subsidised pays a copago proportional to the service. SOAT covers it fully.
CUOTA_MODERADORA_POR_NIVEL: dict[int, Decimal] = {
    1: Decimal("5500"),
    2: Decimal("22000"),
    3: Decimal("57500"),
}
PORCENTAJE_COPAGO_SUBSIDIADO = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class ResultadoAfiliacion:
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


def validar_afiliacion(
    regimen: Regimen,
    afiliacion_activa: bool,
    *,
    nivel_cuota_moderadora: int = 1,
) -> ResultadoAfiliacion:
    """Resolve the billing consequences of a patient's affiliation status."""
    if regimen is Regimen.PARTICULAR:
        return ResultadoAfiliacion(
            regimen=regimen,
            activa=True,  # a private patient is by definition "active"
            regimen_efectivo=Regimen.PARTICULAR,
            cubierto=False,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje="Paciente particular: paga tarifa plena, sin copago ni cuota moderadora.",
        )

    if not afiliacion_activa:
        return ResultadoAfiliacion(
            regimen=regimen,
            activa=False,
            regimen_efectivo=Regimen.PARTICULAR,
            cubierto=False,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje=(
                f"La afiliación al régimen {regimen} figura inactiva. "
                "La atención se liquida a tarifa particular."
            ),
            sugerencia=(
                "Informa al paciente que puede reactivar su afiliación ante su EPS; "
                "entre tanto se cobra tarifa particular."
            ),
        )

    if regimen is Regimen.SOAT:
        return ResultadoAfiliacion(
            regimen=regimen,
            activa=True,
            regimen_efectivo=Regimen.SOAT,
            cubierto=True,
            requiere_copago=False,
            concepto_cargo=ConceptoCargo.PARTICULAR,
            mensaje="Cobertura SOAT vigente: la atención derivada del accidente no genera cobro.",
        )

    if regimen is Regimen.CONTRIBUTIVO:
        cuota = CUOTA_MODERADORA_POR_NIVEL.get(
            nivel_cuota_moderadora, CUOTA_MODERADORA_POR_NIVEL[1]
        )
        return ResultadoAfiliacion(
            regimen=regimen,
            activa=True,
            regimen_efectivo=Regimen.CONTRIBUTIVO,
            cubierto=True,
            requiere_copago=True,
            concepto_cargo=ConceptoCargo.CUOTA_MODERADORA,
            mensaje=(
                f"Afiliación contributiva activa. Aplica cuota moderadora de ${cuota:,.0f} COP."
            ),
        )

    # Regimen.SUBSIDIADO
    return ResultadoAfiliacion(
        regimen=regimen,
        activa=True,
        regimen_efectivo=Regimen.SUBSIDIADO,
        cubierto=True,
        requiere_copago=True,
        concepto_cargo=ConceptoCargo.COPAGO,
        mensaje=(
            "Afiliación subsidiada activa. Aplica copago del "
            f"{PORCENTAJE_COPAGO_SUBSIDIADO:.0%} sobre la tarifa."
        ),
    )


def tarifa_base(especialidad: str) -> Decimal:
    """Full private tariff for a specialty, in COP."""
    return TARIFA_PARTICULAR.get(especialidad, TARIFA_PARTICULAR_DEFECTO)
