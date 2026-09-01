"""The boundary between internal values and the words a person reads.

Internal values are English; the clinic reads Spanish. These tests exist so the
two cannot be spliced back together by accident: a state added to the enum
without a label fails here rather than reaching a receptionist as `scheduled`.
"""

from __future__ import annotations

import pytest

from backend.domain.labels import (
    APPOINTMENT_STATE_LABELS,
    SPECIALTY_LABELS,
    specialty_label,
    state_label,
)
from backend.enums import AppointmentState, Specialty


class TestCoverage:
    def test_every_state_has_a_label(self) -> None:
        """A new state without a label would otherwise leak English into the
        question a human approves."""
        assert set(APPOINTMENT_STATE_LABELS) == set(AppointmentState)

    def test_no_label_is_the_internal_value(self) -> None:
        for state, label in APPOINTMENT_STATE_LABELS.items():
            assert label != state.value, f"{state.value} was never translated"

    def test_no_label_carries_an_underscore(self) -> None:
        """`no_show` is a machine value. A person reads `no asistió`."""
        for label in APPOINTMENT_STATE_LABELS.values():
            assert "_" not in label


class TestLabel:
    def test_accepts_the_enum(self) -> None:
        assert state_label(AppointmentState.CANCELLED) == "cancelada"

    def test_accepts_the_string_from_the_backend(self) -> None:
        """Callers hold decoded JSON, not enum members."""
        assert state_label("no_show") == "no asistió"

    def test_an_unknown_state_fails_instead_of_leaking_english(self) -> None:
        with pytest.raises(ValueError, match="inventado"):
            state_label("inventado")


class TestSpecialties:
    def test_every_specialty_has_a_label(self) -> None:
        assert set(SPECIALTY_LABELS) == set(Specialty)

    def test_no_label_is_the_internal_value(self) -> None:
        for specialty, label in SPECIALTY_LABELS.items():
            assert label != specialty.value, f"{specialty.value} was never translated"

    def test_the_label_carries_its_accents(self) -> None:
        """`odontología general`, not `odontologia general`. The clinic writes
        Spanish properly."""
        assert specialty_label("general_dentistry") == "odontología general"
        assert specialty_label(Specialty.PEDIATRIC_DENTISTRY) == "odontopediatría"
