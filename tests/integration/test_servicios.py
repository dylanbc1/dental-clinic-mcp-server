"""Service-layer tests: the write rules applied to persisted state.

Three invariants get checked over and over here, because they are the ones that
would silently break: the transition was validated, the audit row exists, and
the derived effects (slot release, charge, waiting list) actually happened.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.errores import (
    CitaNoEncontrada,
    ConsentimientoRequerido,
    EspecialidadNoCoincide,
    ListaEsperaVacia,
    MotivoRequerido,
    PacienteNoEncontrado,
    PacienteYaTieneCita,
    ProfesionalNoEncontrado,
    SlotEnElPasado,
    SlotNoDisponible,
    SlotNoEncontrado,
    TransicionInvalida,
    YaEnListaEspera,
)
from backend.domain.servicios import (
    agenda_del_dia,
    agendar_cita,
    buscar_pacientes,
    cambiar_estado,
    cancelar_cita,
    confirmar_cita,
    consultar_cartera,
    consultar_disponibilidad,
    inscribir_en_lista_espera,
    listar_citas_paciente,
    obtener_cita,
    obtener_clinica,
    obtener_paciente,
    ofrecer_cupo_lista_espera,
    registrar_asistencia,
    registrar_motivo_consulta,
    reprogramar_cita,
    validar_afiliacion_paciente,
)
from backend.enums import (
    ConceptoCargo,
    Especialidad,
    EstadoCargo,
    EstadoCartera,
    EstadoCita,
    EstadoListaEspera,
    EstadoSlot,
    PrioridadListaEspera,
    Regimen,
)
from backend.models import AgendaSlot, Cargo, Cita, CitaHistorial, ListaEspera
from tests.conftest import Escenario

pytestmark = pytest.mark.integration

ACTOR = "recepcion@clinica.test"


def historial_de(sesion: Session, cita_id: int) -> list[CitaHistorial]:
    return list(
        sesion.scalars(
            select(CitaHistorial).where(CitaHistorial.cita_id == cita_id).order_by(CitaHistorial.id)
        )
    )


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


class TestBusquedas:
    def test_busca_por_documento_exacto(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        assert [p.id for p in buscar_pacientes(s, documento="11111111")] == [escenario.ana_id]

    def test_el_documento_no_hace_match_parcial(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """A partial document match is how you hand an agent the wrong record."""
        assert buscar_pacientes(sesiones(), documento="1111") == []

    def test_busca_por_nombre_sin_distinguir_mayusculas(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        assert [p.id for p in buscar_pacientes(sesiones(), nombre="ANA gómez")] == [
            escenario.ana_id
        ]

    def test_busca_por_fragmento_de_nombre(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        assert len(buscar_pacientes(sesiones(), nombre="Ruiz")) == 2

    def test_sin_criterio_lanza_error_accionable(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(PacienteNoEncontrado) as exc:
            buscar_pacientes(sesiones())
        assert "buscar_paciente" in (exc.value.sugerencia or "")

    def test_respeta_el_limite(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        assert len(buscar_pacientes(sesiones(), nombre="a", limite=2)) <= 2

    def test_paciente_inexistente(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(PacienteNoEncontrado):
            obtener_paciente(sesiones(), 999_999)

    def test_cita_inexistente(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        with pytest.raises(CitaNoEncontrada) as exc:
            obtener_cita(sesiones(), 999_999)
        assert "listar_citas_paciente" in (exc.value.sugerencia or "")

    def test_hay_clinica(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        assert obtener_clinica(sesiones()).id == escenario.clinica_id


class TestDisponibilidad:
    def test_devuelve_solo_cupos_libres_y_futuros(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        libres = consultar_disponibilidad(sesiones())
        ids = {s.slot_id for s in libres}
        assert escenario.slot_pasado_id not in ids
        assert ids >= set(escenario.slots_general[:3])

    def test_filtra_por_especialidad(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        libres = consultar_disponibilidad(sesiones(), especialidad=Especialidad.ORTODONCIA)
        assert libres
        assert all(s.especialidad is Especialidad.ORTODONCIA for s in libres)

    def test_filtra_por_fecha(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        assert consultar_disponibilidad(sesiones(), fecha=escenario.fecha_futura)
        assert consultar_disponibilidad(sesiones(), fecha=date(2000, 1, 3)) == []

    def test_filtra_por_profesional(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        libres = consultar_disponibilidad(sesiones(), profesional_id=escenario.orto_id)
        assert all(s.profesional_id == escenario.orto_id for s in libres)

    def test_profesional_inexistente_es_un_error_no_una_lista_vacia(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(ProfesionalNoEncontrado):
            consultar_disponibilidad(sesiones(), profesional_id=999_999)

    def test_los_cupos_vienen_en_orden_cronologico(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        libres = consultar_disponibilidad(sesiones())
        assert [s.inicio for s in libres] == sorted(s.inicio for s in libres)

    def test_la_etiqueta_esta_en_hora_local(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        etiqueta = consultar_disponibilidad(sesiones())[0].etiqueta
        assert etiqueta.startswith(str(escenario.fecha_futura))


class TestAfiliacionYCartera:
    def test_afiliacion_activa(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        r = validar_afiliacion_paciente(sesiones(), escenario.ana_id)
        assert r.activa and r.requiere_copago

    def test_afiliacion_inactiva_cae_a_particular(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        r = validar_afiliacion_paciente(sesiones(), escenario.bruno_id)
        assert not r.activa
        assert r.regimen_efectivo is Regimen.PARTICULAR

    def test_cartera_al_dia(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        assert consultar_cartera(sesiones(), escenario.ana_id).estado is EstadoCartera.AL_DIA

    def test_cartera_en_mora(self, sesiones: Callable[[], Session], escenario: Escenario) -> None:
        resumen = consultar_cartera(sesiones(), escenario.deudor_id)
        assert resumen.estado is EstadoCartera.EN_MORA
        assert resumen.total_vencido == Decimal("180000")
        assert resumen.dias_mora_maximo >= 74


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #


class TestAgendar:
    def test_crea_la_cita_ocupa_el_cupo_y_audita(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        resultado = agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()

        assert resultado.cita.estado is EstadoCita.AGENDADA
        assert resultado.cita.creada_por == ACTOR
        assert s.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.OCUPADO

        historial = historial_de(s, resultado.cita.id)
        assert len(historial) == 1
        assert historial[0].estado_anterior is None
        assert historial[0].estado_nuevo is EstadoCita.AGENDADA
        assert historial[0].usuario == ACTOR

    def test_devuelve_la_afiliacion_para_informar_la_tarifa(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        resultado = agendar_cita(
            sesiones(),
            paciente_id=escenario.bruno_id,
            slot_id=escenario.slots_general[0],
            usuario=ACTOR,
        )
        assert resultado.afiliacion.regimen_efectivo is Regimen.PARTICULAR

    def test_la_mora_alerta_pero_no_bloquea(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """The rule from §2.3 that a naive implementation gets wrong."""
        resultado = agendar_cita(
            sesiones(),
            paciente_id=escenario.deudor_id,
            slot_id=escenario.slots_general[0],
            usuario=ACTOR,
        )
        assert resultado.cita.id is not None
        assert resultado.alerta_cartera is not None
        assert "can still be booked" in resultado.alerta_cartera

    def test_sin_mora_no_hay_alerta(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        resultado = agendar_cita(
            sesiones(),
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario=ACTOR,
        )
        assert resultado.alerta_cartera is None

    def test_un_cupo_ocupado_sugiere_alternativas_concretas(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()

        with pytest.raises(SlotNoDisponible) as exc:
            agendar_cita(
                s,
                paciente_id=escenario.carla_id,
                slot_id=escenario.slots_general[0],
                usuario=ACTOR,
            )
        # An LLM that receives named alternatives recovers on its own turn.
        assert exc.value.detalles["alternativas"]
        assert "closest free slots" in (exc.value.sugerencia or "")

    def test_un_cupo_pasado_se_rechaza(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(SlotEnElPasado):
            agendar_cita(
                sesiones(),
                paciente_id=escenario.ana_id,
                slot_id=escenario.slot_pasado_id,
                usuario=ACTOR,
            )

    def test_un_cupo_inexistente_se_rechaza(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(SlotNoEncontrado):
            agendar_cita(sesiones(), paciente_id=escenario.ana_id, slot_id=999_999, usuario=ACTOR)

    def test_la_especialidad_esperada_se_verifica(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """Guards against the model picking a slot from the wrong list."""
        with pytest.raises(EspecialidadNoCoincide) as exc:
            agendar_cita(
                sesiones(),
                paciente_id=escenario.ana_id,
                slot_id=escenario.slots_general[0],
                usuario=ACTOR,
                especialidad_esperada=Especialidad.ORTODONCIA,
            )
        assert exc.value.detalles["especialidad_del_cupo"] == "odontologia_general"

    def test_la_especialidad_correcta_pasa(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        agendar_cita(
            sesiones(),
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_orto[0],
            usuario=ACTOR,
            especialidad_esperada=Especialidad.ORTODONCIA,
        )

    def test_no_se_puede_agendar_dos_citas_solapadas_al_mismo_paciente(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """Same time slot, two professionals: the patient cannot be in both."""
        s = sesiones()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        with pytest.raises(PacienteYaTieneCita) as exc:
            agendar_cita(
                s, paciente_id=escenario.ana_id, slot_id=escenario.slots_orto[0], usuario=ACTOR
            )
        assert "cita_existente_id" in exc.value.detalles

    def test_horarios_distintos_si_se_permiten(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[1], usuario=ACTOR
        )
        s.commit()
        assert len(listar_citas_paciente(s, escenario.ana_id)) == 2


class TestIdempotencia:
    def test_la_misma_clave_devuelve_la_misma_cita(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        primera = agendar_cita(
            s,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario=ACTOR,
            idempotency_key="peticion-1",
        )
        s.commit()
        segunda = agendar_cita(
            s,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[1],
            usuario=ACTOR,
            idempotency_key="peticion-1",
        )
        assert segunda.cita.id == primera.cita.id
        assert segunda.reutilizada is True
        assert s.scalar(select(func.count()).select_from(Cita)) == 1

    def test_sin_clave_cada_llamada_crea_una_cita(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        agendar_cita(
            s, paciente_id=escenario.carla_id, slot_id=escenario.slots_general[1], usuario=ACTOR
        )
        s.commit()
        assert s.scalar(select(func.count()).select_from(Cita)) == 2


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


@pytest.fixture
def cita_agendada(sesiones: Callable[[], Session], escenario: Escenario) -> tuple[Session, int]:
    s = sesiones()
    resultado = agendar_cita(
        s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[0], usuario=ACTOR
    )
    s.commit()
    return s, resultado.cita.id


class TestConfirmar:
    def test_confirma_y_audita(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        resultado = confirmar_cita(s, cita_id, usuario=ACTOR)
        s.commit()
        assert resultado.cita.estado is EstadoCita.CONFIRMADA
        historial = historial_de(s, cita_id)
        assert historial[-1].estado_anterior is EstadoCita.AGENDADA
        assert historial[-1].estado_nuevo is EstadoCita.CONFIRMADA

    def test_no_libera_el_cupo_ni_genera_cargo(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        resultado = confirmar_cita(s, cita_id, usuario=ACTOR)
        assert not resultado.efectos.libera_slot
        assert resultado.cargo_generado is None

    def test_confirmar_dos_veces_falla_con_error_tipado(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        s.commit()
        with pytest.raises(TransicionInvalida):
            confirmar_cita(s, cita_id, usuario=ACTOR)


class TestCancelar:
    def test_cancela_libera_el_cupo_y_guarda_el_motivo(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        resultado = cancelar_cita(s, cita_id, motivo="El paciente viajó", usuario=ACTOR)
        s.commit()

        assert resultado.cita.estado is EstadoCita.CANCELADA
        assert resultado.cita.motivo_cancelacion == "El paciente viajó"
        assert resultado.efectos.libera_slot
        assert s.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE
        assert historial_de(s, cita_id)[-1].motivo == "El paciente viajó"

    def test_sin_motivo_se_rechaza(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        with pytest.raises(MotivoRequerido):
            cambiar_estado(s, cita_id, EstadoCita.CANCELADA, usuario=ACTOR)

    def test_el_cupo_liberado_vuelve_a_estar_disponible(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        cancelar_cita(s, cita_id, motivo="cambio de planes", usuario=ACTOR)
        s.commit()
        libres = {x.slot_id for x in consultar_disponibilidad(s)}
        assert escenario.slots_general[0] in libres

    def test_sin_lista_de_espera_no_hay_siguiente(self, cita_agendada: tuple[Session, int]) -> None:
        """Cancelling with an empty queue is normal, not an error."""
        s, cita_id = cita_agendada
        resultado = cancelar_cita(s, cita_id, motivo="x", usuario=ACTOR)
        assert resultado.siguiente_en_espera is None

    def test_con_lista_de_espera_devuelve_al_siguiente(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        inscribir_en_lista_espera(
            s,
            paciente_id=escenario.carla_id,
            especialidad=Especialidad.ODONTOLOGIA_GENERAL,
        )
        s.commit()
        resultado = cancelar_cita(s, cita_id, motivo="x", usuario=ACTOR)
        assert resultado.siguiente_en_espera is not None
        assert resultado.siguiente_en_espera.paciente_id == escenario.carla_id

    def test_no_se_ofrece_el_cupo_a_quien_lo_liberó(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ODONTOLOGIA_GENERAL
        )
        s.commit()
        resultado = cancelar_cita(s, cita_id, motivo="x", usuario=ACTOR)
        assert resultado.siguiente_en_espera is None


class TestAsistencia:
    def test_flujo_completo_hasta_atendida_genera_cargo(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.EN_ESPERA, usuario=ACTOR)
        resultado = registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)
        s.commit()

        assert resultado.cita.estado is EstadoCita.ATENDIDA
        assert resultado.cargo_generado is not None
        # Ana is contributory level 1 → cuota moderadora, not full tariff.
        assert resultado.cargo_generado.concepto is ConceptoCargo.CUOTA_MODERADORA
        assert resultado.cargo_generado.monto == Decimal("5500")
        assert resultado.cargo_generado.estado is EstadoCargo.PENDIENTE

    def test_el_cargo_vence_a_30_dias(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.EN_ESPERA, usuario=ACTOR)
        resultado = registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)
        cita = resultado.cita
        assert resultado.cargo_generado is not None
        esperado = cita.slot.inicio.date() + timedelta(days=30)
        assert abs((resultado.cargo_generado.vencimiento - esperado).days) <= 1

    def test_afiliacion_inactiva_liquida_tarifa_particular(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        cita = agendar_cita(
            s, paciente_id=escenario.bruno_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        ).cita
        confirmar_cita(s, cita.id, usuario=ACTOR)
        registrar_asistencia(s, cita.id, EstadoCita.EN_ESPERA, usuario=ACTOR)
        resultado = registrar_asistencia(s, cita.id, EstadoCita.ATENDIDA, usuario=ACTOR)
        s.commit()
        assert resultado.cargo_generado is not None
        assert resultado.cargo_generado.concepto is ConceptoCargo.PARTICULAR
        assert resultado.cargo_generado.monto == Decimal("120000")

    def test_no_show_desde_confirmada_penaliza(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        resultado = registrar_asistencia(s, cita_id, EstadoCita.NO_ASISTIO, usuario=ACTOR)
        s.commit()
        assert resultado.cargo_generado is not None
        assert resultado.cargo_generado.concepto is ConceptoCargo.NO_SHOW
        assert resultado.cargo_generado.monto == Decimal("40000")

    def test_no_show_sin_confirmar_no_penaliza(self, cita_agendada: tuple[Session, int]) -> None:
        """The default policy only charges a patient who had committed."""
        s, cita_id = cita_agendada
        resultado = registrar_asistencia(s, cita_id, EstadoCita.NO_ASISTIO, usuario=ACTOR)
        s.commit()
        assert resultado.cargo_generado is None

    def test_el_no_show_libera_el_cupo(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        registrar_asistencia(s, cita_id, EstadoCita.NO_ASISTIO, usuario=ACTOR)
        s.commit()
        assert s.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    def test_saltarse_en_espera_se_rechaza(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        with pytest.raises(TransicionInvalida):
            registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)

    def test_el_cargo_aparece_en_la_cartera(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.EN_ESPERA, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)
        s.commit()
        resumen = consultar_cartera(s, escenario.ana_id)
        assert resumen.cantidad_cargos == 1
        assert resumen.total_pendiente == Decimal("5500")


class TestReprogramar:
    def test_libera_el_viejo_ocupa_el_nuevo_y_encadena(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        resultado = reprogramar_cita(
            s, cita_id, escenario.slots_general[2], usuario=ACTOR, motivo="Choque de agenda"
        )
        s.commit()

        original = obtener_cita(s, cita_id)
        assert original.estado is EstadoCita.REPROGRAMADA
        assert s.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE
        assert s.get(AgendaSlot, escenario.slots_general[2]).estado is EstadoSlot.OCUPADO

        nueva = resultado.cita
        assert nueva.id != cita_id
        assert nueva.estado is EstadoCita.AGENDADA
        assert nueva.cita_origen_id == cita_id

    def test_la_nueva_cita_tiene_su_propio_historial(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        nueva = reprogramar_cita(s, cita_id, escenario.slots_general[2], usuario=ACTOR).cita
        s.commit()
        historial = historial_de(s, nueva.id)
        assert len(historial) == 1
        assert "Rescheduled from" in (historial[0].motivo or "")

    def test_no_reprograma_a_un_cupo_ocupado(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        agendar_cita(
            s, paciente_id=escenario.carla_id, slot_id=escenario.slots_general[3], usuario=ACTOR
        )
        s.commit()
        with pytest.raises(SlotNoDisponible):
            reprogramar_cita(s, cita_id, escenario.slots_general[3], usuario=ACTOR)

    def test_no_reprograma_al_pasado(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        with pytest.raises(SlotEnElPasado):
            reprogramar_cita(s, cita_id, escenario.slot_pasado_id, usuario=ACTOR)

    def test_una_cita_reprogramada_es_final(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        s, cita_id = cita_agendada
        reprogramar_cita(s, cita_id, escenario.slots_general[2], usuario=ACTOR)
        s.commit()
        with pytest.raises(TransicionInvalida):
            confirmar_cita(s, cita_id, usuario=ACTOR)


# --------------------------------------------------------------------------- #
# Waiting list
# --------------------------------------------------------------------------- #


class TestListaEspera:
    def test_inscribe_y_evita_duplicados(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ORTODONCIA
        )
        s.commit()
        with pytest.raises(YaEnListaEspera):
            inscribir_en_lista_espera(
                s, paciente_id=escenario.ana_id, especialidad=Especialidad.ORTODONCIA
            )

    def test_paciente_inexistente_no_se_inscribe(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(PacienteNoEncontrado):
            inscribir_en_lista_espera(
                sesiones(), paciente_id=999_999, especialidad=Especialidad.ORTODONCIA
            )

    def test_ofrece_el_cupo_al_primero_de_la_cola(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ORTODONCIA
        )
        inscribir_en_lista_espera(
            s,
            paciente_id=escenario.carla_id,
            especialidad=Especialidad.ORTODONCIA,
            prioridad=PrioridadListaEspera.URGENCIA,
        )
        s.commit()

        oferta = ofrecer_cupo_lista_espera(s, escenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        # Urgency jumps the queue even though Ana enrolled first.
        assert oferta.paciente.id == escenario.carla_id
        assert oferta.posicion_original == 1
        assert oferta.entrada.estado is EstadoListaEspera.OFRECIDA
        assert oferta.entrada.slot_ofrecido_id == escenario.slots_orto[0]

    def test_ofrecer_no_agenda_nada(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """Offering is a contact instruction, not a booking. Booking the slot is
        a separate decision that gets its own approval."""
        s = sesiones()
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ORTODONCIA
        )
        s.commit()
        ofrecer_cupo_lista_espera(s, escenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Cita)) == 0
        assert s.get(AgendaSlot, escenario.slots_orto[0]).estado is EstadoSlot.LIBRE

    def test_lista_vacia_da_un_error_accionable(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        with pytest.raises(ListaEsperaVacia) as exc:
            ofrecer_cupo_lista_espera(sesiones(), escenario.slots_orto[0], usuario=ACTOR)
        assert "consultar_disponibilidad" in (exc.value.sugerencia or "")

    def test_no_ofrece_de_otra_especialidad(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ENDODONCIA
        )
        s.commit()
        with pytest.raises(ListaEsperaVacia):
            ofrecer_cupo_lista_espera(s, escenario.slots_orto[0], usuario=ACTOR)

    def test_una_entrada_ofrecida_sale_de_la_cola(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        inscribir_en_lista_espera(
            s, paciente_id=escenario.ana_id, especialidad=Especialidad.ORTODONCIA
        )
        s.commit()
        ofrecer_cupo_lista_espera(s, escenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        activas = s.scalars(
            select(ListaEspera).where(ListaEspera.estado == EstadoListaEspera.ACTIVA)
        ).all()
        assert activas == []


# --------------------------------------------------------------------------- #
# Clinical
# --------------------------------------------------------------------------- #


class TestMotivoDeConsulta:
    def test_con_consentimiento_registra_y_audita(self, cita_agendada: tuple[Session, int]) -> None:
        s, cita_id = cita_agendada
        cita = registrar_motivo_consulta(
            s, cita_id, "Dolor en molar inferior derecho", usuario="odontologa@clinica.test"
        )
        s.commit()

        assert cita.motivo == "Dolor en molar inferior derecho"
        assert cita.motivo_registrado_por == "odontologa@clinica.test"
        assert cita.motivo_registrado_en is not None

        ultimo = historial_de(s, cita_id)[-1]
        assert "clinical data" in (ultimo.motivo or "")
        assert ultimo.usuario == "odontologa@clinica.test"

    def test_sin_consentimiento_se_rechaza(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        """Carla has no consent on file. Scope alone must not be enough."""
        s = sesiones()
        cita = agendar_cita(
            s, paciente_id=escenario.carla_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        ).cita
        s.commit()
        with pytest.raises(ConsentimientoRequerido) as exc:
            registrar_motivo_consulta(s, cita.id, "Dolor", usuario=ACTOR)
        assert "2654" in (exc.value.sugerencia or "")
        assert exc.value.http_status == 403

    def test_el_rechazo_no_deja_rastro_del_motivo(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        cita = agendar_cita(
            s, paciente_id=escenario.carla_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        ).cita
        s.commit()
        with pytest.raises(ConsentimientoRequerido):
            registrar_motivo_consulta(s, cita.id, "Dolor agudo", usuario=ACTOR)
        s.rollback()
        assert obtener_cita(s, cita.id).motivo is None

    def test_el_registro_clinico_no_cambia_el_estado(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        s, cita_id = cita_agendada
        antes = obtener_cita(s, cita_id).estado
        registrar_motivo_consulta(s, cita_id, "Control de rutina", usuario=ACTOR)
        s.commit()
        assert obtener_cita(s, cita_id).estado is antes


# --------------------------------------------------------------------------- #
# Day view
# --------------------------------------------------------------------------- #


class TestAgendaDelDia:
    def test_lista_las_citas_del_dia_en_orden(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        s = sesiones()
        agendar_cita(
            s, paciente_id=escenario.ana_id, slot_id=escenario.slots_general[1], usuario=ACTOR
        )
        agendar_cita(
            s, paciente_id=escenario.carla_id, slot_id=escenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        citas = agenda_del_dia(s, escenario.fecha_futura)
        assert [c.slot.inicio for c in citas] == sorted(c.slot.inicio for c in citas)
        assert len(citas) == 2

    def test_un_dia_sin_citas_devuelve_vacio(
        self, sesiones: Callable[[], Session], escenario: Escenario
    ) -> None:
        assert agenda_del_dia(sesiones(), date(2000, 1, 3)) == []

    def test_incluye_las_canceladas(
        self, cita_agendada: tuple[Session, int], escenario: Escenario
    ) -> None:
        """The front desk needs to see what was cancelled today, not a clean slate."""
        s, cita_id = cita_agendada
        cancelar_cita(s, cita_id, motivo="x", usuario=ACTOR)
        s.commit()
        citas = agenda_del_dia(s, escenario.fecha_futura)
        assert [c.estado for c in citas] == [EstadoCita.CANCELADA]


class TestAuditoriaCompleta:
    def test_cada_transicion_deja_exactamente_una_fila(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.EN_ESPERA, usuario=ACTOR)
        registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)
        s.commit()

        historial = historial_de(s, cita_id)
        assert [h.estado_nuevo for h in historial] == [
            EstadoCita.AGENDADA,
            EstadoCita.CONFIRMADA,
            EstadoCita.EN_ESPERA,
            EstadoCita.ATENDIDA,
        ]
        assert all(h.usuario == ACTOR for h in historial)

    def test_una_transicion_rechazada_no_deja_rastro(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        s, cita_id = cita_agendada
        antes = len(historial_de(s, cita_id))
        with pytest.raises(TransicionInvalida):
            registrar_asistencia(s, cita_id, EstadoCita.ATENDIDA, usuario=ACTOR)
        s.rollback()
        assert len(historial_de(s, cita_id)) == antes

    def test_el_actor_queda_registrado_por_operacion(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        """An audit trail with the same user in every row is not an audit trail."""
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario="ana@clinica.test")
        cancelar_cita(s, cita_id, motivo="x", usuario="jefe@clinica.test")
        s.commit()
        usuarios = [h.usuario for h in historial_de(s, cita_id)]
        assert usuarios == [ACTOR, "ana@clinica.test", "jefe@clinica.test"]

    def test_todo_cargo_derivado_de_una_cita_queda_ligado_a_ella(
        self, cita_agendada: tuple[Session, int]
    ) -> None:
        """A charge produced by a transition must point back at it, so the
        patient can be told *what* they are being billed for. (Standalone
        charges with no appointment are legitimate, imported debt for instance,
        so the assertion is scoped to the generated one.)"""
        s, cita_id = cita_agendada
        confirmar_cita(s, cita_id, usuario=ACTOR)
        resultado = registrar_asistencia(s, cita_id, EstadoCita.NO_ASISTIO, usuario=ACTOR)
        s.commit()
        assert resultado.cargo_generado is not None
        assert resultado.cargo_generado.cita_id == cita_id
        assert all(c.paciente_id is not None for c in s.scalars(select(Cargo)))
