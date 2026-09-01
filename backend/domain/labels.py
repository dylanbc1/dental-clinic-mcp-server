"""The only place an internal value becomes words a person reads.

State values, error codes and tool names are English because engineers read
them. The text a clinic employee reads is Spanish. Splicing one into the other
gives a receptionist in Bogotá a sentence with `scheduled` in the middle of it,
so no other module interpolates a state into prose: it comes through here.

Two rules for callers:

* If the state carries information the person needs, ask for its label.
* If it does not, say what changes instead of naming the state. "El cupo
  quedará libre" tells the front desk everything that matters; adding "la cita
  pasará a 'cancelled'" tells them nothing they can act on.

`state_label` raises on an unknown state on purpose. A missing label is a bug,
and failing is better than leaking English into the question a human approves.
"""

from __future__ import annotations

from backend.enums import AppointmentState, Specialty

#: Spanish for each appointment state. The words the front desk already uses.
APPOINTMENT_STATE_LABELS: dict[AppointmentState, str] = {
    AppointmentState.SCHEDULED: "agendada",
    AppointmentState.CONFIRMED: "confirmada",
    AppointmentState.WAITING: "en espera",
    AppointmentState.ATTENDED: "atendida",
    AppointmentState.CANCELLED: "cancelada",
    AppointmentState.RESCHEDULED: "reprogramada",
    AppointmentState.NO_SHOW: "no asistió",
}


#: Spanish for each specialty. A receptionist books `periodoncia`, not
#: `periodontics`, however the value travels on the wire.
SPECIALTY_LABELS: dict[Specialty, str] = {
    Specialty.GENERAL_DENTISTRY: "odontología general",
    Specialty.ORTHODONTICS: "ortodoncia",
    Specialty.ENDODONTICS: "endodoncia",
    Specialty.PERIODONTICS: "periodoncia",
    Specialty.PEDIATRIC_DENTISTRY: "odontopediatría",
}


def specialty_label(specialty: Specialty | str) -> str:
    """The Spanish a person reads for a dental specialty."""
    return SPECIALTY_LABELS[Specialty(specialty)]


def state_label(state: AppointmentState | str) -> str:
    """The Spanish a person reads for an appointment state.

    Accepts the raw string the backend puts on the wire, so a caller holding a
    decoded JSON payload does not have to rebuild the enum first.
    """
    return APPOINTMENT_STATE_LABELS[AppointmentState(state)]
