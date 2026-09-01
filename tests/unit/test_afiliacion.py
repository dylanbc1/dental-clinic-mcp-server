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
    def test_active_applies_cuota_moderadora(self) -> None:
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_active=True)
        assert r.active
        assert r.covered
        assert r.requires_copago
        assert r.charge_concept is ChargeConcept.CUOTA_MODERADORA
        assert r.effective_regimen is Regimen.CONTRIBUTIVO

    @pytest.mark.parametrize("level", [1, 2, 3])
    def test_the_level_changes_the_reported_amount(self, level: int) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_active=True, cuota_moderadora_level=level
        )
        expected = CUOTA_MODERADORA_BY_BRACKET[level]
        assert f"{expected:,.0f}" in r.message

    def test_an_unknown_level_falls_back_to_level_1(self) -> None:
        r = validate_afiliacion(
            Regimen.CONTRIBUTIVO, afiliacion_active=True, cuota_moderadora_level=99
        )
        assert f"{CUOTA_MODERADORA_BY_BRACKET[1]:,.0f}" in r.message


class TestRegimenSubsidiado:
    def test_active_applies_copago(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_active=True)
        assert r.charge_concept is ChargeConcept.COPAGO
        assert r.requires_copago
        assert r.covered


class TestParticular:
    def test_it_never_has_a_copago(self) -> None:
        r = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=True)
        assert not r.requires_copago
        assert not r.covered
        assert r.charge_concept is ChargeConcept.PARTICULAR

    def test_the_afiliacion_flag_is_irrelevant_for_a_particular(self) -> None:
        """A private patient has nothing to be affiliated to."""
        active = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=True)
        inactivo = validate_afiliacion(Regimen.PARTICULAR, afiliacion_active=False)
        assert active == inactivo
        assert inactivo.active is True


class TestSoat:
    def test_fully_covered_creates_no_copago(self) -> None:
        r = validate_afiliacion(Regimen.SOAT, afiliacion_active=True)
        assert r.covered
        assert not r.requires_copago
        assert r.effective_regimen is Regimen.SOAT


class TestInactiveAfiliacion:
    @pytest.mark.parametrize("regimen", [Regimen.CONTRIBUTIVO, Regimen.SUBSIDIADO, Regimen.SOAT])
    def test_falls_back_to_the_particular_tariff(self, regimen: Regimen) -> None:
        r = validate_afiliacion(regimen, afiliacion_active=False)
        assert not r.active
        assert r.effective_regimen is Regimen.PARTICULAR
        assert r.charge_concept is ChargeConcept.PARTICULAR
        assert not r.covered
        assert not r.requires_copago

    def test_keeps_the_original_regimen_to_report_it(self) -> None:
        r = validate_afiliacion(Regimen.SUBSIDIADO, afiliacion_active=False)
        assert r.regimen is Regimen.SUBSIDIADO
        assert "is inactive" in r.message

    def test_gives_an_actionable_suggestion(self) -> None:
        r = validate_afiliacion(Regimen.CONTRIBUTIVO, afiliacion_active=False)
        assert r.suggestion is not None
        assert "EPS" in r.suggestion


class TestDoesNotBlockBooking:
    """The rule that separates this from a naive implementation."""

    @given(
        regimen=st.sampled_from(list(Regimen)),
        active=st.booleans(),
        level=st.integers(min_value=1, max_value=3),
    )
    def test_no_combination_prevents_booking(
        self, regimen: Regimen, active: bool, level: int
    ) -> None:
        r = validate_afiliacion(regimen, active, cuota_moderadora_level=level)
        assert r.blocks_booking is False


class TestTariffs:
    @pytest.mark.parametrize("specialty", list(Specialty))
    def test_every_specialty_has_a_tariff(self, specialty: Specialty) -> None:
        assert str(specialty) in PRIVATE_TARIFF
        assert base_tariff(str(specialty)) > Decimal("0")

    def test_an_unknown_specialty_uses_the_default_tariff(self) -> None:
        assert base_tariff("cirugia_espacial") == DEFAULT_PRIVATE_TARIFF

    def test_endodontics_is_the_most_expensive(self) -> None:
        assert base_tariff("endodontics") == max(PRIVATE_TARIFF.values())


class TestInvariants:
    @given(
        regimen=st.sampled_from(list(Regimen)),
        active=st.booleans(),
        level=st.integers(min_value=1, max_value=3),
    )
    def test_the_result_is_always_well_formed(
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
