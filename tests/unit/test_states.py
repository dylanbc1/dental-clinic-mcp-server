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
from backend.enums import FINAL_STATES, EstadoCita

TODOS = list(EstadoCita)

#: The transitions the sector standard (§2.1) declares legal, written out by
#: hand so the test does not simply mirror the implementation's table.
LEGALES: set[tuple[EstadoCita, EstadoCita]] = {
    (EstadoCita.AGENDADA, EstadoCita.CONFIRMADA),
    (EstadoCita.AGENDADA, EstadoCita.CANCELADA),
    (EstadoCita.AGENDADA, EstadoCita.REPROGRAMADA),
    (EstadoCita.AGENDADA, EstadoCita.NO_ASISTIO),
    (EstadoCita.CONFIRMADA, EstadoCita.EN_ESPERA),
    (EstadoCita.CONFIRMADA, EstadoCita.CANCELADA),
    (EstadoCita.CONFIRMADA, EstadoCita.REPROGRAMADA),
    (EstadoCita.CONFIRMADA, EstadoCita.NO_ASISTIO),
    (EstadoCita.EN_ESPERA, EstadoCita.ATENDIDA),
    (EstadoCita.EN_ESPERA, EstadoCita.CANCELADA),
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
        for estado in FINAL_STATES:
            assert reachable_states(estado) == frozenset()
            assert is_final(estado)

    def test_los_estados_no_finales_si_tienen_salida(self) -> None:
        for estado in set(TODOS) - FINAL_STATES:
            assert reachable_states(estado), f"{estado} quedó sin salidas"

    def test_ninguna_transicion_apunta_a_si_misma(self) -> None:
        for origen, destinos in TRANSITIONS.items():
            assert origen not in destinos

    def test_todo_estado_es_alcanzable_desde_agendada(self) -> None:
        """No unreachable state: a state nothing can reach is dead code."""
        alcanzables = {EstadoCita.AGENDADA}
        frontera = [EstadoCita.AGENDADA]
        while frontera:
            current = frontera.pop()
            for next_up in TRANSITIONS[current]:
                if next_up not in alcanzables:
                    alcanzables.add(next_up)
                    frontera.append(next_up)
        assert alcanzables == set(TODOS)


# ---------------------------------------------------------------- exhaustive


@pytest.mark.parametrize(("origen", "target"), sorted(LEGALES))
def test_toda_transicion_legal_se_acepta(origen: EstadoCita, target: EstadoCita) -> None:
    assert is_valid_transition(origen, target)
    effects = validate_transition(origen, target, motivo="motivo de prueba")
    assert effects.estado_anterior is origen
    assert effects.estado_nuevo is target
    assert effects.requiere_auditoria is True


@pytest.mark.parametrize(("origen", "target"), sorted(ILEGALES))
def test_toda_transicion_ilegal_se_rechaza(origen: EstadoCita, target: EstadoCita) -> None:
    assert not is_valid_transition(origen, target)
    with pytest.raises(InvalidTransition):
        validate_transition(origen, target, motivo="motivo de prueba")


def test_la_particion_legal_ilegal_cubre_el_espacio_completo() -> None:
    assert len(LEGALES) + len(ILEGALES) == len(TODOS) ** 2 == 49


# ------------------------------------------------------------------- errores


class TestErroresAccionables:
    def test_transicion_ilegal_lista_las_validas(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(EstadoCita.AGENDADA, EstadoCita.ATENDIDA)
        error = exc.value
        assert error.codigo is ErrorCode.TRANSICION_INVALIDA
        assert error.sugerencia is not None
        # The suggestion must name every acceptable alternative, otherwise the
        # model has to guess, which is what layer 4 exists to prevent.
        for valida in TRANSITIONS[EstadoCita.AGENDADA]:
            assert str(valida) in error.sugerencia
        assert error.detalles["estado_actual"] == "agendada"
        assert error.detalles["estado_solicitado"] == "atendida"

    def test_estado_final_devuelve_su_propio_codigo(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(EstadoCita.ATENDIDA, EstadoCita.CANCELADA)
        assert exc.value.codigo is ErrorCode.CITA_EN_ESTADO_FINAL
        assert "agendar_cita" in (exc.value.sugerencia or "")

    def test_el_error_serializa_a_la_forma_de_cable(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(EstadoCita.EN_ESPERA, EstadoCita.CONFIRMADA)
        payload = exc.value.to_dict()
        assert payload["error"] is True
        assert payload["codigo"] == "TRANSICION_INVALIDA"
        assert payload["mensaje"]
        assert payload["sugerencia"]
        assert "detalles" in payload


class TestMotivoObligatorio:
    @pytest.mark.parametrize(
        "origen", [EstadoCita.AGENDADA, EstadoCita.CONFIRMADA, EstadoCita.EN_ESPERA]
    )
    def test_cancelar_sin_motivo_falla(self, origen: EstadoCita) -> None:
        with pytest.raises(ReasonRequired) as exc:
            validate_transition(origen, EstadoCita.CANCELADA)
        assert exc.value.codigo is ErrorCode.MOTIVO_REQUERIDO
        assert "motivo" in (exc.value.sugerencia or "")

    @pytest.mark.parametrize("motivo", ["", "   ", "\t\n"])
    def test_motivo_en_blanco_no_cuenta_como_motivo(self, motivo: str) -> None:
        with pytest.raises(ReasonRequired):
            validate_transition(EstadoCita.AGENDADA, EstadoCita.CANCELADA, motivo=motivo)

    def test_cancelar_con_motivo_pasa(self) -> None:
        effects = validate_transition(
            EstadoCita.CONFIRMADA, EstadoCita.CANCELADA, motivo="El paciente viajó"
        )
        assert effects.libera_slot

    def test_solo_cancelada_exige_motivo(self) -> None:
        assert {EstadoCita.CANCELADA} == TRANSITIONS_REQUIRING_REASON
        # Every other legal transition must go through without one.
        for origen, target in LEGALES:
            if target is EstadoCita.CANCELADA:
                continue
            validate_transition(origen, target)


# ------------------------------------------------------------------- efectos


class TestEfectos:
    @pytest.mark.parametrize(("origen", "target"), sorted(LEGALES))
    def test_los_efectos_son_consistentes_con_las_tablas(
        self, origen: EstadoCita, target: EstadoCita
    ) -> None:
        effects = validate_transition(origen, target, motivo="x")
        assert effects.libera_slot is (target in TRANSITIONS_FREEING_SLOT)
        assert effects.genera_cargo is (target in TRANSITIONS_CREATING_CHARGE)

    def test_solo_la_cancelacion_dispara_lista_de_espera(self) -> None:
        # A reschedule moves the same patient and a no-show happens once the
        # slot has already elapsed: neither leaves a slot someone can take.
        assert validate_transition(
            EstadoCita.AGENDADA, EstadoCita.CANCELADA, motivo="x"
        ).dispara_lista_espera
        assert not validate_transition(
            EstadoCita.AGENDADA, EstadoCita.REPROGRAMADA
        ).dispara_lista_espera
        assert not validate_transition(
            EstadoCita.CONFIRMADA, EstadoCita.NO_ASISTIO
        ).dispara_lista_espera

    def test_atender_genera_cargo_y_no_libera_cupo(self) -> None:
        effects = validate_transition(EstadoCita.EN_ESPERA, EstadoCita.ATENDIDA)
        assert effects.genera_cargo
        assert not effects.libera_slot

    def test_los_efectos_son_inmutables(self) -> None:
        effects = validate_transition(EstadoCita.AGENDADA, EstadoCita.CONFIRMADA)
        with pytest.raises((AttributeError, TypeError)):
            effects.libera_slot = True  # type: ignore[misc]


# ------------------------------------------------------------ property-based

estados = st.sampled_from(TODOS)


class TestPropiedades:
    @given(origen=estados, target=estados)
    def test_validar_y_predicado_nunca_se_contradicen(
        self, origen: EstadoCita, target: EstadoCita
    ) -> None:
        """The pure predicate and the raising validator must agree, always."""
        if is_valid_transition(origen, target):
            validate_transition(origen, target, motivo="motivo")
        else:
            with pytest.raises(InvalidTransition):
                validate_transition(origen, target, motivo="motivo")

    @given(origen=estados, target=estados)
    def test_ninguna_entrada_produce_una_excepcion_inesperada(
        self, origen: EstadoCita, target: EstadoCita
    ) -> None:
        """Whatever happens, it is a typed domain error, never a bare crash."""
        try:
            validate_transition(origen, target)
        except (InvalidTransition, ReasonRequired) as error:
            assert error.codigo in {
                ErrorCode.TRANSICION_INVALIDA,
                ErrorCode.CITA_EN_ESTADO_FINAL,
                ErrorCode.MOTIVO_REQUERIDO,
            }
            assert error.to_dict()["mensaje"]

    @given(origen=estados)
    def test_desde_un_estado_final_nada_es_posible(self, origen: EstadoCita) -> None:
        if is_final(origen):
            for target in TODOS:
                assert not is_valid_transition(origen, target)
