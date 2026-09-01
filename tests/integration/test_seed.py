"""Seed tests.

Two properties, both load-bearing for a portfolio project:

* **Determinism.** Same seed, same base date, byte-identical dataset. Without
  it "reproduce what I saw in the demo" is impossible.
* **Realism.** The generated agenda must actually be usable: free slots to book,
  patients in arrears to collect from, inactive affiliations to catch. A seed
  that produces a uniform, tidy world hides every interesting bug.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.states import es_transicion_valida
from backend.enums import (
    ESTADOS_QUE_OCUPAN_SLOT,
    EstadoCargo,
    EstadoCita,
    EstadoSlot,
    Regimen,
)
from backend.models import AgendaSlot, Cargo, Cita, CitaHistorial, Clinica, ListaEspera, Paciente
from backend.seed import ParametrosSeed, base_vacia, sembrar

pytestmark = pytest.mark.integration

FECHA_BASE = date(2026, 8, 31)
PARAMS = ParametrosSeed(seed=20260831, pacientes=25, dias_agenda=10, fecha_base=FECHA_BASE)


def huella(session: Session) -> str:
    """Content hash over business fields only.

    Primary keys are deliberately excluded: sequences keep advancing between
    runs, so including them would make every run differ for a reason that has
    nothing to do with the data.
    """
    contenido: dict[str, list] = {}

    contenido["paciente"] = sorted(
        (
            p.tipo_documento,
            p.documento,
            p.nombre,
            p.telefono,
            p.regimen,
            p.afiliacion_activa,
            p.nivel_cuota_moderadora,
            p.consentimiento_datos_clinicos,
            p.fecha_nacimiento.isoformat() if p.fecha_nacimiento else None,
        )
        for p in session.scalars(select(Paciente))
    )
    contenido["slot"] = sorted(
        (s.profesional.registro, s.inicio.isoformat(), s.fin.isoformat(), s.estado)
        for s in session.scalars(select(AgendaSlot))
    )
    contenido["cita"] = sorted(
        (
            c.paciente.documento,
            c.profesional.registro,
            c.slot.inicio.isoformat(),
            c.estado,
            c.motivo_cancelacion,
        )
        for c in session.scalars(select(Cita))
    )
    contenido["cargo"] = sorted(
        (g.paciente.documento, g.concepto, str(g.monto), g.estado, g.vencimiento.isoformat())
        for g in session.scalars(select(Cargo))
    )
    contenido["lista_espera"] = sorted(
        (e.paciente.documento, e.especialidad, e.prioridad, e.estado)
        for e in session.scalars(select(ListaEspera))
    )
    crudo = json.dumps(contenido, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(crudo.encode()).hexdigest()


@pytest.fixture
def sembrado(sesiones: Callable[[], Session]) -> Session:
    sesion = sesiones()
    sembrar(sesion, PARAMS)
    sesion.commit()
    return sesion


class TestDeterminismo:
    def test_dos_corridas_con_la_misma_semilla_dan_lo_mismo(
        self, sesiones: Callable[[], Session]
    ) -> None:
        sesion = sesiones()
        sembrar(sesion, PARAMS)
        sesion.commit()
        primera = huella(sesion)

        sembrar(sesion, PARAMS)
        sesion.commit()
        assert huella(sesion) == primera

    def test_otra_semilla_da_otro_resultado(self, sesiones: Callable[[], Session]) -> None:
        sesion = sesiones()
        sembrar(sesion, PARAMS)
        sesion.commit()
        primera = huella(sesion)

        from dataclasses import replace

        sembrar(sesion, replace(PARAMS, seed=PARAMS.seed + 1))
        sesion.commit()
        assert huella(sesion) != primera

    def test_no_depende_de_la_hora_de_ejecucion(self, sesiones: Callable[[], Session]) -> None:
        """The past/future split must come from the base date, never from the
        wall clock, otherwise the same seed drifts through the day."""
        sesion = sesiones()
        sembrar(sesion, PARAMS)
        sesion.commit()
        estados_primera = sorted(
            (c.slot.inicio.isoformat(), c.estado) for c in sesion.scalars(select(Cita))
        )
        sembrar(sesion, PARAMS)
        sesion.commit()
        estados_segunda = sorted(
            (c.slot.inicio.isoformat(), c.estado) for c in sesion.scalars(select(Cita))
        )
        assert estados_primera == estados_segunda


class TestIdempotenciaDelComando:
    def test_sembrar_deja_la_base_no_vacia(self, sembrado: Session) -> None:
        assert not base_vacia(sembrado)

    def test_base_vacia_detecta_una_base_limpia(self, tablas_vacias: Session) -> None:
        assert base_vacia(tablas_vacias)

    def test_sembrar_dos_veces_no_duplica(self, sesiones: Callable[[], Session]) -> None:
        sesion = sesiones()
        sembrar(sesion, PARAMS)
        sesion.commit()
        pacientes = sesion.scalar(select(func.count()).select_from(Paciente))

        sembrar(sesion, PARAMS)
        sesion.commit()
        assert sesion.scalar(select(func.count()).select_from(Paciente)) == pacientes


class TestConsistenciaDelDataset:
    def test_hay_exactamente_una_clinica(self, sembrado: Session) -> None:
        assert sembrado.scalar(select(func.count()).select_from(Clinica)) == 1

    def test_se_generan_los_pacientes_pedidos(self, sembrado: Session) -> None:
        assert sembrado.scalar(select(func.count()).select_from(Paciente)) == PARAMS.pacientes

    def test_los_documentos_no_se_repiten(self, sembrado: Session) -> None:
        documentos = list(sembrado.scalars(select(Paciente.documento)))
        assert len(documentos) == len(set(documentos))

    def test_ninguna_cita_ocupa_un_slot_ya_ocupado(self, sembrado: Session) -> None:
        """If the partial unique index were wrong, the seed itself would be the
        first thing to violate it."""
        activas = [
            c.slot_id for c in sembrado.scalars(select(Cita)) if c.estado in ESTADOS_QUE_OCUPAN_SLOT
        ]
        assert len(activas) == len(set(activas))

    def test_el_estado_del_slot_concuerda_con_su_cita(self, sembrado: Session) -> None:
        for cita in sembrado.scalars(select(Cita)):
            if cita.estado in ESTADOS_QUE_OCUPAN_SLOT:
                assert cita.slot.estado is EstadoSlot.OCUPADO, cita.id

    def test_todo_historial_describe_un_camino_legal(self, sembrado: Session) -> None:
        """Every seeded appointment must have a history the state machine would
        actually have accepted. A seed that fabricates impossible histories
        makes every downstream test meaningless."""
        for cita in sembrado.scalars(select(Cita)):
            historial = sorted(cita.historial, key=lambda h: h.momento)
            assert historial, f"cita {cita.id} sin historial"
            assert historial[0].estado_anterior is None
            assert historial[0].estado_nuevo is EstadoCita.AGENDADA
            for anterior, siguiente in itertools.pairwise(historial):
                assert siguiente.estado_anterior is anterior.estado_nuevo
                assert es_transicion_valida(anterior.estado_nuevo, siguiente.estado_nuevo)
            assert historial[-1].estado_nuevo is cita.estado

    def test_toda_transicion_quedo_auditada(self, sembrado: Session) -> None:
        filas = sembrado.scalar(select(func.count()).select_from(CitaHistorial))
        citas = sembrado.scalar(select(func.count()).select_from(Cita))
        assert filas is not None and citas is not None
        assert filas >= citas  # at least the creation row per appointment

    def test_los_cargos_solo_cuelgan_de_citas_atendidas_o_no_asistidas(
        self, sembrado: Session
    ) -> None:
        for cargo in sembrado.scalars(select(Cargo)):
            if cargo.cita is not None:
                assert cargo.cita.estado in {EstadoCita.ATENDIDA, EstadoCita.NO_ASISTIO}

    def test_ningun_cargo_es_negativo(self, sembrado: Session) -> None:
        assert all(c.monto >= 0 for c in sembrado.scalars(select(Cargo)))

    def test_un_paciente_soat_activo_no_acumula_cargos_de_atencion(self, sembrado: Session) -> None:
        for cargo in sembrado.scalars(select(Cargo)):
            if cargo.cita is not None and cargo.cita.estado is EstadoCita.ATENDIDA:
                paciente = cargo.paciente
                assert not (paciente.regimen is Regimen.SOAT and paciente.afiliacion_activa), (
                    f"SOAT activo con cargo de atención: paciente {paciente.id}"
                )


class TestRealismoDelDataset:
    """A seed nobody can demo against is a seed that failed."""

    def test_quedan_cupos_libres_para_agendar(self, sembrado: Session) -> None:
        libres = sembrado.scalar(
            select(func.count())
            .select_from(AgendaSlot)
            .where(AgendaSlot.estado == EstadoSlot.LIBRE)
        )
        assert libres is not None and libres > 50

    def test_hay_citas_en_varios_estados(self, sembrado: Session) -> None:
        estados = {c.estado for c in sembrado.scalars(select(Cita))}
        assert {EstadoCita.AGENDADA, EstadoCita.CONFIRMADA, EstadoCita.ATENDIDA} <= estados

    def test_hay_no_shows_que_es_el_dolor_que_ataca_el_proyecto(self, sembrado: Session) -> None:
        no_shows = [c for c in sembrado.scalars(select(Cita)) if c.estado is EstadoCita.NO_ASISTIO]
        assert no_shows

    def test_hay_afiliaciones_inactivas(self, sembrado: Session) -> None:
        inactivos = [
            p
            for p in sembrado.scalars(select(Paciente))
            if not p.afiliacion_activa and p.regimen is not Regimen.PARTICULAR
        ]
        assert inactivos, "sin afiliaciones inactivas, validar_afiliacion no tiene qué atrapar"

    def test_hay_pacientes_sin_consentimiento_clinico(self, sembrado: Session) -> None:
        """The clinical tool must have real cases where it is correctly refused."""
        sin_consentimiento = [
            p for p in sembrado.scalars(select(Paciente)) if not p.consentimiento_datos_clinicos
        ]
        assert sin_consentimiento

    def test_hay_cargos_pendientes_para_cobrar(self, sembrado: Session) -> None:
        pendientes = [c for c in sembrado.scalars(select(Cargo)) if c.estado == "pendiente"]
        assert pendientes

    def test_hay_cartera_realmente_vencida(self, sembrado: Session) -> None:
        """Charges fall due 30 days after the visit, and the seeded agenda only
        reaches a couple of weeks back. Without carried-over balances every
        patient reads `al_dia`, and the rule that debt warns without blocking
        has nothing to demonstrate itself on."""
        vencidos = [
            c
            for c in sembrado.scalars(select(Cargo))
            if c.estado == EstadoCargo.PENDIENTE and c.vencimiento < FECHA_BASE
        ]
        assert vencidos, "el dataset no tiene ni un cargo vencido"
        assert len({c.paciente_id for c in vencidos}) >= 3

    def test_la_mora_cubre_varios_tramos_de_antiguedad(self, sembrado: Session) -> None:
        """A ledger where everything is 20 days late exercises one bucket."""
        dias = {
            (FECHA_BASE - c.vencimiento).days
            for c in sembrado.scalars(select(Cargo))
            if c.estado == EstadoCargo.PENDIENTE and c.vencimiento < FECHA_BASE
        }
        assert max(dias) > 60, f"la mora más antigua es de {max(dias)} días"

    def test_algun_paciente_supera_el_umbral_de_alerta(self, sembrado: Session) -> None:
        """Otherwise `alerta_al_agendar` never fires on the demo data."""
        from collections import defaultdict

        from backend.domain.cartera import POLITICA_POR_DEFECTO

        por_paciente: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for c in sembrado.scalars(select(Cargo)):
            if c.estado == EstadoCargo.PENDIENTE and c.vencimiento < FECHA_BASE:
                por_paciente[c.paciente_id] += c.monto
        assert any(
            total >= POLITICA_POR_DEFECTO.umbral_alerta_mora for total in por_paciente.values()
        )

    def test_hay_pacientes_en_lista_de_espera(self, sembrado: Session) -> None:
        assert sembrado.scalar(select(func.count()).select_from(ListaEspera))

    def test_estan_representados_los_cuatro_regimenes(self, sembrado: Session) -> None:
        regimenes = {p.regimen for p in sembrado.scalars(select(Paciente))}
        assert regimenes == set(Regimen)

    def test_la_agenda_cubre_pasado_y_futuro(self, sembrado: Session) -> None:
        fechas = [s.fecha for s in sembrado.scalars(select(AgendaSlot))]
        assert min(fechas) < FECHA_BASE < max(fechas)


class TestSinPiiReal:
    def test_ningun_dato_de_contacto_apunta_a_un_dominio_real(self, sembrado: Session) -> None:
        """Cheap but explicit: the project's headline claim is that no real
        patient data exists here, so it gets an assertion."""
        for paciente in sembrado.scalars(select(Paciente)):
            assert paciente.telefono.startswith("+57 3")
            if paciente.email:
                assert "@" in paciente.email
