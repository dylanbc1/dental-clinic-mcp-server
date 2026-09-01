"""State machine tests.

The whole 7x7 transition space is enumerated: there is no legal transition that
is not asserted legal, and no illegal one that is not asserted illegal. That
exhaustiveness is the point: a state machine tested by example always grows an
untested edge.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.errors import ErrorCode, InvalidTransition, ReasonRequired
from backend.domain.states import (
    TRANSITIONS,
    TRANSITIONS_CREATING_CHARGE,
    TRANSITIONS_FREEING_SLOT,
    TRANSITIONS_REQUIRING_REASON,
    is_final,
    is_valid_transition,
    reachable_states,
    validate_transition,
)
from backend.enums import FINAL_STATES, AppointmentState

TODOS = list(AppointmentState)

#: The transitions the sector standard (§2.1) declares legal, written out by
#: hand so the test does not simply mirror the implementation's table.
LEGALES: set[tuple[AppointmentState, AppointmentState]] = {
    (AppointmentState.SCHEDULED, AppointmentState.CONFIRMED),
    (AppointmentState.SCHEDULED, AppointmentState.CANCELLED),
    (AppointmentState.SCHEDULED, AppointmentState.RESCHEDULED),
    (AppointmentState.SCHEDULED, AppointmentState.NO_SHOW),
    (AppointmentState.CONFIRMED, AppointmentState.WAITING),
    (AppointmentState.CONFIRMED, AppointmentState.CANCELLED),
    (AppointmentState.CONFIRMED, AppointmentState.RESCHEDULED),
    (AppointmentState.CONFIRMED, AppointmentState.NO_SHOW),
    (AppointmentState.WAITING, AppointmentState.ATTENDED),
    (AppointmentState.WAITING, AppointmentState.CANCELLED),
}

ILEGALES = {par for par in itertools.product(TODOS, TODOS) if par not in LEGALES}


# --------------------------------------------------------------------- table


class TestTablaDeTransiciones:
    def test_la_tabla_cubre_todos_los_estados(self) -> None:
        assert set(TRANSITIONS) == set(TODOS)

    def test_la_tabla_coincide_con_el_estandar_del_sector(self) -> None:
        derivadas = {
            (origen, target) for origen, destinos in TRANSITIONS.items() for target in destinos
        }
        assert derivadas == LEGALES

    def test_los_estados_finales_no_tienen_salida(self) -> None:
        for status in FINAL_STATES:
            assert reachable_states(status) == frozenset()
            assert is_final(status)

    def test_los_estados_no_finales_si_tienen_salida(self) -> None:
        for status in set(TODOS) - FINAL_STATES:
            assert reachable_states(status), f"{status} quedó sin salidas"

    def test_ninguna_transicion_apunta_a_si_misma(self) -> None:
        for origen, destinos in TRANSITIONS.items():
            assert origen not in destinos

    def test_todo_estado_es_alcanzable_desde_agendada(self) -> None:
        """No unreachable state: a state nothing can reach is dead code."""
        alcanzables = {AppointmentState.SCHEDULED}
        frontera = [AppointmentState.SCHEDULED]
        while frontera:
            current = frontera.pop()
            for next_up in TRANSITIONS[current]:
                if next_up not in alcanzables:
                    alcanzables.add(next_up)
                    frontera.append(next_up)
        assert alcanzables == set(TODOS)


# ---------------------------------------------------------------- exhaustive


@pytest.mark.parametrize(("origen", "target"), sorted(LEGALES))
def test_toda_transicion_legal_se_acepta(
    origen: AppointmentState, target: AppointmentState
) -> None:
    assert is_valid_transition(origen, target)
    effects = validate_transition(origen, target, reason="motivo de prueba")
    assert effects.previous_status is origen
    assert effects.new_status is target
    assert effects.requiere_auditoria is True


@pytest.mark.parametrize(("origen", "target"), sorted(ILEGALES))
def test_toda_transicion_ilegal_se_rechaza(
    origen: AppointmentState, target: AppointmentState
) -> None:
    assert not is_valid_transition(origen, target)
    with pytest.raises(InvalidTransition):
        validate_transition(origen, target, reason="motivo de prueba")


def test_la_particion_legal_ilegal_cubre_el_espacio_completo() -> None:
    assert len(LEGALES) + len(ILEGALES) == len(TODOS) ** 2 == 49


# ------------------------------------------------------------------- errores


class TestErroresAccionables:
    def test_transicion_ilegal_lista_las_validas(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.SCHEDULED, AppointmentState.ATTENDED)
        error = exc.value
        assert error.code is ErrorCode.INVALID_TRANSITION
        assert error.suggestion is not None
        # The suggestion must name every acceptable alternative, otherwise the
        # model has to guess, which is what layer 4 exists to prevent.
        for valida in TRANSITIONS[AppointmentState.SCHEDULED]:
            assert str(valida) in error.suggestion
        assert error.details["estado_actual"] == "scheduled"
        assert error.details["requested_state"] == "attended"

    def test_estado_final_devuelve_su_propio_codigo(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.ATTENDED, AppointmentState.CANCELLED)
        assert exc.value.code is ErrorCode.APPOINTMENT_IN_FINAL_STATE
        assert "book_appointment" in (exc.value.suggestion or "")

    def test_el_error_serializa_a_la_forma_de_cable(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.WAITING, AppointmentState.CONFIRMED)
        payload = exc.value.to_dict()
        assert payload["error"] is True
        assert payload["code"] == "INVALID_TRANSITION"
        assert payload["message"]
        assert payload["suggestion"]
        assert "details" in payload


class TestMotivoObligatorio:
    @pytest.mark.parametrize(
        "origen", [AppointmentState.SCHEDULED, AppointmentState.CONFIRMED, AppointmentState.WAITING]
    )
    def test_cancelar_sin_motivo_falla(self, origen: AppointmentState) -> None:
        with pytest.raises(ReasonRequired) as exc:
            validate_transition(origen, AppointmentState.CANCELLED)
        assert exc.value.code is ErrorCode.REASON_REQUIRED
        assert "reason" in (exc.value.suggestion or "")

    @pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
    def test_motivo_en_blanco_no_cuenta_como_motivo(self, reason: str) -> None:
        with pytest.raises(ReasonRequired):
            validate_transition(
                AppointmentState.SCHEDULED, AppointmentState.CANCELLED, reason=reason
            )

    def test_cancelar_con_motivo_pasa(self) -> None:
        effects = validate_transition(
            AppointmentState.CONFIRMED, AppointmentState.CANCELLED, reason="El paciente viajó"
        )
        assert effects.libera_slot

    def test_solo_cancelada_exige_motivo(self) -> None:
        assert {AppointmentState.CANCELLED} == TRANSITIONS_REQUIRING_REASON
        # Every other legal transition must go through without one.
        for origen, target in LEGALES:
            if target is AppointmentState.CANCELLED:
                continue
            validate_transition(origen, target)


# ------------------------------------------------------------------- efectos


class TestEfectos:
    @pytest.mark.parametrize(("origen", "target"), sorted(LEGALES))
    def test_los_efectos_son_consistentes_con_las_tablas(
        self, origen: AppointmentState, target: AppointmentState
    ) -> None:
        effects = validate_transition(origen, target, reason="x")
        assert effects.libera_slot is (target in TRANSITIONS_FREEING_SLOT)
        assert effects.genera_cargo is (target in TRANSITIONS_CREATING_CHARGE)

    def test_solo_la_cancelacion_dispara_lista_de_espera(self) -> None:
        # A reschedule moves the same patient and a no-show happens once the
        # slot has already elapsed: neither leaves a slot someone can take.
        assert validate_transition(
            AppointmentState.SCHEDULED, AppointmentState.CANCELLED, reason="x"
        ).dispara_lista_espera
        assert not validate_transition(
            AppointmentState.SCHEDULED, AppointmentState.RESCHEDULED
        ).dispara_lista_espera
        assert not validate_transition(
            AppointmentState.CONFIRMED, AppointmentState.NO_SHOW
        ).dispara_lista_espera

    def test_atender_genera_cargo_y_no_libera_cupo(self) -> None:
        effects = validate_transition(AppointmentState.WAITING, AppointmentState.ATTENDED)
        assert effects.genera_cargo
        assert not effects.libera_slot

    def test_los_efectos_son_inmutables(self) -> None:
        effects = validate_transition(AppointmentState.SCHEDULED, AppointmentState.CONFIRMED)
        with pytest.raises((AttributeError, TypeError)):
            effects.libera_slot = True  # type: ignore[misc]


# ------------------------------------------------------------ property-based

estados = st.sampled_from(TODOS)


class TestPropiedades:
    @given(origen=estados, target=estados)
    def test_validar_y_predicado_nunca_se_contradicen(
        self, origen: AppointmentState, target: AppointmentState
    ) -> None:
        """The pure predicate and the raising validator must agree, always."""
        if is_valid_transition(origen, target):
            validate_transition(origen, target, reason="reason")
        else:
            with pytest.raises(InvalidTransition):
                validate_transition(origen, target, reason="reason")

    @given(origen=estados, target=estados)
    def test_ninguna_entrada_produce_una_excepcion_inesperada(
        self, origen: AppointmentState, target: AppointmentState
    ) -> None:
        """Whatever happens, it is a typed domain error, never a bare crash."""
        try:
            validate_transition(origen, target)
        except (InvalidTransition, ReasonRequired) as error:
            assert error.code in {
                ErrorCode.INVALID_TRANSITION,
                ErrorCode.APPOINTMENT_IN_FINAL_STATE,
                ErrorCode.REASON_REQUIRED,
            }
            assert error.to_dict()["message"]

    @given(origen=estados)
    def test_desde_un_estado_final_nada_es_posible(self, origen: AppointmentState) -> None:
        if is_final(origen):
            for target in TODOS:
                assert not is_valid_transition(origen, target)
