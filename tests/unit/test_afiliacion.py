"""Affiliation validation tests (§2.2).

The behaviour under test is the one a clinic actually exhibits: affiliation
never turns a patient away, it changes what they pay.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.afiliacion import (
    CUOTA_MODERADORA_BY_BRACKET,
    DEFAULT_PRIVATE_TARIFF,
    PRIVATE_TARIFF,
    base_tariff,
    validate_afiliacion,
)
from backend.enums import ChargeConcept, Regimen, Specialty


class TestRegimenContributivo:
    def test_activo_aplica_cuota_moderadora(self) -> None:
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_activa=True)
        assert r.activa
        assert r.cubierto
        assert r.requiere_copago
        assert r.concepto_cargo is ChargeConcept.CUOTA_MODERADORA
        assert r.regimen_efectivo is Regimen.CONTRIBUTIVO

    @pytest.mark.parametrize("nivel", [1, 2, 3])
    def test_el_nivel_cambia_el_monto_informado(self, nivel: int) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_activa=True, nivel_cuota_moderadora=nivel
        )
        esperado = CUOTA_MODERADORA_BY_BRACKET[nivel]
        assert f"{esperado:,.0f}" in r.mensaje

    def test_nivel_desconocido_cae_al_nivel_1(self) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_activa=True, nivel_cuota_moderadora=99
        )
        assert f"{CUOTA_MODERADORA_BY_BRACKET[1]:,.0f}" in r.mensaje


class TestRegimenSubsidiado:
    def test_activo_aplica_copago(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_activa=True)
        assert r.concepto_cargo is ChargeConcept.COPAGO
        assert r.requiere_copago
        assert r.cubierto


class TestParticular:
    def test_nunca_tiene_copago(self) -> None:
        r = validate_afiliacion(Regimen.PARTICULAR, afiliacion_activa=True)
        assert not r.requiere_copago
        assert not r.cubierto
        assert r.concepto_cargo is ChargeConcept.PARTICULAR

    def test_el_flag_de_afiliacion_es_irrelevante_para_un_particular(self) -> None:
        """A private patient has nothing to be affiliated to."""
        activo = validate_afiliacion(Regimen.PARTICULAR, afiliacion_activa=True)
        inactivo = validate_afiliacion(Regimen.PARTICULAR, afiliacion_activa=False)
        assert activo == inactivo
        assert inactivo.activa is True


class TestSoat:
    def test_cubre_totalmente_y_no_genera_copago(self) -> None:
        r = validate_afiliacion(Regimen.SOAT, afiliacion_activa=True)
        assert r.cubierto
        assert not r.requiere_copago
        assert r.regimen_efectivo is Regimen.SOAT


class TestAfiliacionInactiva:
    @pytest.mark.parametrize("regimen", [Regimen.CONTRIBUTIVO, Regimen.SUBSIDIADO, Regimen.SOAT])
    def test_cae_a_tarifa_particular(self, regimen: Regimen) -> None:
        r = validate_afiliacion(regimen, afiliacion_activa=False)
        assert not r.activa
        assert r.regimen_efectivo is Regimen.PARTICULAR
        assert r.concepto_cargo is ChargeConcept.PARTICULAR
        assert not r.cubierto
        assert not r.requiere_copago

    def test_conserva_el_regimen_original_para_informar(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_activa=False)
        assert r.regimen is Regimen.SUBSIDIADO
        assert "is inactive" in r.mensaje

    def test_da_una_sugerencia_accionable(self) -> None:
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_activa=False)
        assert r.sugerencia is not None
        assert "EPS" in r.sugerencia


class TestNoBloqueaAgendamiento:
    """The rule that separates this from a naive implementation."""

    @given(
        regimen=st.sampled_from(list(Regimen)),
        activa=st.booleans(),
        nivel=st.integers(min_value=1, max_value=3),
    )
    def test_ninguna_combinacion_impide_agendar(
        self, regimen: Regimen, activa: bool, nivel: int
    ) -> None:
        r = validate_afiliacion(regimen, activa, nivel_cuota_moderadora=nivel)
        assert r.bloquea_agendamiento is False


class TestTarifas:
    @pytest.mark.parametrize("especialidad", list(Specialty))
    def test_toda_especialidad_tiene_tarifa(self, especialidad: Specialty) -> None:
        assert str(especialidad) in PRIVATE_TARIFF
        assert base_tariff(str(especialidad)) > Decimal("0")

    def test_especialidad_desconocida_usa_la_tarifa_por_defecto(self) -> None:
        assert base_tariff("cirugia_espacial") == DEFAULT_PRIVATE_TARIFF

    def test_endodoncia_es_la_mas_costosa(self) -> None:
        assert base_tariff("endodontics") == max(PRIVATE_TARIFF.values())


class TestInvariantes:
    @given(
        regimen=st.sampled_from(list(Regimen)),
        activa=st.booleans(),
        nivel=st.integers(min_value=1, max_value=3),
    )
    def test_el_resultado_siempre_esta_bien_formado(
        self, regimen: Regimen, activa: bool, nivel: int
    ) -> None:
        r = validate_afiliacion(regimen, activa, nivel_cuota_moderadora=nivel)
        assert r.mensaje
        assert r.regimen is regimen
        assert isinstance(r.concepto_cargo, ChargeConcept)
        # Coverage and copayment cannot contradict each other: only a covered
        # service can ask the patient for a copayment.
        if r.requiere_copago:
            assert r.cubierto
        if r.regimen_efectivo is Regimen.PARTICULAR:
            assert not r.requiere_copago
