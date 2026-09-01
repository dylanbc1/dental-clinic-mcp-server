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

from backend.domain.errors import CodigoError, MotivoRequerido, TransicionInvalida
from backend.domain.states import (
    TRANSICIONES,
    TRANSICIONES_QUE_EXIGEN_MOTIVO,
    TRANSICIONES_QUE_GENERAN_CARGO,
    TRANSICIONES_QUE_LIBERAN_SLOT,
    es_final,
    es_transicion_valida,
    transiciones_posibles,
    validar_transicion,
)
from backend.enums import ESTADOS_FINALES, EstadoCita

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
        assert set(TRANSICIONES) == set(TODOS)

    def test_la_tabla_coincide_con_el_estandar_del_sector(self) -> None:
        derivadas = {
            (origen, destino) for origen, destinos in TRANSICIONES.items() for destino in destinos
        }
        assert derivadas == LEGALES

    def test_los_estados_finales_no_tienen_salida(self) -> None:
        for estado in ESTADOS_FINALES:
            assert transiciones_posibles(estado) == frozenset()
            assert es_final(estado)

    def test_los_estados_no_finales_si_tienen_salida(self) -> None:
        for estado in set(TODOS) - ESTADOS_FINALES:
            assert transiciones_posibles(estado), f"{estado} quedó sin salidas"

    def test_ninguna_transicion_apunta_a_si_misma(self) -> None:
        for origen, destinos in TRANSICIONES.items():
            assert origen not in destinos

    def test_todo_estado_es_alcanzable_desde_agendada(self) -> None:
        """No unreachable state: a state nothing can reach is dead code."""
        alcanzables = {EstadoCita.AGENDADA}
        frontera = [EstadoCita.AGENDADA]
        while frontera:
            actual = frontera.pop()
            for siguiente in TRANSICIONES[actual]:
                if siguiente not in alcanzables:
                    alcanzables.add(siguiente)
                    frontera.append(siguiente)
        assert alcanzables == set(TODOS)


# ---------------------------------------------------------------- exhaustive


@pytest.mark.parametrize(("origen", "destino"), sorted(LEGALES))
def test_toda_transicion_legal_se_acepta(origen: EstadoCita, destino: EstadoCita) -> None:
    assert es_transicion_valida(origen, destino)
    efectos = validar_transicion(origen, destino, motivo="motivo de prueba")
    assert efectos.estado_anterior is origen
    assert efectos.estado_nuevo is destino
    assert efectos.requiere_auditoria is True


@pytest.mark.parametrize(("origen", "destino"), sorted(ILEGALES))
def test_toda_transicion_ilegal_se_rechaza(origen: EstadoCita, destino: EstadoCita) -> None:
    assert not es_transicion_valida(origen, destino)
    with pytest.raises(TransicionInvalida):
        validar_transicion(origen, destino, motivo="motivo de prueba")


def test_la_particion_legal_ilegal_cubre_el_espacio_completo() -> None:
    assert len(LEGALES) + len(ILEGALES) == len(TODOS) ** 2 == 49


# ------------------------------------------------------------------- errores


class TestErroresAccionables:
    def test_transicion_ilegal_lista_las_validas(self) -> None:
        with pytest.raises(TransicionInvalida) as exc:
            validar_transicion(EstadoCita.AGENDADA, EstadoCita.ATENDIDA)
        error = exc.value
        assert error.codigo is CodigoError.TRANSICION_INVALIDA
        assert error.sugerencia is not None
        # The suggestion must name every acceptable alternative, otherwise the
        # model has to guess, which is what layer 4 exists to prevent.
        for valida in TRANSICIONES[EstadoCita.AGENDADA]:
            assert str(valida) in error.sugerencia
        assert error.detalles["estado_actual"] == "agendada"
        assert error.detalles["estado_solicitado"] == "atendida"

    def test_estado_final_devuelve_su_propio_codigo(self) -> None:
        with pytest.raises(TransicionInvalida) as exc:
            validar_transicion(EstadoCita.ATENDIDA, EstadoCita.CANCELADA)
        assert exc.value.codigo is CodigoError.CITA_EN_ESTADO_FINAL
        assert "agendar_cita" in (exc.value.sugerencia or "")

    def test_el_error_serializa_a_la_forma_de_cable(self) -> None:
        with pytest.raises(TransicionInvalida) as exc:
            validar_transicion(EstadoCita.EN_ESPERA, EstadoCita.CONFIRMADA)
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
        with pytest.raises(MotivoRequerido) as exc:
            validar_transicion(origen, EstadoCita.CANCELADA)
        assert exc.value.codigo is CodigoError.MOTIVO_REQUERIDO
        assert "motivo" in (exc.value.sugerencia or "")

    @pytest.mark.parametrize("motivo", ["", "   ", "\t\n"])
    def test_motivo_en_blanco_no_cuenta_como_motivo(self, motivo: str) -> None:
        with pytest.raises(MotivoRequerido):
            validar_transicion(EstadoCita.AGENDADA, EstadoCita.CANCELADA, motivo=motivo)

    def test_cancelar_con_motivo_pasa(self) -> None:
        efectos = validar_transicion(
            EstadoCita.CONFIRMADA, EstadoCita.CANCELADA, motivo="El paciente viajó"
        )
        assert efectos.libera_slot

    def test_solo_cancelada_exige_motivo(self) -> None:
        assert {EstadoCita.CANCELADA} == TRANSICIONES_QUE_EXIGEN_MOTIVO
        # Every other legal transition must go through without one.
        for origen, destino in LEGALES:
            if destino is EstadoCita.CANCELADA:
                continue
            validar_transicion(origen, destino)


# ------------------------------------------------------------------- efectos


class TestEfectos:
    @pytest.mark.parametrize(("origen", "destino"), sorted(LEGALES))
    def test_los_efectos_son_consistentes_con_las_tablas(
        self, origen: EstadoCita, destino: EstadoCita
    ) -> None:
        efectos = validar_transicion(origen, destino, motivo="x")
        assert efectos.libera_slot is (destino in TRANSICIONES_QUE_LIBERAN_SLOT)
        assert efectos.genera_cargo is (destino in TRANSICIONES_QUE_GENERAN_CARGO)

    def test_solo_la_cancelacion_dispara_lista_de_espera(self) -> None:
        # A reschedule moves the same patient and a no-show happens once the
        # slot has already elapsed: neither leaves a slot someone can take.
        assert validar_transicion(
            EstadoCita.AGENDADA, EstadoCita.CANCELADA, motivo="x"
        ).dispara_lista_espera
        assert not validar_transicion(
            EstadoCita.AGENDADA, EstadoCita.REPROGRAMADA
        ).dispara_lista_espera
        assert not validar_transicion(
            EstadoCita.CONFIRMADA, EstadoCita.NO_ASISTIO
        ).dispara_lista_espera

    def test_atender_genera_cargo_y_no_libera_cupo(self) -> None:
        efectos = validar_transicion(EstadoCita.EN_ESPERA, EstadoCita.ATENDIDA)
        assert efectos.genera_cargo
        assert not efectos.libera_slot

    def test_los_efectos_son_inmutables(self) -> None:
        efectos = validar_transicion(EstadoCita.AGENDADA, EstadoCita.CONFIRMADA)
        with pytest.raises((AttributeError, TypeError)):
            efectos.libera_slot = True  # type: ignore[misc]


# ------------------------------------------------------------ property-based

estados = st.sampled_from(TODOS)


class TestPropiedades:
    @given(origen=estados, destino=estados)
    def test_validar_y_predicado_nunca_se_contradicen(
        self, origen: EstadoCita, destino: EstadoCita
    ) -> None:
        """The pure predicate and the raising validator must agree, always."""
        if es_transicion_valida(origen, destino):
            validar_transicion(origen, destino, motivo="motivo")
        else:
            with pytest.raises(TransicionInvalida):
                validar_transicion(origen, destino, motivo="motivo")

    @given(origen=estados, destino=estados)
    def test_ninguna_entrada_produce_una_excepcion_inesperada(
        self, origen: EstadoCita, destino: EstadoCita
    ) -> None:
        """Whatever happens, it is a typed domain error, never a bare crash."""
        try:
            validar_transicion(origen, destino)
        except (TransicionInvalida, MotivoRequerido) as error:
            assert error.codigo in {
                CodigoError.TRANSICION_INVALIDA,
                CodigoError.CITA_EN_ESTADO_FINAL,
                CodigoError.MOTIVO_REQUERIDO,
            }
            assert error.to_dict()["mensaje"]

    @given(origen=estados)
    def test_desde_un_estado_final_nada_es_posible(self, origen: EstadoCita) -> None:
        if es_final(origen):
            for destino in TODOS:
                assert not es_transicion_valida(origen, destino)
