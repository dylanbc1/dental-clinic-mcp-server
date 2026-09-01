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
from backend.domain.affiliation import validate_affiliation
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
    agenda_days: int
    base_date: date

    @property
    def history_days(self) -> int:
        """How far back the agenda (and therefore the history) reaches."""
        return max(7, self.agenda_days // 2)


def _weighted_choice(rng: random.Random, options: list[tuple[Regimen, int]]) -> Regimen:
    total = sum(peso for _, peso in options)
    roll = rng.randrange(total)
    accumulated = 0
    for value, peso in options:
        accumulated += peso
        if roll < accumulated:
            return value
    return options[-1][0]  # pragma: no cover - unreachable, guards float drift


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
    clinic = Clinic(
        name="Clínica Odontológica Sonrisa Viva",
        nit="901.455.782-3",
        specialty="Odontología integral",
        address="Calle 93 #15-42, Consultorio 304",
        phone="+57 601 743 2200",
        city="Bogotá",
        timezone_name="America/Bogota",
    )
    session.add(clinic)
    session.flush()
    return clinic


def _create_professionals(session: Session, clinic: Clinic) -> list[Professional]:
    professionals = [
        Professional(
            clinic_id=clinic.id,
            name=name,
            license_number=f"RM-{4100 + index}",
            specialty=specialty,
            active=True,
        )
        for index, (name, specialty) in enumerate(PROFESSIONALS)
    ]
    session.add_all(professionals)
    session.flush()
    return professionals


def _create_patients(
    session: Session, fake: Faker, rng: random.Random, params: SeedParams
) -> list[Patient]:
    patients: list[Patient] = []
    documents: set[str] = set()

    for _ in range(params.patients):
        document_number = str(rng.randrange(10_000_000, 1_300_000_000))
        while document_number in documents:
            document_number = str(rng.randrange(10_000_000, 1_300_000_000))
        documents.add(document_number)

        regimen = _weighted_choice(rng, REGIMEN_MIX)
        # ~12% of affiliated patients have lapsed. This is what makes
        # validate_affiliation worth calling instead of assuming.
        active = regimen is Regimen.PARTICULAR or rng.random() > 0.12

        birth = fake.date_of_birth(minimum_age=3, maximum_age=88)
        age = params.base_date.year - birth.year
        doc_type = DocumentType.TI if age < 18 else DocumentType.CC

        patients.append(
            Patient(
                document_type=doc_type,
                document_number=document_number,
                name=fake.name(),
                phone=f"+57 3{rng.randrange(10, 25)}{rng.randrange(1000000, 9999999)}",
                email=fake.email(),
                birth_date=birth,
                regimen=regimen,
                affiliation_active=active,
                eps=None if regimen is Regimen.PARTICULAR else fake.company(),
                cuota_moderadora_level=rng.choice([1, 1, 2, 2, 3]),
                # Consent is deliberately NOT universal: the clinical tool must
                # have real cases where it is correctly refused.
                clinical_data_consent=rng.random() > 0.35,
            )
        )
    session.add_all(patients)
    session.flush()
    return patients


def _create_agenda(
    session: Session, professionals: list[Professional], params: SeedParams
) -> list[AgendaSlot]:
    slots: list[AgendaSlot] = []
    start = params.base_date - timedelta(days=params.history_days)
    for offset in range(params.history_days + params.agenda_days):
        dia = start + timedelta(days=offset)
        ranges = slots_for_day(dia)
        if not ranges:
            continue
        for professional in professionals:
            for start, end in ranges:
                slots.append(
                    AgendaSlot(
                        professional_id=professional.id,
                        day=to_clinic_time(start).date(),
                        start=start,
                        end=end,
                        status=SlotState.FREE,
                    )
                )
    session.add_all(slots)
    session.flush()
    return slots


def _state_for_slot(rng: random.Random, is_past: bool, hours_left: float) -> AppointmentState:
    """Pick a plausible appointment state given where the slot sits in time."""
    if is_past:
        # ~22% no-show rate: close to the quantified problem this project targets.
        return rng.choices(
            [AppointmentState.ATTENDED, AppointmentState.NO_SHOW, AppointmentState.CANCELLED],
            weights=[68, 22, 10],
        )[0]
    if hours_left <= 3:
        return rng.choices(
            [AppointmentState.WAITING, AppointmentState.CONFIRMED], weights=[40, 60]
        )[0]
    if hours_left <= 48:
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
    reference = local(params.base_date, REFERENCE_HOUR)

    appointments: list[Appointment] = []
    charges: list[Charge] = []
    by_professional: dict[int, list[AgendaSlot]] = {}
    for slot in slots:
        by_professional.setdefault(slot.professional_id, []).append(slot)

    for professional_slots in by_professional.values():
        # ~45% occupancy leaves a realistic agenda: full enough to be
        # interesting, free enough that check_availability returns rows.
        chosen = rng.sample(professional_slots, k=int(len(professional_slots) * 0.45))
        for slot in chosen:
            patient = rng.choice(patients)
            start_local = to_clinic_time(slot.start)
            is_past = start_local < reference
            horas = (start_local - reference).total_seconds() / 3600
            status = _state_for_slot(rng, is_past, horas)

            releases = status in {AppointmentState.CANCELLED, AppointmentState.RESCHEDULED}
            slot.status = SlotState.FREE if releases else SlotState.BUSY

            appointment = Appointment(
                patient_id=patient.id,
                professional_id=slot.professional_id,
                slot_id=slot.id,
                status=status,
                created_by=SEED_USER,
                cancellation_reason=(
                    fake.sentence(nb_words=6) if status is AppointmentState.CANCELLED else None
                ),
            )
            session.add(appointment)
            session.flush()

            path = _history_path(status)
            occurred_at = slot.start - timedelta(days=len(path) + 1)
            previous: AppointmentState | None = None
            for step in path:
                session.add(
                    AppointmentHistory(
                        appointment_id=appointment.id,
                        previous_status=previous,
                        new_status=step,
                        user=SEED_USER,
                        reason=appointment.cancellation_reason
                        if step is AppointmentState.CANCELLED
                        else None,
                        occurred_at=occurred_at,
                    )
                )
                previous = step
                occurred_at += timedelta(hours=6)

            affiliation = validate_affiliation(
                patient.regimen,
                patient.affiliation_active,
                cuota_moderadora_level=patient.cuota_moderadora_level,
            )
            calculated = None
            if status is AppointmentState.ATTENDED:
                calculated = charge_for_visit(
                    affiliation,
                    str(slot.professional.specialty),
                    cuota_moderadora_level=patient.cuota_moderadora_level,
                )
            elif status is AppointmentState.NO_SHOW:
                calculated = charge_for_no_show(was_confirmed=True)

            if calculated is not None:
                due_date = start_local.date() + timedelta(days=30)
                # Around a third stay unpaid, which is what gives the cartera
                # tools something real to report.
                paid = rng.random() > 0.35
                charge = Charge(
                    patient_id=patient.id,
                    appointment_id=appointment.id,
                    concept=calculated.concept,
                    amount=Decimal(calculated.amount),
                    description=calculated.description,
                    status=ChargeState.PAID if paid else ChargeState.PENDING,
                    due_date=due_date,
                    paid_at=slot.start + timedelta(days=2) if paid else None,
                )
                session.add(charge)
                charges.append(charge)

            appointments.append(appointment)

    session.flush()
    return appointments, charges


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
    charges: list[Charge] = []
    for patient in rng.sample(patients, k=max(4, len(patients) // 4)):
        affiliation = validate_affiliation(
            patient.regimen,
            patient.affiliation_active,
            cuota_moderadora_level=patient.cuota_moderadora_level,
        )
        for _ in range(rng.choice([1, 1, 2, 3])):
            since, until, _peso = rng.choices(
                AGEING_PROFILE, weights=[p for *_, p in AGEING_PROFILE]
            )[0]
            days_overdue = rng.randint(since, until)
            calculated = charge_for_visit(
                affiliation,
                rng.choice([str(e) for e in Specialty]),
                cuota_moderadora_level=patient.cuota_moderadora_level,
            )
            if calculated is None:  # SOAT covers the service, nothing to collect
                continue
            # A third of the aged debt was eventually settled, so the ledger is
            # not uniformly delinquent.
            paid = rng.random() < 0.33
            charges.append(
                Charge(
                    patient_id=patient.id,
                    appointment_id=None,
                    concept=calculated.concept,
                    amount=Decimal(calculated.amount),
                    description=f"{calculated.description} (saldo anterior)",
                    status=ChargeState.PAID if paid else ChargeState.PENDING,
                    due_date=params.base_date - timedelta(days=days_overdue),
                    paid_at=None,
                )
            )
    session.add_all(charges)
    session.flush()
    return charges


def _create_waiting_list(
    session: Session, fake: Faker, rng: random.Random, patients: list[Patient]
) -> list[WaitingList]:
    specialties = list(Specialty)
    entries: list[WaitingList] = []
    taken: set[tuple[int, Specialty]] = set()

    for patient in rng.sample(patients, k=max(6, len(patients) // 6)):
        specialty = rng.choice(specialties)
        if (patient.id, specialty) in taken:
            continue
        taken.add((patient.id, specialty))
        entries.append(
            WaitingList(
                patient_id=patient.id,
                specialty=specialty,
                priority=rng.choices(
                    [WaitingListPriority.SENIORITY, WaitingListPriority.URGENT],
                    weights=[80, 20],
                )[0],
                status=WaitingListState.ACTIVE,
                notes=fake.sentence(nb_words=8),
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
    clinic = _create_clinic(session)
    professionals = _create_professionals(session, clinic)
    patients = _create_patients(session, fake, rng, params)
    slots = _create_agenda(session, professionals, params)
    appointments, charges = _create_appointments(session, fake, rng, patients, slots, params)
    historic = _create_historic_cartera(session, rng, patients, params)
    waiting = _create_waiting_list(session, fake, rng, patients)

    return {
        "clinic": 1,
        "professional": len(professionals),
        "patient": len(patients),
        "agenda_slot": len(slots),
        "appointment": len(appointments),
        "charge": len(charges) + len(historic),
        "waiting_list": len(waiting),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Load deterministic synthetic data.")
    parser.add_argument("--seed", type=int, default=settings.seed_value)
    parser.add_argument("--patients", type=int, default=settings.seed_patients)
    parser.add_argument("--agenda-days", type=int, default=settings.seed_agenda_days)
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
        agenda_days=args.agenda_days,
        base_date=args.base_date or now_at_clinic().date(),
    )

    with session_scope() as session:
        if args.if_empty and not database_is_empty(session):
            print("Seed skipped: the database already holds data.")
            return 0
        counts = seed_database(session, params)

    print(f"Seed done (seed={params.seed}, base date={params.base_date}):")
    for table, count in counts.items():
        print(f"  {table:<14} {count:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
