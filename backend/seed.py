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
from backend.domain.afiliacion import validate_afiliacion
from backend.domain.cartera import (
    charge_for_no_show,
    charge_for_visit,
)
from backend.domain.time import local, now_at_clinic, slots_for_day, to_clinic_time
from backend.enums import (
    AppointmentState,
    ChargeState,
    DocumentType,
    Regimen,
    SlotState,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)
from backend.models import (
    AgendaSlot,
    Appointment,
    AppointmentHistory,
    Charge,
    Clinic,
    Patient,
    Professional,
    WaitingList,
)

SEED_USER = "seed@clinica.local"

#: Clinic-local instant the seeded agenda pivots around. Slots before it are
#: history (attended / no-show / cancelled), slots after it are upcoming.
REFERENCE_HOUR = time(9, 0)

#: Distribution of affiliation regimes, roughly matching Colombian coverage.
REGIMEN_MIX: list[tuple[Regimen, int]] = [
    (Regimen.CONTRIBUTIVO, 48),
    (Regimen.SUBSIDIADO, 38),
    (Regimen.PARTICULAR, 10),
    (Regimen.SOAT, 4),
]

PROFESSIONALS: list[tuple[str, Specialty]] = [
    ("Dra. Marcela Ospina Rivera", Specialty.GENERAL_DENTISTRY),
    ("Dr. Andrés Felipe Cadena", Specialty.GENERAL_DENTISTRY),
    ("Dra. Laura Betancur Gómez", Specialty.ORTHODONTICS),
    ("Dr. Julián Restrepo Vélez", Specialty.ENDODONTICS),
    ("Dra. Paula Andrea Quintero", Specialty.PERIODONTICS),
    ("Dr. Santiago Mejía Arango", Specialty.PEDIATRIC_DENTISTRY),
]


@dataclass(frozen=True, slots=True)
class SeedParams:
    seed: int
    patients: int
    dias_agenda: int
    fecha_base: date

    @property
    def history_days(self) -> int:
        """How far back the agenda (and therefore the history) reaches."""
        return max(7, self.dias_agenda // 2)


def _weighted_choice(rng: random.Random, opciones: list[tuple[Regimen, int]]) -> Regimen:
    total = sum(peso for _, peso in opciones)
    tirada = rng.randrange(total)
    acumulado = 0
    for value, peso in opciones:
        acumulado += peso
        if tirada < acumulado:
            return value
    return opciones[-1][0]  # pragma: no cover - unreachable, guards float drift


def database_is_empty(session: Session) -> bool:
    return session.scalar(select(func.count()).select_from(Clinic)) == 0


def wipe(session: Session) -> None:
    """Wipe every table. Ordered by dependency so foreign keys stay satisfied."""
    order = (
        WaitingList,
        Charge,
        AppointmentHistory,
        Appointment,
        AgendaSlot,
        Patient,
        Professional,
        Clinic,
    )
    for model in order:
        session.query(model).delete()
    session.flush()


def _create_clinic(session: Session) -> Clinic:
    clinica = Clinic(
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


def _create_professionals(session: Session, clinica: Clinic) -> list[Professional]:
    profesionales = [
        Professional(
            clinica_id=clinica.id,
            nombre=nombre,
            registro=f"RM-{4100 + indice}",
            especialidad=especialidad,
            activo=True,
        )
        for indice, (nombre, especialidad) in enumerate(PROFESSIONALS)
    ]
    session.add_all(profesionales)
    session.flush()
    return profesionales


def _create_patients(
    session: Session, fake: Faker, rng: random.Random, params: SeedParams
) -> list[Patient]:
    patients: list[Patient] = []
    documentos: set[str] = set()

    for _ in range(params.patients):
        documento = str(rng.randrange(10_000_000, 1_300_000_000))
        while documento in documentos:
            documento = str(rng.randrange(10_000_000, 1_300_000_000))
        documentos.add(documento)

        regimen = _weighted_choice(rng, REGIMEN_MIX)
        # ~12% of affiliated patients have lapsed. This is what makes
        # validate_afiliacion worth calling instead of assuming.
        activa = regimen is Regimen.PARTICULAR or rng.random() > 0.12

        nacimiento = fake.date_of_birth(minimum_age=3, maximum_age=88)
        edad = params.fecha_base.year - nacimiento.year
        tipo_doc = DocumentType.TI if edad < 18 else DocumentType.CC

        patients.append(
            Patient(
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
    session.add_all(patients)
    session.flush()
    return patients


def _create_agenda(
    session: Session, profesionales: list[Professional], params: SeedParams
) -> list[AgendaSlot]:
    slots: list[AgendaSlot] = []
    inicio = params.fecha_base - timedelta(days=params.history_days)
    for offset in range(params.history_days + params.dias_agenda):
        dia = inicio + timedelta(days=offset)
        rangos = slots_for_day(dia)
        if not rangos:
            continue
        for profesional in profesionales:
            for comienzo, fin in rangos:
                slots.append(
                    AgendaSlot(
                        profesional_id=profesional.id,
                        fecha=to_clinic_time(comienzo).date(),
                        inicio=comienzo,
                        fin=fin,
                        estado=SlotState.FREE,
                    )
                )
    session.add_all(slots)
    session.flush()
    return slots


def _state_for_slot(
    rng: random.Random, es_pasado: bool, horas_restantes: float
) -> AppointmentState:
    """Pick a plausible appointment state given where the slot sits in time."""
    if es_pasado:
        # ~22% no-show rate: close to the quantified problem this project targets.
        return rng.choices(
            [AppointmentState.ATTENDED, AppointmentState.NO_SHOW, AppointmentState.CANCELLED],
            weights=[68, 22, 10],
        )[0]
    if horas_restantes <= 3:
        return rng.choices(
            [AppointmentState.WAITING, AppointmentState.CONFIRMED], weights=[40, 60]
        )[0]
    if horas_restantes <= 48:
        return rng.choices(
            [AppointmentState.CONFIRMED, AppointmentState.SCHEDULED], weights=[70, 30]
        )[0]
    return rng.choices([AppointmentState.SCHEDULED, AppointmentState.CONFIRMED], weights=[75, 25])[
        0
    ]


def _history_path(estado_final: AppointmentState) -> list[AppointmentState]:
    """A legal path from `scheduled` to the target state, so every seeded
    appointment has a history that the state machine would actually accept."""
    rutas: dict[AppointmentState, list[AppointmentState]] = {
        AppointmentState.SCHEDULED: [AppointmentState.SCHEDULED],
        AppointmentState.CONFIRMED: [AppointmentState.SCHEDULED, AppointmentState.CONFIRMED],
        AppointmentState.WAITING: [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.WAITING,
        ],
        AppointmentState.ATTENDED: [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.WAITING,
            AppointmentState.ATTENDED,
        ],
        AppointmentState.CANCELLED: [AppointmentState.SCHEDULED, AppointmentState.CANCELLED],
        AppointmentState.NO_SHOW: [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.NO_SHOW,
        ],
        AppointmentState.RESCHEDULED: [AppointmentState.SCHEDULED, AppointmentState.RESCHEDULED],
    }
    return rutas[estado_final]


def _create_appointments(
    session: Session,
    fake: Faker,
    rng: random.Random,
    patients: list[Patient],
    slots: list[AgendaSlot],
    params: SeedParams,
) -> tuple[list[Appointment], list[Charge]]:
    # Fixed reference *instant*, not "now on the base date": deriving it from
    # the wall clock would make two runs on the same day produce different
    # past/future splits, and determinism is a tested property here.
    reference = local(params.fecha_base, REFERENCE_HOUR)

    citas: list[Appointment] = []
    cargos: list[Charge] = []
    por_profesional: dict[int, list[AgendaSlot]] = {}
    for slot in slots:
        por_profesional.setdefault(slot.profesional_id, []).append(slot)

    for slot_lista in por_profesional.values():
        # ~45% occupancy leaves a realistic agenda: full enough to be
        # interesting, free enough that check_availability returns rows.
        elegidos = rng.sample(slot_lista, k=int(len(slot_lista) * 0.45))
        for slot in elegidos:
            paciente = rng.choice(patients)
            inicio_local = to_clinic_time(slot.inicio)
            es_pasado = inicio_local < reference
            horas = (inicio_local - reference).total_seconds() / 3600
            estado = _state_for_slot(rng, es_pasado, horas)

            libera = estado in {AppointmentState.CANCELLED, AppointmentState.RESCHEDULED}
            slot.estado = SlotState.FREE if libera else SlotState.BUSY

            cita = Appointment(
                paciente_id=paciente.id,
                profesional_id=slot.profesional_id,
                slot_id=slot.id,
                estado=estado,
                creada_por=SEED_USER,
                motivo_cancelacion=(
                    fake.sentence(nb_words=6) if estado is AppointmentState.CANCELLED else None
                ),
            )
            session.add(cita)
            session.flush()

            ruta = _history_path(estado)
            momento = slot.inicio - timedelta(days=len(ruta) + 1)
            previous: AppointmentState | None = None
            for step in ruta:
                session.add(
                    AppointmentHistory(
                        cita_id=cita.id,
                        estado_anterior=previous,
                        estado_nuevo=step,
                        usuario=SEED_USER,
                        motivo=cita.motivo_cancelacion
                        if step is AppointmentState.CANCELLED
                        else None,
                        momento=momento,
                    )
                )
                previous = step
                momento += timedelta(hours=6)

            afiliacion = validate_afiliacion(
                paciente.regimen,
                paciente.afiliacion_activa,
                nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
            )
            calculated = None
            if estado is AppointmentState.ATTENDED:
                calculated = charge_for_visit(
                    afiliacion,
                    str(slot.profesional.especialidad),
                    nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
                )
            elif estado is AppointmentState.NO_SHOW:
                calculated = charge_for_no_show(estaba_confirmada=True)

            if calculated is not None:
                vencimiento = inicio_local.date() + timedelta(days=30)
                # Around a third stay unpaid, which is what gives the cartera
                # tools something real to report.
                pagado = rng.random() > 0.35
                cargo = Charge(
                    paciente_id=paciente.id,
                    cita_id=cita.id,
                    concepto=calculated.concepto,
                    monto=Decimal(calculated.monto),
                    descripcion=calculated.descripcion,
                    estado=ChargeState.PAID if pagado else ChargeState.PENDING,
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
CARRIED_OVER_CARTERA_DAYS = 210

#: Ageing profile of that carried-over debt: mostly recent, with a tail. Values
#: are (days overdue lower bound, upper bound, weight).
AGEING_PROFILE: list[tuple[int, int, int]] = [
    (1, 30, 45),
    (31, 60, 25),
    (61, 90, 15),
    (91, CARRIED_OVER_CARTERA_DAYS, 15),
]


def _create_historic_cartera(
    session: Session, rng: random.Random, patients: list[Patient], params: SeedParams
) -> list[Charge]:
    """Balances carried over from before the seeded agenda window.

    These have no appointment attached, which is exactly right: they predate the
    slice of agenda this dataset holds. Modelling them is what gives the
    collections tools, and the ageing buckets, something real to report.
    """
    cargos: list[Charge] = []
    for paciente in rng.sample(patients, k=max(4, len(patients) // 4)):
        afiliacion = validate_afiliacion(
            paciente.regimen,
            paciente.afiliacion_activa,
            nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
        )
        for _ in range(rng.choice([1, 1, 2, 3])):
            desde, hasta, _peso = rng.choices(
                AGEING_PROFILE, weights=[p for *_, p in AGEING_PROFILE]
            )[0]
            dias_vencido = rng.randint(desde, hasta)
            calculated = charge_for_visit(
                afiliacion,
                rng.choice([str(e) for e in Specialty]),
                nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
            )
            if calculated is None:  # SOAT covers the service, nothing to collect
                continue
            # A third of the aged debt was eventually settled, so the ledger is
            # not uniformly delinquent.
            pagado = rng.random() < 0.33
            cargos.append(
                Charge(
                    paciente_id=paciente.id,
                    cita_id=None,
                    concepto=calculated.concepto,
                    monto=Decimal(calculated.monto),
                    descripcion=f"{calculated.descripcion} (saldo anterior)",
                    estado=ChargeState.PAID if pagado else ChargeState.PENDING,
                    vencimiento=params.fecha_base - timedelta(days=dias_vencido),
                    pagado_en=None,
                )
            )
    session.add_all(cargos)
    session.flush()
    return cargos


def _create_waiting_list(
    session: Session, fake: Faker, rng: random.Random, patients: list[Patient]
) -> list[WaitingList]:
    especialidades = list(Specialty)
    entries: list[WaitingList] = []
    ocupados: set[tuple[int, Specialty]] = set()

    for paciente in rng.sample(patients, k=max(6, len(patients) // 6)):
        especialidad = rng.choice(especialidades)
        if (paciente.id, especialidad) in ocupados:
            continue
        ocupados.add((paciente.id, especialidad))
        entries.append(
            WaitingList(
                paciente_id=paciente.id,
                especialidad=especialidad,
                prioridad=rng.choices(
                    [WaitingListPriority.SENIORITY, WaitingListPriority.URGENT],
                    weights=[80, 20],
                )[0],
                estado=WaitingListState.ACTIVE,
                notas=fake.sentence(nb_words=8),
            )
        )
    session.add_all(entries)
    session.flush()
    return entries


def seed_database(session: Session, params: SeedParams) -> dict[str, int]:
    """Populate an empty database and return a count per table."""
    fake = Faker("es_CO")
    Faker.seed(params.seed)
    # Reproducible mock data. Nothing generated here is a secret, so a
    # non-cryptographic PRNG is the correct choice, not a shortcut.
    rng = random.Random(params.seed)  # nosec B311

    wipe(session)
    clinica = _create_clinic(session)
    profesionales = _create_professionals(session, clinica)
    patients = _create_patients(session, fake, rng, params)
    slots = _create_agenda(session, profesionales, params)
    citas, cargos = _create_appointments(session, fake, rng, patients, slots, params)
    historicos = _create_historic_cartera(session, rng, patients, params)
    lista = _create_waiting_list(session, fake, rng, patients)

    return {
        "clinica": 1,
        "profesional": len(profesionales),
        "paciente": len(patients),
        "agenda_slot": len(slots),
        "cita": len(citas),
        "cargo": len(cargos) + len(historicos),
        "lista_espera": len(lista),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Load deterministic synthetic data.")
    parser.add_argument("--seed", type=int, default=settings.seed_value)
    parser.add_argument("--patients", type=int, default=settings.seed_pacientes)
    parser.add_argument("--agenda-days", type=int, default=settings.seed_dias_agenda)
    parser.add_argument(
        "--base-date",
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
    params = SeedParams(
        seed=args.seed,
        patients=args.patients,
        dias_agenda=args.agenda_days,
        fecha_base=args.base_date or now_at_clinic().date(),
    )

    with session_scope() as session:
        if args.if_empty and not database_is_empty(session):
            print("Seed skipped: the database already holds data.")
            return 0
        counts = seed_database(session, params)

    print(f"Seed done (seed={params.seed}, base date={params.fecha_base}):")
    for table, count in counts.items():
        print(f"  {table:<14} {count:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
