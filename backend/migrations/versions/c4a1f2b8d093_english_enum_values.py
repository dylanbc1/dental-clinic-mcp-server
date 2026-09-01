"""Translate the generic enum values to English.

The i18n pass moved every value an engineer reads into English. Domain values
stay Spanish, so `regimen_enum`, `tipo_documento_enum`, `concepto_cargo_enum`
and the `cartera` states are untouched here: `en_mora` and `cuota_moderadora`
carry legal meaning English does not.

`ALTER TYPE ... RENAME VALUE` rewrites the label in `pg_enum` in place. Stored
rows, the partial unique index on `cita` and the one on `lista_espera` all
reference the value by its internal id, not by its text, so they follow the
rename without a rewrite and without a window where double-booking is possible.
That is the reason this is a rename rather than a drop-and-recreate.

Revision ID: c4a1f2b8d093
Revises: 970626c5b21a
"""

from __future__ import annotations

from collections.abc import Iterator

from alembic import op

revision: str = "c4a1f2b8d093"
down_revision: str | None = "970626c5b21a"
branch_labels: None = None
depends_on: None = None

#: Enum type -> (old value, new value). Only generic vocabulary appears here.
RENAMED_VALUES: dict[str, tuple[tuple[str, str], ...]] = {
    "estado_cita_enum": (
        ("agendada", "scheduled"),
        ("confirmada", "confirmed"),
        ("en_espera", "waiting"),
        ("atendida", "attended"),
        ("cancelada", "cancelled"),
        ("reprogramada", "rescheduled"),
        ("no_asistio", "no_show"),
    ),
    "estado_slot_enum": (
        ("libre", "free"),
        ("ocupado", "busy"),
        ("bloqueado", "blocked"),
    ),
    "estado_cargo_enum": (
        ("pendiente", "pending"),
        ("pagado", "paid"),
        ("anulado", "voided"),
    ),
    "prioridad_lista_enum": (
        ("urgencia", "urgent"),
        ("antiguedad", "seniority"),
    ),
    "estado_lista_enum": (
        ("activa", "active"),
        ("ofrecida", "offered"),
        ("aceptada", "accepted"),
        ("retirada", "withdrawn"),
    ),
    "especialidad_enum": (
        ("odontologia_general", "general_dentistry"),
        ("ortodoncia", "orthodontics"),
        ("endodoncia", "endodontics"),
        ("periodoncia", "periodontics"),
        ("odontopediatria", "pediatric_dentistry"),
    ),
}

#: Enum types whose name is itself generic. `regimen_enum`, `tipo_documento_enum`
#: and `concepto_cargo_enum` are left alone at this revision; `a3f7c21b5e48`
#: later moves the last two, on the grounds that a type name is structure even
#: when the values inside it are Colombian legal terms. There is no cartera type
#: here: that state is computed per request, never stored.
RENAMED_TYPES: tuple[tuple[str, str], ...] = (
    ("estado_cita_enum", "appointment_state_enum"),
    ("estado_slot_enum", "slot_state_enum"),
    ("estado_cargo_enum", "charge_state_enum"),
    ("prioridad_lista_enum", "waiting_list_priority_enum"),
    ("estado_lista_enum", "waiting_list_state_enum"),
    ("especialidad_enum", "specialty_enum"),
)


def _pairs(*, reverse: bool) -> Iterator[tuple[str, str, str]]:
    """(type, from, to) for every value rename, in either direction."""
    for enum_type, values in RENAMED_VALUES.items():
        for old, new in values:
            yield (enum_type, new, old) if reverse else (enum_type, old, new)


def upgrade() -> None:
    for enum_type, old, new in _pairs(reverse=False):
        op.execute(f"ALTER TYPE {enum_type} RENAME VALUE '{old}' TO '{new}'")
    for old_type, new_type in RENAMED_TYPES:
        op.execute(f"ALTER TYPE {old_type} RENAME TO {new_type}")


def downgrade() -> None:
    # Types first: the value renames below name the types by their old name.
    for old_type, new_type in RENAMED_TYPES:
        op.execute(f"ALTER TYPE {new_type} RENAME TO {old_type}")
    for enum_type, old, new in _pairs(reverse=True):
        op.execute(f"ALTER TYPE {enum_type} RENAME VALUE '{old}' TO '{new}'")
