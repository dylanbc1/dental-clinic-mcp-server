"""Concurrency guarantees, the tests that justify the schema.

Two agents will race for the same slot. An application-level "is it free?"
check always loses that race, because both read *free* before either writes.
These tests prove the database itself refuses the second booking.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from backend.enums import EstadoCita, EstadoSlot
from backend.models import AgendaSlot, Cita

pytestmark = pytest.mark.integration


def _cita(datos: dict[str, int], paciente: str, **extra: object) -> Cita:
    campos: dict[str, object] = {
        "paciente_id": datos[paciente],
        "profesional_id": datos["profesional_id"],
        "slot_id": datos["slot_id"],
        "estado": EstadoCita.AGENDADA,
        "creada_por": f"agente-{paciente}",
    }
    campos.update(extra)
    # A cancelled appointment needs its reason, exactly as the domain demands.
    if campos["estado"] is EstadoCita.CANCELADA:
        campos.setdefault("motivo_cancelacion", "motivo de prueba")
    return Cita(**campos)  # type: ignore[arg-type]


class TestDobleReserva:
    def test_dos_agentes_sobre_el_mismo_cupo_solo_uno_gana(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        agente_a, agente_b = sesiones(), sesiones()

        agente_a.add(_cita(datos_minimos, "paciente_a"))
        agente_a.commit()  # A gets there first

        agente_b.add(_cita(datos_minimos, "paciente_b"))
        with pytest.raises(IntegrityError):
            agente_b.commit()
        agente_b.rollback()

        # Exactly one appointment survives. Not two, not zero.
        sesion = sesiones()
        citas = sesion.query(Cita).filter_by(slot_id=datos_minimos["slot_id"]).all()
        assert len(citas) == 1
        assert citas[0].paciente_id == datos_minimos["paciente_a"]

    def test_cancelar_libera_el_cupo_para_otro_paciente(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        """The uniqueness is partial: a cancelled appointment must not keep the
        slot hostage, otherwise cancellation would be pointless."""
        sesion = sesiones()
        primera = _cita(datos_minimos, "paciente_a")
        sesion.add(primera)
        sesion.commit()

        primera.estado = EstadoCita.CANCELADA
        primera.motivo_cancelacion = "El paciente viajó"
        sesion.commit()

        sesion.add(_cita(datos_minimos, "paciente_b"))
        sesion.commit()  # must not raise

        activas = (
            sesion.query(Cita)
            .filter(Cita.slot_id == datos_minimos["slot_id"], Cita.estado != EstadoCita.CANCELADA)
            .all()
        )
        assert len(activas) == 1

    @pytest.mark.parametrize(
        "estado",
        [EstadoCita.AGENDADA, EstadoCita.CONFIRMADA, EstadoCita.EN_ESPERA, EstadoCita.ATENDIDA],
    )
    def test_todo_estado_que_ocupa_bloquea_una_segunda_cita(
        self,
        sesiones: Callable[[], Session],
        datos_minimos: dict[str, int],
        estado: EstadoCita,
    ) -> None:
        sesion = sesiones()
        sesion.add(_cita(datos_minimos, "paciente_a", estado=estado))
        sesion.commit()

        otra = sesiones()
        otra.add(_cita(datos_minimos, "paciente_b"))
        with pytest.raises(IntegrityError):
            otra.commit()
        otra.rollback()

    @pytest.mark.parametrize(
        "estado", [EstadoCita.CANCELADA, EstadoCita.REPROGRAMADA, EstadoCita.NO_ASISTIO]
    )
    def test_ningun_estado_liberador_bloquea_una_segunda_cita(
        self,
        sesiones: Callable[[], Session],
        datos_minimos: dict[str, int],
        estado: EstadoCita,
    ) -> None:
        sesion = sesiones()
        sesion.add(_cita(datos_minimos, "paciente_a", estado=estado))
        sesion.commit()

        otra = sesiones()
        otra.add(_cita(datos_minimos, "paciente_b"))
        otra.commit()  # must not raise


class TestIdempotencia:
    def test_reenviar_la_misma_clave_no_crea_un_duplicado(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        """An agent that retries a timed-out booking must not end up with two
        appointments. The database is what makes that a guarantee."""
        sesion = sesiones()
        sesion.add(_cita(datos_minimos, "paciente_a", idempotency_key="req-abc-123"))
        sesion.commit()

        reintento = sesiones()
        reintento.add(_cita(datos_minimos, "paciente_b", idempotency_key="req-abc-123"))
        with pytest.raises(IntegrityError):
            reintento.commit()
        reintento.rollback()

    def test_claves_distintas_no_se_estorban(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        sesion = sesiones()
        primera = _cita(datos_minimos, "paciente_a", idempotency_key="req-1")
        sesion.add(primera)
        sesion.commit()
        primera.estado = EstadoCita.CANCELADA
        primera.motivo_cancelacion = "cambio de plan"
        sesion.commit()

        sesion.add(_cita(datos_minimos, "paciente_b", idempotency_key="req-2"))
        sesion.commit()

    def test_varias_citas_pueden_no_tener_clave(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        """NULL is not equal to NULL in SQL: appointments created without an
        idempotency key must not collide with each other."""
        sesion = sesiones()
        primera = _cita(datos_minimos, "paciente_a")
        sesion.add(primera)
        sesion.commit()
        primera.estado = EstadoCita.CANCELADA
        primera.motivo_cancelacion = "x"
        sesion.commit()

        sesion.add(_cita(datos_minimos, "paciente_b"))
        sesion.commit()


class TestBloqueoOptimista:
    def test_dos_escrituras_sobre_el_mismo_slot_detectan_el_conflicto(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        agente_a, agente_b = sesiones(), sesiones()
        slot_a = agente_a.get(AgendaSlot, datos_minimos["slot_id"])
        slot_b = agente_b.get(AgendaSlot, datos_minimos["slot_id"])
        assert slot_a is not None and slot_b is not None
        assert slot_a.version_id == slot_b.version_id  # both read the same version

        slot_a.estado = EstadoSlot.OCUPADO
        agente_a.commit()

        slot_b.estado = EstadoSlot.BLOQUEADO
        with pytest.raises(StaleDataError):
            agente_b.commit()
        agente_b.rollback()

    def test_la_version_avanza_en_cada_escritura(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        sesion = sesiones()
        slot = sesion.get(AgendaSlot, datos_minimos["slot_id"])
        assert slot is not None
        inicial = slot.version_id

        slot.estado = EstadoSlot.OCUPADO
        sesion.commit()
        assert slot.version_id == inicial + 1

        slot.estado = EstadoSlot.LIBRE
        sesion.commit()
        assert slot.version_id == inicial + 2


class TestUnicidadDeSlot:
    def test_un_profesional_no_puede_tener_dos_slots_a_la_misma_hora(
        self, sesiones: Callable[[], Session], datos_minimos: dict[str, int]
    ) -> None:
        sesion = sesiones()
        original = sesion.get(AgendaSlot, datos_minimos["slot_id"])
        assert original is not None

        otra = sesiones()
        otra.add(
            AgendaSlot(
                profesional_id=original.profesional_id,
                fecha=original.fecha,
                inicio=original.inicio,
                fin=original.fin,
            )
        )
        with pytest.raises(IntegrityError):
            otra.commit()
        otra.rollback()
