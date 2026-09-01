"""Accounts receivable tests (§2.3)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.afiliacion import validate_afiliacion
from backend.domain.cartera import (
    AGEING_BUCKETS,
    CarteraPolicy,
    PendingCharge,
    booking_warning,
    charge_for_no_show,
    charge_for_visit,
    summarise_cartera,
)
from backend.enums import CarteraState, ChargeConcept, ChargeState, Regimen

HOY = date(2026, 8, 31)


def charge(
    amount: str,
    days_overdue: int,
    *,
    charge_id: int = 1,
    status: ChargeState = ChargeState.PENDING,
    concept: ChargeConcept = ChargeConcept.COPAGO,
) -> PendingCharge:
    return PendingCharge(
        charge_id=charge_id,
        concept=concept,
        amount=Decimal(amount),
        due_date=HOY - timedelta(days=days_overdue),
        status=status,
    )


# ------------------------------------------------------------ cargo creation


class TestChargeForVisit:
    def test_particular_pays_the_full_tariff(self) -> None:
        afiliacion = validate_afiliacion(Regimen.PARTICULAR, True)
        result = charge_for_visit(afiliacion, "endodontics")
        assert result is not None
        assert result.concept is ChargeConcept.PARTICULAR
        assert result.amount == Decimal("350000")

    def test_soat_creates_no_charge(self) -> None:
        afiliacion = validate_afiliacion(Regimen.SOAT, True)
        assert charge_for_visit(afiliacion, "general_dentistry") is None

    def test_contributivo_pays_the_cuota_moderadora_for_its_level(self) -> None:
        afiliacion = validate_afiliacion(Regimen.CONTRIBUTIVO, True, cuota_moderadora_level=2)
        result = charge_for_visit(afiliacion, "orthodontics", cuota_moderadora_level=2)
        assert result is not None
        assert result.concept is ChargeConcept.CUOTA_MODERADORA
        assert result.amount == Decimal("22000")

    def test_subsidiado_pays_a_percentage_of_the_tariff(self) -> None:
        afiliacion = validate_afiliacion(Regimen.SUBSIDIADO, True)
        result = charge_for_visit(afiliacion, "endodontics")
        assert result is not None
        assert result.concept is ChargeConcept.COPAGO
        assert result.amount == Decimal("35000")  # 10% of 350.000

    def test_inactive_afiliacion_is_billed_as_particular(self) -> None:
        """The regime says 'subsidised' but the charge must be the full tariff."""
        afiliacion = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_active=False)
        result = charge_for_visit(afiliacion, "endodontics")
        assert result is not None
        assert result.concept is ChargeConcept.PARTICULAR
        assert result.amount == Decimal("350000")

    @given(
        regimen=st.sampled_from(list(Regimen)),
        active=st.booleans(),
        specialty=st.sampled_from(
            ["general_dentistry", "orthodontics", "endodontics", "periodontics"]
        ),
    )
    def test_the_amount_is_never_negative(
        self, regimen: Regimen, active: bool, specialty: str
    ) -> None:
        afiliacion = validate_afiliacion(regimen, active)
        result = charge_for_visit(afiliacion, specialty)
        assert result is None or result.amount >= 0


class TestChargeForNoShow:
    def test_the_default_policy_charges_whoever_had_confirmed(self) -> None:
        result = charge_for_no_show(was_confirmed=True)
        assert result is not None
        assert result.concept is ChargeConcept.NO_SHOW
        assert result.amount == Decimal("40000")

    def test_it_does_not_charge_someone_who_never_confirmed(self) -> None:
        assert charge_for_no_show(was_confirmed=False) is None

    def test_a_clinic_can_choose_not_to_charge_no_shows(self) -> None:
        policy = CarteraPolicy(charges_no_show=False)
        assert charge_for_no_show(was_confirmed=True, policy=policy) is None

    def test_a_clinic_can_charge_everyone(self) -> None:
        policy = CarteraPolicy(penalises_only_confirmed=False)
        result = charge_for_no_show(was_confirmed=False, policy=policy)
        assert result is not None

    def test_the_amount_is_configurable(self) -> None:
        policy = CarteraPolicy(no_show_amount=Decimal("75000"))
        result = charge_for_no_show(was_confirmed=True, policy=policy)
        assert result is not None
        assert result.amount == Decimal("75000")


# ------------------------------------------------------------------- summary


class TestCarteraSummary:
    def test_with_no_charges_it_is_al_dia(self) -> None:
        summary = summarise_cartera(1, [], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("0")
        assert summary.charge_count == 0
        assert "up to date" in summary.message

    def test_paid_charges_do_not_count(self) -> None:
        charges = [charge("100000", 60, status=ChargeState.PAID)]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("0")

    def test_voided_charges_do_not_count(self) -> None:
        charges = [charge("100000", 60, status=ChargeState.VOIDED)]
        assert summarise_cartera(1, charges, hoy=HOY).pending_total == Decimal("0")

    def test_a_charge_not_yet_due_leaves_the_cartera_al_dia(self) -> None:
        summary = summarise_cartera(1, [charge("50000", -10)], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("50000")
        assert summary.overdue_total == Decimal("0")
        assert "not yet due" in summary.message

    def test_an_overdue_charge_puts_the_cartera_en_mora(self) -> None:
        summary = summarise_cartera(1, [charge("50000", 45)], hoy=HOY)
        assert summary.status is CarteraState.EN_MORA
        assert summary.overdue_total == Decimal("50000")
        assert summary.max_overdue_days == 45

    def test_a_charge_due_today_is_not_yet_en_mora(self) -> None:
        """Due today means due today, not overdue. Off-by-one lives here."""
        summary = summarise_cartera(1, [charge("10000", 0)], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA

    def test_grace_days_delay_the_mora(self) -> None:
        policy = CarteraPolicy(grace_days=10)
        assert (
            summarise_cartera(1, [charge("10000", 5)], hoy=HOY, policy=policy).status
            is CarteraState.AL_DIA
        )
        assert (
            summarise_cartera(1, [charge("10000", 15)], hoy=HOY, policy=policy).status
            is CarteraState.EN_MORA
        )

    def test_it_takes_the_maximum_days_overdue(self) -> None:
        charges = [charge("1000", 10, charge_id=1), charge("2000", 120, charge_id=2)]
        assert summarise_cartera(1, charges, hoy=HOY).max_overdue_days == 120

    def test_it_adds_several_charges_correctly(self) -> None:
        charges = [
            charge("15000", 5, charge_id=1),
            charge("25000", 40, charge_id=2),
            charge("60000", -3, charge_id=3),
        ]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert summary.pending_total == Decimal("100000")
        assert summary.overdue_total == Decimal("40000")
        assert summary.charge_count == 3


class TestAgeing:
    def test_the_buckets_are_exhaustive_and_do_not_overlap(self) -> None:
        names = [name for name, _, _ in AGEING_BUCKETS]
        assert len(names) == len(set(names))
        # every day between -365 and 365 must land in exactly one bucket
        for days in range(-365, 366):
            matches = [n for n, d, h in AGEING_BUCKETS if days >= d and (h is None or days <= h)]
            assert len(matches) == 1, f"{days} fell into {matches}"

    @pytest.mark.parametrize(
        ("days", "bucket"),
        [
            (-5, "corriente"),
            (0, "corriente"),
            (1, "1_30"),
            (30, "1_30"),
            (31, "31_60"),
            (60, "31_60"),
            (61, "61_90"),
            (90, "61_90"),
            (91, "mas_90"),
            (400, "mas_90"),
        ],
    )
    def test_every_charge_falls_in_its_bucket(self, days: int, bucket: str) -> None:
        summary = summarise_cartera(1, [charge("10000", days)], hoy=HOY)
        assert summary.ageing[bucket] == Decimal("10000")

    def test_the_ageing_adds_up_to_the_pending_total(self) -> None:
        charges = [
            charge("10000", -5, charge_id=1),
            charge("20000", 15, charge_id=2),
            charge("30000", 45, charge_id=3),
            charge("40000", 200, charge_id=4),
        ]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert sum(summary.ageing.values()) == summary.pending_total


class TestAlertWhenBooking:
    def test_cartera_al_dia_does_not_alert(self) -> None:
        assert booking_warning(summarise_cartera(1, [], hoy=HOY)) is None

    def test_mora_below_the_threshold_does_not_alert(self) -> None:
        summary = summarise_cartera(1, [charge("20000", 40)], hoy=HOY)
        assert summary.status is CarteraState.EN_MORA
        assert booking_warning(summary) is None

    def test_mora_above_the_threshold_alerts_but_does_not_block(self) -> None:
        summary = summarise_cartera(1, [charge("150000", 70)], hoy=HOY)
        alerta = booking_warning(summary)
        assert alerta is not None
        assert "Heads-up" in alerta
        # The wording must make clear the appointment still goes through: the
        # spec is explicit that debt warns, it does not block.
        assert "can still be booked" in alerta

    def test_the_threshold_is_configurable(self) -> None:
        policy = CarteraPolicy(overdue_alert_threshold=Decimal("10000"))
        summary = summarise_cartera(1, [charge("20000", 40)], hoy=HOY, policy=policy)
        assert booking_warning(summary) is not None


class TestProperties:
    amounts = st.decimals(min_value=0, max_value=5_000_000, places=0)

    @given(
        amounts=st.lists(amounts, min_size=0, max_size=15),
        desfases=st.lists(st.integers(min_value=-90, max_value=400), min_size=0, max_size=15),
    )
    def test_summary_invariants(self, amounts: list[Decimal], desfases: list[int]) -> None:
        charges = [
            charge(str(m), d, charge_id=i)
            for i, (m, d) in enumerate(zip(amounts, desfases, strict=False))
        ]
        summary = summarise_cartera(7, charges, hoy=HOY)
        assert summary.overdue_total <= summary.pending_total
        assert summary.pending_total >= 0
        assert summary.max_overdue_days >= 0
        assert summary.charge_count == len(charges)
        assert sum(summary.ageing.values()) == summary.pending_total
        # A zero-amount overdue charge still counts as arrears, so compare
        # against the actual overdue set rather than against the total.
        hay_vencidos = any(c.days_overdue(HOY) > 0 for c in charges)
        assert (summary.status is CarteraState.EN_MORA) is hay_vencidos
        assert summary.patient_id == 7
