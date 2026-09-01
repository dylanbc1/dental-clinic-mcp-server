"""Waiting list ordering tests (§2.4).

The queue must be a *total* order: same input, same output, every time. A queue
that reshuffles between calls offers the same slot to two different patients.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.errors import ErrorCode, WaitingListEmpty
from backend.domain.time import UTC
from backend.domain.waiting_list import (
    WaitingListEntry,
    candidates_for_slot,
    in_queue_order,
    next_in_queue,
    position_in_queue,
)
from backend.enums import Specialty, WaitingListPriority, WaitingListState

BASE = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
GENERAL = Specialty.GENERAL_DENTISTRY
ORTO = Specialty.ORTHODONTICS


def entry(
    entry_id: int,
    patient_id: int,
    *,
    minutos: int = 0,
    specialty: Specialty = GENERAL,
    priority: WaitingListPriority = WaitingListPriority.SENIORITY,
    status: WaitingListState = WaitingListState.ACTIVE,
) -> WaitingListEntry:
    return WaitingListEntry(
        entry_id=entry_id,
        patient_id=patient_id,
        specialty=specialty,
        priority=priority,
        created_at=BASE + timedelta(minutes=minutos),
        status=status,
    )


class TestOrden:
    def test_a_igual_prioridad_manda_la_antiguedad(self) -> None:
        entries = [entry(1, 10, minutos=30), entry(2, 20, minutos=0)]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_la_urgencia_pasa_por_encima_de_la_antiguedad(self) -> None:
        entries = [
            entry(1, 10, minutos=0),  # enrolled first, but routine
            entry(2, 20, minutos=500, priority=WaitingListPriority.URGENT),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_entre_urgencias_manda_la_antiguedad(self) -> None:
        entries = [
            entry(1, 10, minutos=100, priority=WaitingListPriority.URGENT),
            entry(2, 20, minutos=50, priority=WaitingListPriority.URGENT),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_el_id_desempata_tiempos_identicos(self) -> None:
        """Two entries created in the same microsecond must still have a
        deterministic order, or the same slot gets offered twice."""
        entries = [entry(9, 90), entry(3, 30), entry(5, 50)]
        assert [e.entry_id for e in in_queue_order(entries)] == [3, 5, 9]

    def test_solo_participan_las_entradas_activas(self) -> None:
        entries = [
            entry(1, 10, status=WaitingListState.WITHDRAWN),
            entry(2, 20, minutos=10, status=WaitingListState.OFFERED),
            entry(3, 30, minutos=20, status=WaitingListState.ACCEPTED),
            entry(4, 40, minutos=30),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [40]

    def test_el_orden_es_estable_ante_permutaciones_de_la_entrada(self) -> None:
        entries = [entry(i, i * 10, minutos=i * 7) for i in range(1, 9)]
        expected = [e.entry_id for e in in_queue_order(entries)]
        rng = random.Random(42)  # noqa: S311 - shuffling test input
        for _ in range(20):
            barajado = entries[:]
            rng.shuffle(barajado)
            assert [e.entry_id for e in in_queue_order(barajado)] == expected

    def test_ordenar_no_muta_la_lista_recibida(self) -> None:
        entries = [entry(2, 20, minutos=5), entry(1, 10)]
        copia = list(entries)
        in_queue_order(entries)
        assert entries == copia


class TestCandidatos:
    def test_filtra_por_especialidad(self) -> None:
        entries = [entry(1, 10, specialty=ORTO), entry(2, 20, specialty=GENERAL)]
        assert [e.patient_id for e in candidates_for_slot(entries, GENERAL)] == [20]

    def test_excluye_a_los_pacientes_indicados(self) -> None:
        """The patient whose cancellation freed the slot is not offered it back."""
        entries = [entry(1, 10), entry(2, 20, minutos=5)]
        candidates = candidates_for_slot(entries, GENERAL, excluir_pacientes=frozenset({10}))
        assert [e.patient_id for e in candidates] == [20]

    def test_sin_coincidencias_devuelve_lista_vacia(self) -> None:
        assert candidates_for_slot([entry(1, 10, specialty=ORTO)], GENERAL) == []


class TestSiguienteEnLista:
    def test_devuelve_el_primero_de_la_cola(self) -> None:
        entries = [
            entry(1, 10, minutos=60),
            entry(2, 20, minutos=10, priority=WaitingListPriority.URGENT),
        ]
        assert next_in_queue(entries, GENERAL).patient_id == 20

    def test_lista_vacia_lanza_error_tipado_no_none(self) -> None:
        with pytest.raises(WaitingListEmpty) as exc:
            next_in_queue([], GENERAL)
        assert exc.value.code is ErrorCode.WAITING_LIST_EMPTY
        assert exc.value.suggestion is not None
        assert "check_availability" in exc.value.suggestion
        assert exc.value.details["specialty"] == str(GENERAL)

    def test_todos_excluidos_equivale_a_lista_vacia(self) -> None:
        entries = [entry(1, 10)]
        with pytest.raises(WaitingListEmpty):
            next_in_queue(entries, GENERAL, excluir_pacientes=frozenset({10}))


class TestPosicion:
    def test_devuelve_la_posicion_en_base_1(self) -> None:
        entries = [entry(1, 10), entry(2, 20, minutos=5), entry(3, 30, minutos=9)]
        assert position_in_queue(entries, 10, GENERAL) == 1
        assert position_in_queue(entries, 30, GENERAL) == 3

    def test_paciente_no_inscrito_devuelve_none(self) -> None:
        assert position_in_queue([entry(1, 10)], 99, GENERAL) is None

    def test_la_posicion_respeta_la_urgencia(self) -> None:
        entries = [
            entry(1, 10),
            entry(2, 20, minutos=99, priority=WaitingListPriority.URGENT),
        ]
        assert position_in_queue(entries, 20, GENERAL) == 1
        assert position_in_queue(entries, 10, GENERAL) == 2


class TestPropiedades:
    entradas_st = st.lists(
        st.builds(
            entry,
            entry_id=st.integers(min_value=1, max_value=500),
            patient_id=st.integers(min_value=1, max_value=500),
            minutos=st.integers(min_value=0, max_value=10_000),
            specialty=st.sampled_from(list(Specialty)),
            priority=st.sampled_from(list(WaitingListPriority)),
            status=st.sampled_from(list(WaitingListState)),
        ),
        max_size=25,
        unique_by=lambda e: e.entry_id,
    )

    @given(entries=entradas_st)
    def test_el_orden_es_total_y_determinista(self, entries: list[WaitingListEntry]) -> None:
        primera = [e.entry_id for e in in_queue_order(entries)]
        segunda = [e.entry_id for e in in_queue_order(list(reversed(entries)))]
        assert primera == segunda

    @given(entries=entradas_st)
    def test_ninguna_urgencia_queda_detras_de_una_rutina(
        self, entries: list[WaitingListEntry]
    ) -> None:
        queue = in_queue_order(entries)
        visto_rutina = False
        for e in queue:
            if e.priority is WaitingListPriority.SENIORITY:
                visto_rutina = True
            elif visto_rutina:
                pytest.fail("una urgencia quedó detrás de una entrada por antigüedad")

    @given(entries=entradas_st)
    def test_ordenar_conserva_exactamente_las_activas(
        self, entries: list[WaitingListEntry]
    ) -> None:
        active = {e.entry_id for e in entries if e.status is WaitingListState.ACTIVE}
        assert {e.entry_id for e in in_queue_order(entries)} == active
