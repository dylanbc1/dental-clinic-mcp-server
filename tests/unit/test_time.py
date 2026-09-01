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
    CLINIC_TZ,
    CLOSING,
    OPENING,
    SLOT_LENGTH,
    UTC,
    hours_until,
    is_within_hours,
    is_working_day,
    local,
    now_at_clinic,
    now_utc,
    slots_for_day,
    to_clinic_time,
    to_utc,
    within_confirmation_window,
)

LUNES = date(2026, 8, 31)
SABADO = date(2026, 9, 5)
DOMINGO = date(2026, 9, 6)


class TestConversiones:
    def test_bogota_es_utc_menos_cinco(self) -> None:
        occurred_at = local(LUNES, time(9, 0))
        assert to_utc(occurred_at).hour == 14

    def test_ida_y_vuelta_conserva_el_instante(self) -> None:
        occurred_at = local(LUNES, time(15, 30))
        assert to_clinic_time(to_utc(occurred_at)) == occurred_at

    @pytest.mark.parametrize("funcion", [to_clinic_time, to_utc])
    def test_un_datetime_naive_se_rechaza(self, funcion: object) -> None:
        """Guessing a timezone is how schedules silently drift."""
        with pytest.raises(ValueError, match="naive"):
            funcion(datetime(2026, 8, 31, 9, 0))  # type: ignore[operator]

    def test_ahora_siempre_trae_zona(self) -> None:
        assert now_utc().tzinfo is not None
        assert now_at_clinic().tzinfo is not None
        assert now_at_clinic().tzinfo is CLINIC_TZ

    def test_ahora_utc_y_ahora_local_son_el_mismo_instante(self) -> None:
        diferencia = abs((now_utc() - now_at_clinic()).total_seconds())
        assert diferencia < 1


class TestDiasHabiles:
    @pytest.mark.parametrize(
        ("day", "habil"),
        [(LUNES, True), (date(2026, 9, 4), True), (SABADO, True), (DOMINGO, False)],
    )
    def test_domingo_no_es_habil(self, day: date, habil: bool) -> None:
        assert is_working_day(day) is habil


class TestHorarioHabil:
    @pytest.mark.parametrize(
        ("hora", "expected"),
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
    def test_franjas_de_un_dia_entre_semana(self, hora: time, expected: bool) -> None:
        assert is_within_hours(local(LUNES, hora)) is expected

    def test_sabado_solo_en_la_manana(self) -> None:
        assert is_within_hours(local(SABADO, time(9, 0)))
        assert not is_within_hours(local(SABADO, time(15, 0)))

    def test_domingo_nunca(self) -> None:
        assert not is_within_hours(local(DOMINGO, time(10, 0)))

    def test_evalua_en_hora_local_no_en_utc(self) -> None:
        """14:00 UTC is 09:00 in Bogota: business hours. The naive check fails."""
        momento_utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        assert is_within_hours(momento_utc)
        # And 02:00 UTC is 21:00 the previous day locally: closed.
        assert not is_within_hours(datetime(2026, 9, 1, 2, 0, tzinfo=UTC))


class TestSlotsDelDia:
    def test_un_dia_entre_semana_tiene_16_slots(self) -> None:
        """8-12 and 14-18, half-hour slots."""
        assert len(slots_for_day(LUNES)) == 16

    def test_un_sabado_tiene_8_slots(self) -> None:
        assert len(slots_for_day(SABADO)) == 8

    def test_domingo_no_tiene_slots(self) -> None:
        assert slots_for_day(DOMINGO) == []

    def test_los_slots_se_devuelven_en_utc(self) -> None:
        for start, end in slots_for_day(LUNES):
            assert start.tzinfo is UTC
            assert end.tzinfo is UTC

    def test_ningun_slot_cae_en_el_almuerzo(self) -> None:
        for start, _ in slots_for_day(LUNES):
            assert not (time(12, 0) <= to_clinic_time(start).time() < time(14, 0))

    def test_los_slots_no_se_solapan_y_son_contiguos_por_bloque(self) -> None:
        for (_, fin_anterior), (start, _) in itertools.pairwise(slots_for_day(LUNES)):
            assert start >= fin_anterior

    def test_cada_slot_dura_lo_declarado(self) -> None:
        for start, end in slots_for_day(LUNES):
            assert end - start == SLOT_LENGTH

    def test_el_primero_abre_y_el_ultimo_cierra(self) -> None:
        slots = slots_for_day(LUNES)
        assert to_clinic_time(slots[0][0]).time() == OPENING
        assert to_clinic_time(slots[-1][1]).time() == CLOSING

    def test_todo_slot_generado_cae_en_horario_habil(self) -> None:
        for day in (LUNES, SABADO):
            for start, _ in slots_for_day(day):
                assert is_within_hours(start)


class TestVentanaDeConfirmacion:
    AHORA = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("horas", "dentro"),
        [(-1, False), (0, True), (1, True), (24, True), (48, True), (49, False), (72, False)],
    )
    def test_la_ventana_es_de_48_horas(self, horas: float, dentro: bool) -> None:
        appointment = self.AHORA + timedelta(hours=horas)
        assert within_confirmation_window(appointment, now=self.AHORA) is dentro

    def test_una_cita_pasada_queda_fuera(self) -> None:
        pasada = self.AHORA - timedelta(days=3)
        assert not within_confirmation_window(pasada, now=self.AHORA)


class TestHorasHasta:
    def test_signo_positivo_hacia_el_futuro(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert hours_until(now + timedelta(hours=5), since=now) == 5

    def test_signo_negativo_hacia_el_pasado(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert hours_until(now - timedelta(hours=2), since=now) == -2

    def test_funciona_entre_zonas_distintas(self) -> None:
        utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        bogota = local(LUNES, time(9, 0))  # the same instant
        assert hours_until(utc, since=bogota) == 0


class TestPropiedades:
    fechas = st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))

    @given(day=fechas)
    def test_todo_slot_generado_es_valido(self, day: date) -> None:
        for start, end in slots_for_day(day):
            assert end > start
            assert end - start == SLOT_LENGTH
            assert is_within_hours(start)
            assert to_clinic_time(start).date() == day

    @given(day=fechas)
    def test_ningun_slot_en_domingo(self, day: date) -> None:
        if day.weekday() == 6:
            assert slots_for_day(day) == []

    @given(
        day=fechas,
        hora=st.times(),
    )
    def test_la_conversion_es_involutiva(self, day: date, hora: object) -> None:
        occurred_at = local(day, hora)  # type: ignore[arg-type]
        assert to_utc(to_clinic_time(occurred_at)) == to_utc(occurred_at)
