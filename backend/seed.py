"""Deterministic synthetic data.

Two properties matter here and both are tested:

* **Deterministic.** Same seed and same base date produce a byte-identical
  dataset. Without that, "reproduce the bug I saw" is impossible and every demo
  is a different demo.
* **Synthetic.** Every name, document number, phone and email comes from Faker.
  There is no real patient data in this project and no code path that could
  introduce any.

Run with ``python -m backend.seed`` (see ``--help``).
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal

from faker import Faker
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import session_scope
from backend.domain.afiliacion import validar_afiliacion
from backend.domain.cartera import (
    calcular_cargo_por_atencion,
    calcular_cargo_por_no_show,
)
from backend.domain.tiempo import a_local, ahora_local, local, slots_del_dia
from backend.enums import (
    Especialidad,
    EstadoCargo,
    EstadoCita,
    EstadoListaEspera,
    EstadoSlot,
    PrioridadListaEspera,
    Regimen,
    TipoDocumento,
)
from backend.models import (
    AgendaSlot,
    Cargo,
    Cita,
    CitaHistorial,
    Clinica,
    ListaEspera,
    Paciente,
    Profesional,
)

USUARIO_SEED = "seed@clinica.local"

#: Clinic-local instant the seeded agenda pivots around. Slots before it are
#: history (attended / no-show / cancelled), slots after it are upcoming.
HORA_REFERENCIA = time(9, 0)

#: Distribution of affiliation regimes, roughly matching Colombian coverage.
DISTRIBUCION_REGIMEN: list[tuple[Regimen, int]] = [
    (Regimen.CONTRIBUTIVO, 48),
    (Regimen.SUBSIDIADO, 38),
    (Regimen.PARTICULAR, 10),
    (Regimen.SOAT, 4),
]

PROFESIONALES: list[tuple[str, Especialidad]] = [
    ("Dra. Marcela Ospina Rivera", Especialidad.ODONTOLOGIA_GENERAL),
    ("Dr. Andrés Felipe Cadena", Especialidad.ODONTOLOGIA_GENERAL),
    ("Dra. Laura Betancur Gómez", Especialidad.ORTODONCIA),
    ("Dr. Julián Restrepo Vélez", Especialidad.ENDODONCIA),
    ("Dra. Paula Andrea Quintero", Especialidad.PERIODONCIA),
    ("Dr. Santiago Mejía Arango", Especialidad.ODONTOPEDIATRIA),
]


@dataclass(frozen=True, slots=True)
class ParametrosSeed:
    seed: int
    pacientes: int
    dias_agenda: int
    fecha_base: date

    @property
    def dias_historia(self) -> int:
        """How far back the agenda (and therefore the history) reaches."""
        return max(7, self.dias_agenda // 2)


def _elegir_ponderado(rng: random.Random, opciones: list[tuple[Regimen, int]]) -> Regimen:
    total = sum(peso for _, peso in opciones)
    tirada = rng.randrange(total)
    acumulado = 0
    for valor, peso in opciones:
        acumulado += peso
        if tirada < acumulado:
            return valor
    return opciones[-1][0]  # pragma: no cover - unreachable, guards float drift


def base_vacia(session: Session) -> bool:
    return session.scalar(select(func.count()).select_from(Clinica)) == 0


def limpiar(session: Session) -> None:
    """Wipe every table. Ordered by dependency so foreign keys stay satisfied."""
    orden = (
        ListaEspera,
        Cargo,
        CitaHistorial,
        Cita,
        AgendaSlot,
        Paciente,
        Profesional,
        Clinica,
    )
    for modelo in orden:
        session.query(modelo).delete()
    session.flush()


def _crear_clinica(session: Session) -> Clinica:
    clinica = Clinica(
        nombre="Clínica Odontológica Sonrisa Viva",
        nit="901.455.782-3",
        especialidad="Odontología integral",
        direccion="Calle 93 #15-42, Consultorio 304",
        telefono="+57 601 743 2200",
        ciudad="Bogotá",
        zona_horaria="America/Bogota",
    )
    session.add(clinica)
    session.flush()
    return clinica


def _crear_profesionales(session: Session, clinica: Clinica) -> list[Profesional]:
    profesionales = [
        Profesional(
            clinica_id=clinica.id,
            nombre=nombre,
            registro=f"RM-{4100 + indice}",
            especialidad=especialidad,
            activo=True,
        )
        for indice, (nombre, especialidad) in enumerate(PROFESIONALES)
    ]
    session.add_all(profesionales)
    session.flush()
    return profesionales


def _crear_pacientes(
    session: Session, fake: Faker, rng: random.Random, params: ParametrosSeed
) -> list[Paciente]:
    pacientes: list[Paciente] = []
    documentos: set[str] = set()

    for _ in range(params.pacientes):
        documento = str(rng.randrange(10_000_000, 1_300_000_000))
        while documento in documentos:
            documento = str(rng.randrange(10_000_000, 1_300_000_000))
        documentos.add(documento)

        regimen = _elegir_ponderado(rng, DISTRIBUCION_REGIMEN)
        # ~12% of affiliated patients have lapsed. This is what makes
        # validar_afiliacion worth calling instead of assuming.
        activa = regimen is Regimen.PARTICULAR or rng.random() > 0.12

        nacimiento = fake.date_of_birth(minimum_age=3, maximum_age=88)
        edad = params.fecha_base.year - nacimiento.year
        tipo_doc = TipoDocumento.TI if edad < 18 else TipoDocumento.CC

        pacientes.append(
            Paciente(
                tipo_documento=tipo_doc,
                documento=documento,
                nombre=fake.name(),
                telefono=f"+57 3{rng.randrange(10, 25)}{rng.randrange(1000000, 9999999)}",
                email=fake.email(),
                fecha_nacimiento=nacimiento,
                regimen=regimen,
                afiliacion_activa=activa,
                eps=None if regimen is Regimen.PARTICULAR else fake.company(),
                nivel_cuota_moderadora=rng.choice([1, 1, 2, 2, 3]),
                # Consent is deliberately NOT universal: the clinical tool must
                # have real cases where it is correctly refused.
                consentimiento_datos_clinicos=rng.random() > 0.35,
            )
        )
    session.add_all(pacientes)
    session.flush()
    return pacientes


def _crear_agenda(
    session: Session, profesionales: list[Profesional], params: ParametrosSeed
) -> list[AgendaSlot]:
    slots: list[AgendaSlot] = []
    inicio = params.fecha_base - timedelta(days=params.dias_historia)
    for offset in range(params.dias_historia + params.dias_agenda):
        dia = inicio + timedelta(days=offset)
        rangos = slots_del_dia(dia)
        if not rangos:
            continue
        for profesional in profesionales:
            for comienzo, fin in rangos:
                slots.append(
                    AgendaSlot(
                        profesional_id=profesional.id,
                        fecha=a_local(comienzo).date(),
                        inicio=comienzo,
                        fin=fin,
                        estado=EstadoSlot.LIBRE,
                    )
                )
    session.add_all(slots)
    session.flush()
    return slots


def _estado_para_slot(rng: random.Random, es_pasado: bool, horas_restantes: float) -> EstadoCita:
    """Pick a plausible appointment state given where the slot sits in time."""
    if es_pasado:
        # ~22% no-show rate: close to the quantified problem this project targets.
        return rng.choices(
            [EstadoCita.ATENDIDA, EstadoCita.NO_ASISTIO, EstadoCita.CANCELADA],
            weights=[68, 22, 10],
        )[0]
    if horas_restantes <= 3:
        return rng.choices([EstadoCita.EN_ESPERA, EstadoCita.CONFIRMADA], weights=[40, 60])[0]
    if horas_restantes <= 48:
        return rng.choices([EstadoCita.CONFIRMADA, EstadoCita.AGENDADA], weights=[70, 30])[0]
    return rng.choices([EstadoCita.AGENDADA, EstadoCita.CONFIRMADA], weights=[75, 25])[0]


def _ruta_historial(estado_final: EstadoCita) -> list[EstadoCita]:
    """A legal path from `agendada` to the target state, so every seeded
    appointment has a history that the state machine would actually accept."""
    rutas: dict[EstadoCita, list[EstadoCita]] = {
        EstadoCita.AGENDADA: [EstadoCita.AGENDADA],
        EstadoCita.CONFIRMADA: [EstadoCita.AGENDADA, EstadoCita.CONFIRMADA],
        EstadoCita.EN_ESPERA: [EstadoCita.AGENDADA, EstadoCita.CONFIRMADA, EstadoCita.EN_ESPERA],
        EstadoCita.ATENDIDA: [
            EstadoCita.AGENDADA,
            EstadoCita.CONFIRMADA,
            EstadoCita.EN_ESPERA,
            EstadoCita.ATENDIDA,
        ],
        EstadoCita.CANCELADA: [EstadoCita.AGENDADA, EstadoCita.CANCELADA],
        EstadoCita.NO_ASISTIO: [
            EstadoCita.AGENDADA,
            EstadoCita.CONFIRMADA,
            EstadoCita.NO_ASISTIO,
        ],
        EstadoCita.REPROGRAMADA: [EstadoCita.AGENDADA, EstadoCita.REPROGRAMADA],
    }
    return rutas[estado_final]


def _crear_citas(
    session: Session,
    fake: Faker,
    rng: random.Random,
    pacientes: list[Paciente],
    slots: list[AgendaSlot],
    params: ParametrosSeed,
) -> tuple[list[Cita], list[Cargo]]:
    # Fixed reference *instant*, not "now on the base date": deriving it from
    # the wall clock would make two runs on the same day produce different
    # past/future splits, and determinism is a tested property here.
    referencia = local(params.fecha_base, HORA_REFERENCIA)

    citas: list[Cita] = []
    cargos: list[Cargo] = []
    por_profesional: dict[int, list[AgendaSlot]] = {}
    for slot in slots:
        por_profesional.setdefault(slot.profesional_id, []).append(slot)

    for slot_lista in por_profesional.values():
        # ~45% occupancy leaves a realistic agenda: full enough to be
        # interesting, free enough that consultar_disponibilidad returns rows.
        elegidos = rng.sample(slot_lista, k=int(len(slot_lista) * 0.45))
        for slot in elegidos:
            paciente = rng.choice(pacientes)
            inicio_local = a_local(slot.inicio)
            es_pasado = inicio_local < referencia
            horas = (inicio_local - referencia).total_seconds() / 3600
            estado = _estado_para_slot(rng, es_pasado, horas)

            libera = estado in {EstadoCita.CANCELADA, EstadoCita.REPROGRAMADA}
            slot.estado = EstadoSlot.LIBRE if libera else EstadoSlot.OCUPADO

            cita = Cita(
                paciente_id=paciente.id,
                profesional_id=slot.profesional_id,
                slot_id=slot.id,
                estado=estado,
                creada_por=USUARIO_SEED,
                motivo_cancelacion=(
                    fake.sentence(nb_words=6) if estado is EstadoCita.CANCELADA else None
                ),
            )
            session.add(cita)
            session.flush()

            ruta = _ruta_historial(estado)
            momento = slot.inicio - timedelta(days=len(ruta) + 1)
            anterior: EstadoCita | None = None
            for paso in ruta:
                session.add(
                    CitaHistorial(
                        cita_id=cita.id,
                        estado_anterior=anterior,
                        estado_nuevo=paso,
                        usuario=USUARIO_SEED,
                        motivo=cita.motivo_cancelacion if paso is EstadoCita.CANCELADA else None,
                        momento=momento,
                    )
                )
                anterior = paso
                momento += timedelta(hours=6)

            afiliacion = validar_afiliacion(
                paciente.regimen,
                paciente.afiliacion_activa,
                nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
            )
            calculado = None
            if estado is EstadoCita.ATENDIDA:
                calculado = calcular_cargo_por_atencion(
                    afiliacion,
                    str(slot.profesional.especialidad),
                    nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
                )
            elif estado is EstadoCita.NO_ASISTIO:
                calculado = calcular_cargo_por_no_show(estaba_confirmada=True)

            if calculado is not None:
                vencimiento = inicio_local.date() + timedelta(days=30)
                # Around a third stay unpaid, which is what gives the cartera
                # tools something real to report.
                pagado = rng.random() > 0.35
                cargo = Cargo(
                    paciente_id=paciente.id,
                    cita_id=cita.id,
                    concepto=calculado.concepto,
                    monto=Decimal(calculado.monto),
                    descripcion=calculado.descripcion,
                    estado=EstadoCargo.PAGADO if pagado else EstadoCargo.PENDIENTE,
                    vencimiento=vencimiento,
                    pagado_en=slot.inicio + timedelta(days=2) if pagado else None,
                )
                session.add(cargo)
                cargos.append(cargo)

            citas.append(cita)

    session.flush()
    return citas, cargos


#: How far back the carried-over ledger reaches. The agenda window is a few
#: weeks, but a clinic's balances are older than whatever window you happen to
#: be looking at, and charges fall due 30 days after the visit. Without this the
#: seeded cartera is always `al_dia`, and the rule that debt warns without
#: blocking has no data to demonstrate itself on.
DIAS_CARTERA_HISTORICA = 210

#: Ageing profile of that carried-over debt: mostly recent, with a tail. Values
#: are (days overdue lower bound, upper bound, weight).
PERFIL_ANTIGUEDAD: list[tuple[int, int, int]] = [
    (1, 30, 45),
    (31, 60, 25),
    (61, 90, 15),
    (91, DIAS_CARTERA_HISTORICA, 15),
]


def _crear_cartera_historica(
    session: Session, rng: random.Random, pacientes: list[Paciente], params: ParametrosSeed
) -> list[Cargo]:
    """Balances carried over from before the seeded agenda window.

    These have no appointment attached, which is exactly right: they predate the
    slice of agenda this dataset holds. Modelling them is what gives the
    collections tools, and the ageing buckets, something real to report.
    """
    cargos: list[Cargo] = []
    for paciente in rng.sample(pacientes, k=max(4, len(pacientes) // 4)):
        afiliacion = validar_afiliacion(
            paciente.regimen,
            paciente.afiliacion_activa,
            nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
        )
        for _ in range(rng.choice([1, 1, 2, 3])):
            desde, hasta, _peso = rng.choices(
                PERFIL_ANTIGUEDAD, weights=[p for *_, p in PERFIL_ANTIGUEDAD]
            )[0]
            dias_vencido = rng.randint(desde, hasta)
            calculado = calcular_cargo_por_atencion(
                afiliacion,
                rng.choice([str(e) for e in Especialidad]),
                nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
            )
            if calculado is None:  # SOAT covers the service, nothing to collect
                continue
            # A third of the aged debt was eventually settled, so the ledger is
            # not uniformly delinquent.
            pagado = rng.random() < 0.33
            cargos.append(
                Cargo(
                    paciente_id=paciente.id,
                    cita_id=None,
                    concepto=calculado.concepto,
                    monto=Decimal(calculado.monto),
                    descripcion=f"{calculado.descripcion} (saldo anterior)",
                    estado=EstadoCargo.PAGADO if pagado else EstadoCargo.PENDIENTE,
                    vencimiento=params.fecha_base - timedelta(days=dias_vencido),
                    pagado_en=None,
                )
            )
    session.add_all(cargos)
    session.flush()
    return cargos


def _crear_lista_espera(
    session: Session, fake: Faker, rng: random.Random, pacientes: list[Paciente]
) -> list[ListaEspera]:
    especialidades = list(Especialidad)
    entradas: list[ListaEspera] = []
    ocupados: set[tuple[int, Especialidad]] = set()

    for paciente in rng.sample(pacientes, k=max(6, len(pacientes) // 6)):
        especialidad = rng.choice(especialidades)
        if (paciente.id, especialidad) in ocupados:
            continue
        ocupados.add((paciente.id, especialidad))
        entradas.append(
            ListaEspera(
                paciente_id=paciente.id,
                especialidad=especialidad,
                prioridad=rng.choices(
                    [PrioridadListaEspera.ANTIGUEDAD, PrioridadListaEspera.URGENCIA],
                    weights=[80, 20],
                )[0],
                estado=EstadoListaEspera.ACTIVA,
                notas=fake.sentence(nb_words=8),
            )
        )
    session.add_all(entradas)
    session.flush()
    return entradas


def sembrar(session: Session, params: ParametrosSeed) -> dict[str, int]:
    """Populate an empty database and return a count per table."""
    fake = Faker("es_CO")
    Faker.seed(params.seed)
    # Reproducible mock data. Nothing generated here is a secret, so a
    # non-cryptographic PRNG is the correct choice, not a shortcut.
    rng = random.Random(params.seed)  # nosec B311

    limpiar(session)
    clinica = _crear_clinica(session)
    profesionales = _crear_profesionales(session, clinica)
    pacientes = _crear_pacientes(session, fake, rng, params)
    slots = _crear_agenda(session, profesionales, params)
    citas, cargos = _crear_citas(session, fake, rng, pacientes, slots, params)
    historicos = _crear_cartera_historica(session, rng, pacientes, params)
    lista = _crear_lista_espera(session, fake, rng, pacientes)

    return {
        "clinica": 1,
        "profesional": len(profesionales),
        "paciente": len(pacientes),
        "agenda_slot": len(slots),
        "cita": len(citas),
        "cargo": len(cargos) + len(historicos),
        "lista_espera": len(lista),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Load deterministic synthetic data.")
    parser.add_argument("--seed", type=int, default=settings.seed_value)
    parser.add_argument("--pacientes", type=int, default=settings.seed_pacientes)
    parser.add_argument("--dias-agenda", type=int, default=settings.seed_dias_agenda)
    parser.add_argument(
        "--fecha-base",
        type=date.fromisoformat,
        default=None,
        help="Anchor date for the agenda (default: today, clinic local time).",
    )
    parser.add_argument(
        "--if-empty",
        action="store_true",
        help="Do nothing when the database already has data (used by docker compose).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    params = ParametrosSeed(
        seed=args.seed,
        pacientes=args.pacientes,
        dias_agenda=args.dias_agenda,
        fecha_base=args.fecha_base or ahora_local().date(),
    )

    with session_scope() as session:
        if args.if_empty and not base_vacia(session):
            print("Seed omitido: la base ya tiene datos.")
            return 0
        conteos = sembrar(session, params)

    print(f"Seed listo (semilla={params.seed}, fecha base={params.fecha_base}):")
    for tabla, cantidad in conteos.items():
        print(f"  {tabla:<14} {cantidad:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
