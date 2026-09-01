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


class TestCobertura:
    def test_todo_estado_tiene_etiqueta(self) -> None:
        """A new state without a label would otherwise leak English into the
        question a human approves."""
        assert set(APPOINTMENT_STATE_LABELS) == set(AppointmentState)

    def test_ninguna_etiqueta_es_el_valor_interno(self) -> None:
        for state, label in APPOINTMENT_STATE_LABELS.items():
            assert label != state.value, f"{state.value} was never translated"

    def test_ninguna_etiqueta_lleva_guion_bajo(self) -> None:
        """`no_show` is a machine value. A person reads `no asistió`."""
        for label in APPOINTMENT_STATE_LABELS.values():
            assert "_" not in label


class TestEtiqueta:
    def test_acepta_el_enum(self) -> None:
        assert state_label(AppointmentState.CANCELLED) == "cancelada"

    def test_acepta_la_cadena_que_viene_del_backend(self) -> None:
        """Callers hold decoded JSON, not enum members."""
        assert state_label("no_show") == "no asistió"

    def test_un_estado_desconocido_falla_en_vez_de_filtrar_ingles(self) -> None:
        with pytest.raises(ValueError, match="inventado"):
            state_label("inventado")


class TestEspecialidades:
    def test_toda_especialidad_tiene_etiqueta(self) -> None:
        assert set(SPECIALTY_LABELS) == set(Specialty)

    def test_ninguna_etiqueta_es_el_valor_interno(self) -> None:
        for specialty, label in SPECIALTY_LABELS.items():
            assert label != specialty.value, f"{specialty.value} was never translated"

    def test_la_etiqueta_lleva_tilde_donde_corresponde(self) -> None:
        """`odontología general`, not `odontologia general`. The clinic writes
        Spanish properly."""
        assert specialty_label("general_dentistry") == "odontología general"
        assert specialty_label(Specialty.PEDIATRIC_DENTISTRY) == "odontopediatría"
