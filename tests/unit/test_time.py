"""Timezone and business-hours tests.

America/Bogota is UTC-5 with no DST, which makes the arithmetic checkable by
hand, and makes an off-by-five-hours bug obvious the moment it appears.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.time import (
    APERTURA,
    CIERRE,
    DURACION_SLOT,
    UTC,
    ZONA_CLINICA,
    a_local,
    a_utc,
    ahora_local,
    ahora_utc,
    dentro_de_ventana_confirmacion,
    es_dia_habil,
    es_horario_habil,
    horas_hasta,
    local,
    slots_del_dia,
)

LUNES = date(2026, 8, 31)
SABADO = date(2026, 9, 5)
DOMINGO = date(2026, 9, 6)


class TestConversiones:
    def test_bogota_es_utc_menos_cinco(self) -> None:
        momento = local(LUNES, time(9, 0))
        assert a_utc(momento).hour == 14

    def test_ida_y_vuelta_conserva_el_instante(self) -> None:
        momento = local(LUNES, time(15, 30))
        assert a_local(a_utc(momento)) == momento

    @pytest.mark.parametrize("funcion", [a_local, a_utc])
    def test_un_datetime_naive_se_rechaza(self, funcion: object) -> None:
        """Guessing a timezone is how schedules silently drift."""
        with pytest.raises(ValueError, match="naive"):
            funcion(datetime(2026, 8, 31, 9, 0))  # type: ignore[operator]

    def test_ahora_siempre_trae_zona(self) -> None:
        assert ahora_utc().tzinfo is not None
        assert ahora_local().tzinfo is not None
        assert ahora_local().tzinfo is ZONA_CLINICA

    def test_ahora_utc_y_ahora_local_son_el_mismo_instante(self) -> None:
        diferencia = abs((ahora_utc() - ahora_local()).total_seconds())
        assert diferencia < 1


class TestDiasHabiles:
    @pytest.mark.parametrize(
        ("fecha", "habil"),
        [(LUNES, True), (date(2026, 9, 4), True), (SABADO, True), (DOMINGO, False)],
    )
    def test_domingo_no_es_habil(self, fecha: date, habil: bool) -> None:
        assert es_dia_habil(fecha) is habil


class TestHorarioHabil:
    @pytest.mark.parametrize(
        ("hora", "esperado"),
        [
            (time(7, 59), False),  # before opening
            (time(8, 0), True),  # opening sharp
            (time(11, 59), True),
            (time(12, 0), False),  # lunch
            (time(13, 59), False),
            (time(14, 0), True),
            (time(17, 59), True),
            (time(18, 0), False),  # closing sharp
            (time(23, 0), False),
        ],
    )
    def test_franjas_de_un_dia_entre_semana(self, hora: time, esperado: bool) -> None:
        assert es_horario_habil(local(LUNES, hora)) is esperado

    def test_sabado_solo_en_la_manana(self) -> None:
        assert es_horario_habil(local(SABADO, time(9, 0)))
        assert not es_horario_habil(local(SABADO, time(15, 0)))

    def test_domingo_nunca(self) -> None:
        assert not es_horario_habil(local(DOMINGO, time(10, 0)))

    def test_evalua_en_hora_local_no_en_utc(self) -> None:
        """14:00 UTC is 09:00 in Bogota: business hours. The naive check fails."""
        momento_utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        assert es_horario_habil(momento_utc)
        # And 02:00 UTC is 21:00 the previous day locally: closed.
        assert not es_horario_habil(datetime(2026, 9, 1, 2, 0, tzinfo=UTC))


class TestSlotsDelDia:
    def test_un_dia_entre_semana_tiene_16_slots(self) -> None:
        """8-12 and 14-18, half-hour slots."""
        assert len(slots_del_dia(LUNES)) == 16

    def test_un_sabado_tiene_8_slots(self) -> None:
        assert len(slots_del_dia(SABADO)) == 8

    def test_domingo_no_tiene_slots(self) -> None:
        assert slots_del_dia(DOMINGO) == []

    def test_los_slots_se_devuelven_en_utc(self) -> None:
        for inicio, fin in slots_del_dia(LUNES):
            assert inicio.tzinfo is UTC
            assert fin.tzinfo is UTC

    def test_ningun_slot_cae_en_el_almuerzo(self) -> None:
        for inicio, _ in slots_del_dia(LUNES):
            assert not (time(12, 0) <= a_local(inicio).time() < time(14, 0))

    def test_los_slots_no_se_solapan_y_son_contiguos_por_bloque(self) -> None:
        for (_, fin_anterior), (inicio, _) in itertools.pairwise(slots_del_dia(LUNES)):
            assert inicio >= fin_anterior

    def test_cada_slot_dura_lo_declarado(self) -> None:
        for inicio, fin in slots_del_dia(LUNES):
            assert fin - inicio == DURACION_SLOT

    def test_el_primero_abre_y_el_ultimo_cierra(self) -> None:
        slots = slots_del_dia(LUNES)
        assert a_local(slots[0][0]).time() == APERTURA
        assert a_local(slots[-1][1]).time() == CIERRE

    def test_todo_slot_generado_cae_en_horario_habil(self) -> None:
        for fecha in (LUNES, SABADO):
            for inicio, _ in slots_del_dia(fecha):
                assert es_horario_habil(inicio)


class TestVentanaDeConfirmacion:
    AHORA = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("horas", "dentro"),
        [(-1, False), (0, True), (1, True), (24, True), (48, True), (49, False), (72, False)],
    )
    def test_la_ventana_es_de_48_horas(self, horas: float, dentro: bool) -> None:
        cita = self.AHORA + timedelta(hours=horas)
        assert dentro_de_ventana_confirmacion(cita, ahora=self.AHORA) is dentro

    def test_una_cita_pasada_queda_fuera(self) -> None:
        pasada = self.AHORA - timedelta(days=3)
        assert not dentro_de_ventana_confirmacion(pasada, ahora=self.AHORA)


class TestHorasHasta:
    def test_signo_positivo_hacia_el_futuro(self) -> None:
        ahora = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert horas_hasta(ahora + timedelta(hours=5), desde=ahora) == 5

    def test_signo_negativo_hacia_el_pasado(self) -> None:
        ahora = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert horas_hasta(ahora - timedelta(hours=2), desde=ahora) == -2

    def test_funciona_entre_zonas_distintas(self) -> None:
        utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        bogota = local(LUNES, time(9, 0))  # the same instant
        assert horas_hasta(utc, desde=bogota) == 0


class TestPropiedades:
    fechas = st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))

    @given(fecha=fechas)
    def test_todo_slot_generado_es_valido(self, fecha: date) -> None:
        for inicio, fin in slots_del_dia(fecha):
            assert fin > inicio
            assert fin - inicio == DURACION_SLOT
            assert es_horario_habil(inicio)
            assert a_local(inicio).date() == fecha

    @given(fecha=fechas)
    def test_ningun_slot_en_domingo(self, fecha: date) -> None:
        if fecha.weekday() == 6:
            assert slots_del_dia(fecha) == []

    @given(
        fecha=fechas,
        hora=st.times(),
    )
    def test_la_conversion_es_involutiva(self, fecha: date, hora: object) -> None:
        momento = local(fecha, hora)  # type: ignore[arg-type]
        assert a_utc(a_local(momento)) == a_utc(momento)
