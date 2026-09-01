"""Domain vocabulary.

Class names and generic values are English, because an engineer reads them.
A value stays Spanish only when it is a Colombian legal term that English does
not carry: `cartera` states, affiliation regimes, charge concepts and document
types. "Estar en mora" has a weight `overdue` loses, and `cuota_moderadora` is
not a copay. The generic machinery around them (scheduled, free, pending) has
exact English equivalents and uses them.

These values reach the wire, so renaming one is a migration, never an edit.
Nothing here is ever shown raw to a human: `backend/domain/labels.py` maps a
state to the Spanish a clinic employee reads.
"""

from __future__ import annotations

from enum import StrEnum


class AppointmentState(StrEnum):
    """The appointment state machine of §2.1, the sector standard."""

    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    WAITING = "waiting"
    ATTENDED = "attended"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"
    NO_SHOW = "no_show"


class SlotState(StrEnum):
    FREE = "free"
    BUSY = "busy"
    BLOCKED = "blocked"


class Regimen(StrEnum):
    """Colombian affiliation regimes. No English equivalent carries the
    entitlement rules attached to each one, so the names stand."""

    CONTRIBUTIVO = "contributivo"
    SUBSIDIADO = "subsidiado"
    PARTICULAR = "particular"
    SOAT = "soat"


class DocumentType(StrEnum):
    CC = "CC"  # cedula de ciudadania
    TI = "TI"  # tarjeta de identidad (minors)
    CE = "CE"  # cedula de extranjeria
    PA = "PA"  # pasaporte
    RC = "RC"  # registro civil
    PPT = "PPT"  # permiso por proteccion temporal


class Specialty(StrEnum):
    GENERAL_DENTISTRY = "general_dentistry"
    ORTHODONTICS = "orthodontics"
    ENDODONTICS = "endodontics"
    PERIODONTICS = "periodontics"
    PEDIATRIC_DENTISTRY = "pediatric_dentistry"


class ChargeConcept(StrEnum):
    """What a charge is for. `copago` and `cuota_moderadora` are distinct
    instruments under Colombian law, and `particular` is the self-pay tier."""

    COPAGO = "copago"
    CUOTA_MODERADORA = "cuota_moderadora"
    PARTICULAR = "particular"
    NO_SHOW = "no_show"


class ChargeState(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    VOIDED = "voided"


class CarteraState(StrEnum):
    """Whether the patient's account is current. The values stay Spanish: a
    patient `en_mora` is in a defined legal condition, not merely late."""

    AL_DIA = "al_dia"
    EN_MORA = "en_mora"


class WaitingListPriority(StrEnum):
    URGENT = "urgent"
    SENIORITY = "seniority"


class WaitingListState(StrEnum):
    ACTIVE = "active"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"


#: Terminal states: no transition leaves them.
FINAL_STATES: frozenset[AppointmentState] = frozenset(
    {
        AppointmentState.ATTENDED,
        AppointmentState.CANCELLED,
        AppointmentState.RESCHEDULED,
        AppointmentState.NO_SHOW,
    }
)

#: States in which an appointment still holds its slot. Drives the partial
#: unique index that makes double-booking impossible in the database.
STATES_HOLDING_SLOT: frozenset[AppointmentState] = frozenset(
    {
        AppointmentState.SCHEDULED,
        AppointmentState.CONFIRMED,
        AppointmentState.WAITING,
        AppointmentState.ATTENDED,
    }
)
