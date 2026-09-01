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


class TestCargoPorAtencion:
    def test_particular_paga_la_tarifa_completa(self) -> None:
        afiliacion = validate_afiliacion(Regimen.PARTICULAR, True)
        result = charge_for_visit(afiliacion, "endodontics")
        assert result is not None
        assert result.concept is ChargeConcept.PARTICULAR
        assert result.amount == Decimal("350000")

    def test_soat_no_genera_cargo(self) -> None:
        afiliacion = validate_afiliacion(Regimen.SOAT, True)
        assert charge_for_visit(afiliacion, "general_dentistry") is None

    def test_contributivo_paga_cuota_moderadora_del_nivel(self) -> None:
        afiliacion = validate_afiliacion(Regimen.CONTRIBUTIVO, True, cuota_moderadora_level=2)
        result = charge_for_visit(afiliacion, "orthodontics", cuota_moderadora_level=2)
        assert result is not None
        assert result.concept is ChargeConcept.CUOTA_MODERADORA
        assert result.amount == Decimal("22000")

    def test_subsidiado_paga_un_porcentaje_de_la_tarifa(self) -> None:
        afiliacion = validate_afiliacion(Regimen.SUBSIDIADO, True)
        result = charge_for_visit(afiliacion, "endodontics")
        assert result is not None
        assert result.concept is ChargeConcept.COPAGO
        assert result.amount == Decimal("35000")  # 10% of 350.000

    def test_afiliacion_inactiva_se_liquida_como_particular(self) -> None:
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
    def test_el_monto_nunca_es_negativo(
        self, regimen: Regimen, active: bool, specialty: str
    ) -> None:
        afiliacion = validate_afiliacion(regimen, active)
        result = charge_for_visit(afiliacion, specialty)
        assert result is None or result.amount >= 0


class TestCargoPorNoShow:
    def test_politica_por_defecto_cobra_a_quien_habia_confirmado(self) -> None:
        result = charge_for_no_show(was_confirmed=True)
        assert result is not None
        assert result.concept is ChargeConcept.NO_SHOW
        assert result.amount == Decimal("40000")

    def test_no_cobra_a_quien_nunca_confirmo(self) -> None:
        assert charge_for_no_show(was_confirmed=False) is None

    def test_una_clinica_puede_no_cobrar_no_shows(self) -> None:
        policy = CarteraPolicy(charges_no_show=False)
        assert charge_for_no_show(was_confirmed=True, policy=policy) is None

    def test_una_clinica_puede_cobrar_a_todos(self) -> None:
        policy = CarteraPolicy(penalises_only_confirmed=False)
        result = charge_for_no_show(was_confirmed=False, policy=policy)
        assert result is not None

    def test_el_monto_es_configurable(self) -> None:
        policy = CarteraPolicy(no_show_amount=Decimal("75000"))
        result = charge_for_no_show(was_confirmed=True, policy=policy)
        assert result is not None
        assert result.amount == Decimal("75000")


# ------------------------------------------------------------------- summary


class TestResumenCartera:
    def test_sin_cargos_esta_al_dia(self) -> None:
        summary = summarise_cartera(1, [], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("0")
        assert summary.charge_count == 0
        assert "up to date" in summary.message

    def test_los_cargos_pagados_no_cuentan(self) -> None:
        charges = [charge("100000", 60, status=ChargeState.PAID)]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("0")

    def test_los_cargos_anulados_no_cuentan(self) -> None:
        charges = [charge("100000", 60, status=ChargeState.VOIDED)]
        assert summarise_cartera(1, charges, hoy=HOY).pending_total == Decimal("0")

    def test_un_cargo_no_vencido_deja_la_cartera_al_dia(self) -> None:
        summary = summarise_cartera(1, [charge("50000", -10)], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA
        assert summary.pending_total == Decimal("50000")
        assert summary.overdue_total == Decimal("0")
        assert "not yet due" in summary.message

    def test_un_cargo_vencido_pone_la_cartera_en_mora(self) -> None:
        summary = summarise_cartera(1, [charge("50000", 45)], hoy=HOY)
        assert summary.status is CarteraState.EN_MORA
        assert summary.overdue_total == Decimal("50000")
        assert summary.max_overdue_days == 45

    def test_el_vencimiento_de_hoy_todavia_no_es_mora(self) -> None:
        """Due today means due today, not overdue. Off-by-one lives here."""
        summary = summarise_cartera(1, [charge("10000", 0)], hoy=HOY)
        assert summary.status is CarteraState.AL_DIA

    def test_dias_gracia_retrasa_la_mora(self) -> None:
        policy = CarteraPolicy(grace_days=10)
        assert (
            summarise_cartera(1, [charge("10000", 5)], hoy=HOY, policy=policy).status
            is CarteraState.AL_DIA
        )
        assert (
            summarise_cartera(1, [charge("10000", 15)], hoy=HOY, policy=policy).status
            is CarteraState.EN_MORA
        )

    def test_toma_el_maximo_de_dias_de_mora(self) -> None:
        charges = [charge("1000", 10, charge_id=1), charge("2000", 120, charge_id=2)]
        assert summarise_cartera(1, charges, hoy=HOY).max_overdue_days == 120

    def test_suma_correctamente_varios_cargos(self) -> None:
        charges = [
            charge("15000", 5, charge_id=1),
            charge("25000", 40, charge_id=2),
            charge("60000", -3, charge_id=3),
        ]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert summary.pending_total == Decimal("100000")
        assert summary.overdue_total == Decimal("40000")
        assert summary.charge_count == 3


class TestAntiguedad:
    def test_los_buckets_son_exhaustivos_y_no_se_solapan(self) -> None:
        names = [name for name, _, _ in AGEING_BUCKETS]
        assert len(names) == len(set(names))
        # every day between -365 and 365 must land in exactly one bucket
        for dias in range(-365, 366):
            coincidencias = [
                n for n, d, h in AGEING_BUCKETS if dias >= d and (h is None or dias <= h)
            ]
            assert len(coincidencias) == 1, f"{dias} cayó en {coincidencias}"

    @pytest.mark.parametrize(
        ("dias", "bucket"),
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
    def test_cada_cargo_cae_en_su_bucket(self, dias: int, bucket: str) -> None:
        summary = summarise_cartera(1, [charge("10000", dias)], hoy=HOY)
        assert summary.ageing[bucket] == Decimal("10000")

    def test_la_antiguedad_suma_el_total_pendiente(self) -> None:
        charges = [
            charge("10000", -5, charge_id=1),
            charge("20000", 15, charge_id=2),
            charge("30000", 45, charge_id=3),
            charge("40000", 200, charge_id=4),
        ]
        summary = summarise_cartera(1, charges, hoy=HOY)
        assert sum(summary.ageing.values()) == summary.pending_total


class TestAlertaAlAgendar:
    def test_cartera_al_dia_no_alerta(self) -> None:
        assert booking_warning(summarise_cartera(1, [], hoy=HOY)) is None

    def test_mora_bajo_el_umbral_no_alerta(self) -> None:
        summary = summarise_cartera(1, [charge("20000", 40)], hoy=HOY)
        assert summary.status is CarteraState.EN_MORA
        assert booking_warning(summary) is None

    def test_mora_sobre_el_umbral_alerta_pero_no_bloquea(self) -> None:
        summary = summarise_cartera(1, [charge("150000", 70)], hoy=HOY)
        alerta = booking_warning(summary)
        assert alerta is not None
        assert "Heads-up" in alerta
        # The wording must make clear the appointment still goes through: the
        # spec is explicit that debt warns, it does not block.
        assert "can still be booked" in alerta

    def test_el_umbral_es_configurable(self) -> None:
        policy = CarteraPolicy(overdue_alert_threshold=Decimal("10000"))
        summary = summarise_cartera(1, [charge("20000", 40)], hoy=HOY, policy=policy)
        assert booking_warning(summary) is not None


class TestPropiedades:
    montos = st.decimals(min_value=0, max_value=5_000_000, places=0)

    @given(
        montos=st.lists(montos, min_size=0, max_size=15),
        desfases=st.lists(st.integers(min_value=-90, max_value=400), min_size=0, max_size=15),
    )
    def test_invariantes_del_resumen(self, montos: list[Decimal], desfases: list[int]) -> None:
        charges = [
            charge(str(m), d, charge_id=i)
            for i, (m, d) in enumerate(zip(montos, desfases, strict=False))
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
