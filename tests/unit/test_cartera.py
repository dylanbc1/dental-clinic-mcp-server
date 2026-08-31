"""Accounts receivable tests (§2.3)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.afiliacion import validar_afiliacion
from backend.domain.cartera import (
    BUCKETS_ANTIGUEDAD,
    CargoPendiente,
    PoliticaCartera,
    alerta_al_agendar,
    calcular_cargo_por_atencion,
    calcular_cargo_por_no_show,
    resumir_cartera,
)
from backend.enums import ConceptoCargo, EstadoCargo, EstadoCartera, Regimen

HOY = date(2026, 8, 31)


def cargo(
    monto: str,
    dias_vencido: int,
    *,
    cargo_id: int = 1,
    estado: EstadoCargo = EstadoCargo.PENDIENTE,
    concepto: ConceptoCargo = ConceptoCargo.COPAGO,
) -> CargoPendiente:
    return CargoPendiente(
        cargo_id=cargo_id,
        concepto=concepto,
        monto=Decimal(monto),
        vencimiento=HOY - timedelta(days=dias_vencido),
        estado=estado,
    )


# ------------------------------------------------------------ cargo creation


class TestCargoPorAtencion:
    def test_particular_paga_la_tarifa_completa(self) -> None:
        afiliacion = validar_afiliacion(Regimen.PARTICULAR, True)
        resultado = calcular_cargo_por_atencion(afiliacion, "endodoncia")
        assert resultado is not None
        assert resultado.concepto is ConceptoCargo.PARTICULAR
        assert resultado.monto == Decimal("350000")

    def test_soat_no_genera_cargo(self) -> None:
        afiliacion = validar_afiliacion(Regimen.SOAT, True)
        assert calcular_cargo_por_atencion(afiliacion, "odontologia_general") is None

    def test_contributivo_paga_cuota_moderadora_del_nivel(self) -> None:
        afiliacion = validar_afiliacion(Regimen.CONTRIBUTIVO, True, nivel_cuota_moderadora=2)
        resultado = calcular_cargo_por_atencion(afiliacion, "ortodoncia", nivel_cuota_moderadora=2)
        assert resultado is not None
        assert resultado.concepto is ConceptoCargo.CUOTA_MODERADORA
        assert resultado.monto == Decimal("22000")

    def test_subsidiado_paga_un_porcentaje_de_la_tarifa(self) -> None:
        afiliacion = validar_afiliacion(Regimen.SUBSIDIADO, True)
        resultado = calcular_cargo_por_atencion(afiliacion, "endodoncia")
        assert resultado is not None
        assert resultado.concepto is ConceptoCargo.COPAGO
        assert resultado.monto == Decimal("35000")  # 10% of 350.000

    def test_afiliacion_inactiva_se_liquida_como_particular(self) -> None:
        """The regime says 'subsidised' but the charge must be the full tariff."""
        afiliacion = validar_afiliacion(Regimen.SUBSIDIADO, afiliacion_activa=False)
        resultado = calcular_cargo_por_atencion(afiliacion, "endodoncia")
        assert resultado is not None
        assert resultado.concepto is ConceptoCargo.PARTICULAR
        assert resultado.monto == Decimal("350000")

    @given(
        regimen=st.sampled_from(list(Regimen)),
        activa=st.booleans(),
        especialidad=st.sampled_from(
            ["odontologia_general", "ortodoncia", "endodoncia", "periodoncia"]
        ),
    )
    def test_el_monto_nunca_es_negativo(
        self, regimen: Regimen, activa: bool, especialidad: str
    ) -> None:
        afiliacion = validar_afiliacion(regimen, activa)
        resultado = calcular_cargo_por_atencion(afiliacion, especialidad)
        assert resultado is None or resultado.monto >= 0


class TestCargoPorNoShow:
    def test_politica_por_defecto_cobra_a_quien_habia_confirmado(self) -> None:
        resultado = calcular_cargo_por_no_show(estaba_confirmada=True)
        assert resultado is not None
        assert resultado.concepto is ConceptoCargo.NO_SHOW
        assert resultado.monto == Decimal("40000")

    def test_no_cobra_a_quien_nunca_confirmo(self) -> None:
        assert calcular_cargo_por_no_show(estaba_confirmada=False) is None

    def test_una_clinica_puede_no_cobrar_no_shows(self) -> None:
        politica = PoliticaCartera(cobra_no_show=False)
        assert calcular_cargo_por_no_show(estaba_confirmada=True, politica=politica) is None

    def test_una_clinica_puede_cobrar_a_todos(self) -> None:
        politica = PoliticaCartera(penaliza_solo_confirmadas=False)
        resultado = calcular_cargo_por_no_show(estaba_confirmada=False, politica=politica)
        assert resultado is not None

    def test_el_monto_es_configurable(self) -> None:
        politica = PoliticaCartera(monto_no_show=Decimal("75000"))
        resultado = calcular_cargo_por_no_show(estaba_confirmada=True, politica=politica)
        assert resultado is not None
        assert resultado.monto == Decimal("75000")


# ------------------------------------------------------------------- summary


class TestResumenCartera:
    def test_sin_cargos_esta_al_dia(self) -> None:
        resumen = resumir_cartera(1, [], hoy=HOY)
        assert resumen.estado is EstadoCartera.AL_DIA
        assert resumen.total_pendiente == Decimal("0")
        assert resumen.cantidad_cargos == 0
        assert "al día" in resumen.mensaje

    def test_los_cargos_pagados_no_cuentan(self) -> None:
        cargos = [cargo("100000", 60, estado=EstadoCargo.PAGADO)]
        resumen = resumir_cartera(1, cargos, hoy=HOY)
        assert resumen.estado is EstadoCartera.AL_DIA
        assert resumen.total_pendiente == Decimal("0")

    def test_los_cargos_anulados_no_cuentan(self) -> None:
        cargos = [cargo("100000", 60, estado=EstadoCargo.ANULADO)]
        assert resumir_cartera(1, cargos, hoy=HOY).total_pendiente == Decimal("0")

    def test_un_cargo_no_vencido_deja_la_cartera_al_dia(self) -> None:
        resumen = resumir_cartera(1, [cargo("50000", -10)], hoy=HOY)
        assert resumen.estado is EstadoCartera.AL_DIA
        assert resumen.total_pendiente == Decimal("50000")
        assert resumen.total_vencido == Decimal("0")
        assert "por vencer" in resumen.mensaje

    def test_un_cargo_vencido_pone_la_cartera_en_mora(self) -> None:
        resumen = resumir_cartera(1, [cargo("50000", 45)], hoy=HOY)
        assert resumen.estado is EstadoCartera.EN_MORA
        assert resumen.total_vencido == Decimal("50000")
        assert resumen.dias_mora_maximo == 45

    def test_el_vencimiento_de_hoy_todavia_no_es_mora(self) -> None:
        """Due today means due today, not overdue. Off-by-one lives here."""
        resumen = resumir_cartera(1, [cargo("10000", 0)], hoy=HOY)
        assert resumen.estado is EstadoCartera.AL_DIA

    def test_dias_gracia_retrasa_la_mora(self) -> None:
        politica = PoliticaCartera(dias_gracia=10)
        assert (
            resumir_cartera(1, [cargo("10000", 5)], hoy=HOY, politica=politica).estado
            is EstadoCartera.AL_DIA
        )
        assert (
            resumir_cartera(1, [cargo("10000", 15)], hoy=HOY, politica=politica).estado
            is EstadoCartera.EN_MORA
        )

    def test_toma_el_maximo_de_dias_de_mora(self) -> None:
        cargos = [cargo("1000", 10, cargo_id=1), cargo("2000", 120, cargo_id=2)]
        assert resumir_cartera(1, cargos, hoy=HOY).dias_mora_maximo == 120

    def test_suma_correctamente_varios_cargos(self) -> None:
        cargos = [
            cargo("15000", 5, cargo_id=1),
            cargo("25000", 40, cargo_id=2),
            cargo("60000", -3, cargo_id=3),
        ]
        resumen = resumir_cartera(1, cargos, hoy=HOY)
        assert resumen.total_pendiente == Decimal("100000")
        assert resumen.total_vencido == Decimal("40000")
        assert resumen.cantidad_cargos == 3


class TestAntiguedad:
    def test_los_buckets_son_exhaustivos_y_no_se_solapan(self) -> None:
        nombres = [nombre for nombre, _, _ in BUCKETS_ANTIGUEDAD]
        assert len(nombres) == len(set(nombres))
        # every day between -365 and 365 must land in exactly one bucket
        for dias in range(-365, 366):
            coincidencias = [
                n for n, d, h in BUCKETS_ANTIGUEDAD if dias >= d and (h is None or dias <= h)
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
        resumen = resumir_cartera(1, [cargo("10000", dias)], hoy=HOY)
        assert resumen.antiguedad[bucket] == Decimal("10000")

    def test_la_antiguedad_suma_el_total_pendiente(self) -> None:
        cargos = [
            cargo("10000", -5, cargo_id=1),
            cargo("20000", 15, cargo_id=2),
            cargo("30000", 45, cargo_id=3),
            cargo("40000", 200, cargo_id=4),
        ]
        resumen = resumir_cartera(1, cargos, hoy=HOY)
        assert sum(resumen.antiguedad.values()) == resumen.total_pendiente


class TestAlertaAlAgendar:
    def test_cartera_al_dia_no_alerta(self) -> None:
        assert alerta_al_agendar(resumir_cartera(1, [], hoy=HOY)) is None

    def test_mora_bajo_el_umbral_no_alerta(self) -> None:
        resumen = resumir_cartera(1, [cargo("20000", 40)], hoy=HOY)
        assert resumen.estado is EstadoCartera.EN_MORA
        assert alerta_al_agendar(resumen) is None

    def test_mora_sobre_el_umbral_alerta_pero_no_bloquea(self) -> None:
        resumen = resumir_cartera(1, [cargo("150000", 70)], hoy=HOY)
        alerta = alerta_al_agendar(resumen)
        assert alerta is not None
        assert "Aviso" in alerta
        # The wording must make clear the appointment still goes through: the
        # spec is explicit that debt warns, it does not block.
        assert "se puede agendar" in alerta

    def test_el_umbral_es_configurable(self) -> None:
        politica = PoliticaCartera(umbral_alerta_mora=Decimal("10000"))
        resumen = resumir_cartera(1, [cargo("20000", 40)], hoy=HOY, politica=politica)
        assert alerta_al_agendar(resumen) is not None


class TestPropiedades:
    montos = st.decimals(min_value=0, max_value=5_000_000, places=0)

    @given(
        montos=st.lists(montos, min_size=0, max_size=15),
        desfases=st.lists(st.integers(min_value=-90, max_value=400), min_size=0, max_size=15),
    )
    def test_invariantes_del_resumen(self, montos: list[Decimal], desfases: list[int]) -> None:
        cargos = [
            cargo(str(m), d, cargo_id=i)
            for i, (m, d) in enumerate(zip(montos, desfases, strict=False))
        ]
        resumen = resumir_cartera(7, cargos, hoy=HOY)
        assert resumen.total_vencido <= resumen.total_pendiente
        assert resumen.total_pendiente >= 0
        assert resumen.dias_mora_maximo >= 0
        assert resumen.cantidad_cargos == len(cargos)
        assert sum(resumen.antiguedad.values()) == resumen.total_pendiente
        # A zero-amount overdue charge still counts as arrears, so compare
        # against the actual overdue set rather than against the total.
        hay_vencidos = any(c.dias_vencidos(HOY) > 0 for c in cargos)
        assert (resumen.estado is EstadoCartera.EN_MORA) is hay_vencidos
        assert resumen.paciente_id == 7
