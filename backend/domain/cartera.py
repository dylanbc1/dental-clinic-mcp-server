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

from backend.domain.affiliation import (
    CUOTA_MODERADORA_BY_BRACKET,
    SUBSIDIADO_COPAGO_RATE,
    AffiliationResult,
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

    charges_no_show: bool = True
    no_show_amount: Decimal = Decimal("40000")
    #: Days before a charge counts as in arrears.
    grace_days: int = 0
    #: Debt above this amount raises a warning when scheduling. It never blocks.
    overdue_alert_threshold: Decimal = Decimal("50000")
    #: Only a patient who had *confirmed* is penalised for not showing up.
    penalises_only_confirmed: bool = True


DEFAULT_POLICY = CarteraPolicy()


@dataclass(frozen=True, slots=True)
class CalculatedCharge:
    """A charge the domain decided to create, before it is persisted."""

    concept: ChargeConcept
    amount: Decimal
    description: str


@dataclass(frozen=True, slots=True)
class PendingCharge:
    """Read model of a single outstanding charge."""

    charge_id: int
    concept: ChargeConcept
    amount: Decimal
    due_date: date
    status: ChargeState

    def days_overdue(self, hoy: date) -> int:
        """Positive when overdue, negative when it is not due yet."""
        return (hoy - self.due_date).days


@dataclass(frozen=True, slots=True)
class CarteraSummary:
    """What `check_cartera` returns and what a collections agent acts on."""

    patient_id: int
    status: CarteraState
    pending_total: Decimal
    overdue_total: Decimal
    max_overdue_days: int
    charge_count: int
    ageing: dict[str, Decimal] = field(default_factory=dict)
    above_alert_threshold: bool = False
    message: str = ""


def _round(amount: Decimal) -> Decimal:
    """COP has no cents in practice; round to the peso."""
    return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def charge_for_visit(
    affiliation: AffiliationResult,
    specialty: str,
    *,
    cuota_moderadora_level: int = 1,
) -> CalculatedCharge | None:
    """Charge produced when an appointment reaches `atendida`.

    ``None`` when the service is fully covered (SOAT). That is a legitimate
    outcome, not an error.
    """
    tariff = base_tariff(specialty)

    if affiliation.effective_regimen is Regimen.SOAT:
        return None

    if affiliation.effective_regimen is Regimen.PARTICULAR:
        return CalculatedCharge(
            concept=ChargeConcept.PARTICULAR,
            amount=_round(tariff),
            description=f"Private tariff · {specialty}",
        )

    if affiliation.effective_regimen is Regimen.CONTRIBUTIVO:
        fee = CUOTA_MODERADORA_BY_BRACKET.get(
            cuota_moderadora_level, CUOTA_MODERADORA_BY_BRACKET[1]
        )
        return CalculatedCharge(
            concept=ChargeConcept.CUOTA_MODERADORA,
            amount=_round(fee),
            description=f"Cuota moderadora · {specialty}",
        )

    # Subsidised regime: percentage copayment over the reference tariff.
    return CalculatedCharge(
        concept=ChargeConcept.COPAGO,
        amount=_round(tariff * SUBSIDIADO_COPAGO_RATE),
        description=f"Copago, subsidiado régimen · {specialty}",
    )


def charge_for_no_show(
    *,
    was_confirmed: bool,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CalculatedCharge | None:
    """Penalty charge for a missed appointment, if the policy enables it."""
    if not policy.charges_no_show:
        return None
    if policy.penalises_only_confirmed and not was_confirmed:
        return None
    return CalculatedCharge(
        concept=ChargeConcept.NO_SHOW,
        amount=_round(policy.no_show_amount),
        description="No-show penalty, no prior cancellation",
    )


def _bucket(days: int) -> str:
    for name, since, until in AGEING_BUCKETS:
        if days >= since and (until is None or days <= until):
            return name
    return "corriente"


def summarise_cartera(
    patient_id: int,
    charges: list[PendingCharge],
    *,
    hoy: date,
    policy: CarteraPolicy = DEFAULT_POLICY,
) -> CarteraSummary:
    """Aggregate a patient's outstanding charges into a collections view."""
    outstanding = [c for c in charges if c.status is ChargeState.PENDING]

    pending_total = _round(sum((c.amount for c in outstanding), Decimal("0")))
    overdue = [c for c in outstanding if c.days_overdue(hoy) > policy.grace_days]
    overdue_total = _round(sum((c.amount for c in overdue), Decimal("0")))
    max_overdue_days = max((c.days_overdue(hoy) for c in overdue), default=0)

    ageing: dict[str, Decimal] = {name: Decimal("0") for name, _, _ in AGEING_BUCKETS}
    for charge in outstanding:
        ageing[_bucket(charge.days_overdue(hoy))] += charge.amount
    ageing = {k: _round(v) for k, v in ageing.items()}

    status = CarteraState.EN_MORA if overdue else CarteraState.AL_DIA
    above_threshold = overdue_total >= policy.overdue_alert_threshold

    if status is CarteraState.AL_DIA:
        message = (
            "Cartera up to date."
            if not outstanding
            else f"Cartera up to date. ${pending_total:,.0f} COP not yet due."
        )
    else:
        message = (
            f"Cartera in arrears: ${overdue_total:,.0f} COP overdue, "
            f"up to {max_overdue_days} days late."
        )
        if above_threshold:
            message += " Above the clinic's alert threshold."

    return CarteraSummary(
        patient_id=patient_id,
        status=status,
        pending_total=pending_total,
        overdue_total=overdue_total,
        max_overdue_days=max_overdue_days,
        charge_count=len(outstanding),
        ageing=ageing,
        above_alert_threshold=above_threshold,
        message=message,
    )


def booking_warning(summary: CarteraSummary) -> str | None:
    """Warning shown when scheduling a patient in arrears, never a block.

    Returning a string rather than raising is the point: the tool layer passes
    it to the model as context and the appointment still goes through.
    """
    if summary.status is CarteraState.AL_DIA or not summary.above_alert_threshold:
        return None
    return (
        f"Heads-up: the patient has ${summary.overdue_total:,.0f} COP of overdue "
        f"cartera ({summary.max_overdue_days} days). The appointment can still be "
        "booked; tell the patient about the outstanding balance when confirming."
    )
