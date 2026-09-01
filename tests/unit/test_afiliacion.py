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
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_active=True)
        assert r.active
        assert r.covered
        assert r.requires_copago
        assert r.charge_concept is ChargeConcept.CUOTA_MODERADORA
        assert r.effective_regimen is Regimen.CONTRIBUTIVO

    @pytest.mark.parametrize("level", [1, 2, 3])
    def test_el_nivel_cambia_el_monto_informado(self, level: int) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_active=True, cuota_moderadora_level=level
        )
        expected = CUOTA_MODERADORA_BY_BRACKET[level]
        assert f"{expected:,.0f}" in r.message

    def test_nivel_desconocido_cae_al_nivel_1(self) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_active=True, cuota_moderadora_level=99
        )
        assert f"{CUOTA_MODERADORA_BY_BRACKET[1]:,.0f}" in r.message


class TestRegimenSubsidiado:
    def test_activo_aplica_copago(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_active=True)
        assert r.charge_concept is ChargeConcept.COPAGO
        assert r.requires_copago
        assert r.covered


class TestParticular:
    def test_nunca_tiene_copago(self) -> None:
        r = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=True)
        assert not r.requires_copago
        assert not r.covered
        assert r.charge_concept is ChargeConcept.PARTICULAR

    def test_el_flag_de_afiliacion_es_irrelevante_para_un_particular(self) -> None:
        """A private patient has nothing to be affiliated to."""
        active = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=True)
        inactivo = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=False)
        assert active == inactivo
        assert inactivo.active is True


class TestSoat:
    def test_cubre_totalmente_y_no_genera_copago(self) -> None:
        r = validate_afiliacion(Regimen.SOAT, afiliacion_active=True)
        assert r.covered
        assert not r.requires_copago
        assert r.effective_regimen is Regimen.SOAT


class TestAfiliacionInactiva:
    @pytest.mark.parametrize("regimen", [Regimen.CONTRIBUTIVO, Regimen.SUBSIDIADO, Regimen.SOAT])
    def test_cae_a_tarifa_particular(self, regimen: Regimen) -> None:
        r = validate_afiliacion(regimen, afiliacion_active=False)
        assert not r.active
        assert r.effective_regimen is Regimen.PARTICULAR
        assert r.charge_concept is ChargeConcept.PARTICULAR
        assert not r.covered
        assert not r.requires_copago

    def test_conserva_el_regimen_original_para_informar(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_active=False)
        assert r.regimen is Regimen.SUBSIDIADO
        assert "is inactive" in r.message

    def test_da_una_sugerencia_accionable(self) -> None:
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_active=False)
        assert r.suggestion is not None
        assert "EPS" in r.suggestion


class TestNoBloqueaAgendamiento:
    """The rule that separates this from a naive implementation."""

    @given(
        regimen=st.sampled_from(list(Regimen)),
        active=st.booleans(),
        level=st.integers(min_value=1, max_value=3),
    )
    def test_ninguna_combinacion_impide_agendar(
        self, regimen: Regimen, active: bool, level: int
    ) -> None:
        r = validate_afiliacion(regimen, active, cuota_moderadora_level=level)
        assert r.blocks_booking is False


class TestTarifas:
    @pytest.mark.parametrize("specialty", list(Specialty))
    def test_toda_especialidad_tiene_tarifa(self, specialty: Specialty) -> None:
        assert str(specialty) in PRIVATE_TARIFF
        assert base_tariff(str(specialty)) > Decimal("0")

    def test_especialidad_desconocida_usa_la_tarifa_por_defecto(self) -> None:
        assert base_tariff("cirugia_espacial") == DEFAULT_PRIVATE_TARIFF

    def test_endodoncia_es_la_mas_costosa(self) -> None:
        assert base_tariff("endodontics") == max(PRIVATE_TARIFF.values())


class TestInvariantes:
    @given(
        regimen=st.sampled_from(list(Regimen)),
        active=st.booleans(),
        level=st.integers(min_value=1, max_value=3),
    )
    def test_el_resultado_siempre_esta_bien_formado(
        self, regimen: Regimen, active: bool, level: int
    ) -> None:
        r = validate_afiliacion(regimen, active, cuota_moderadora_level=level)
        assert r.message
        assert r.regimen is regimen
        assert isinstance(r.charge_concept, ChargeConcept)
        # Coverage and copayment cannot contradict each other: only a covered
        # service can ask the patient for a copayment.
        if r.requires_copago:
            assert r.covered
        if r.effective_regimen is Regimen.PARTICULAR:
            assert not r.requires_copago
