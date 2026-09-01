"""Move the wire and the schema to English names.

Phase 5 of the i18n pass. Tables, columns, indexes and check constraints take
the English names the code now uses. Domain vocabulary keeps its Spanish:
`regimen`, `eps`, `nit`, `cartera`, `afiliacion` and `cuota_moderadora` name
things Colombian health law defines, and an English word for them would be a
worse name, not a more readable one.

Everything here is a catalogue rename. No row is rewritten, no index is
dropped, and the partial unique index that prevents double-booking follows its
table and column automatically, so there is never a window in which two agents
could take the same slot.

Two columns carry a name that differs from their Python attribute, because
PostgreSQL reserves the word: `agenda_slot.starts_at`/`ends_at` behind
`slot.start`/`slot.end`, and `appointment_history.changed_by` behind
`.user`. Implicit `_pkey`/`_fkey` names PostgreSQL generated itself are left
alone: nobody writes them by hand and renaming them buys no readability.

Revision ID: 8b7d41e0c6a2
Revises: c4a1f2b8d093
"""

from __future__ import annotations

from alembic import op

revision: str = "8b7d41e0c6a2"
down_revision: str | None = "c4a1f2b8d093"
branch_labels: None = None
depends_on: None = None

#: (old table, new table)
TABLES: tuple[tuple[str, str], ...] = (
    ("clinica", "clinic"),
    ("paciente", "patient"),
    ("profesional", "professional"),
    ("cita", "appointment"),
    ("lista_espera", "waiting_list"),
    ("cita_historial", "appointment_history"),
    ("cargo", "charge"),
)

#: (table after the rename above, old column, new column)
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agenda_slot", "profesional_id", "professional_id"),
    ("agenda_slot", "fecha", "day"),
    ("agenda_slot", "inicio", "starts_at"),
    ("agenda_slot", "fin", "ends_at"),
    ("agenda_slot", "estado", "status"),
    ("agenda_slot", "creada_en", "created_at"),
    ("agenda_slot", "actualizada_en", "updated_at"),
    ("charge", "paciente_id", "patient_id"),
    ("charge", "cita_id", "appointment_id"),
    ("charge", "concepto", "concept"),
    ("charge", "monto", "amount"),
    ("charge", "descripcion", "description"),
    ("charge", "estado", "status"),
    ("charge", "vencimiento", "due_date"),
    ("charge", "pagado_en", "paid_at"),
    ("charge", "creada_en", "created_at"),
    ("charge", "actualizada_en", "updated_at"),
    ("appointment", "paciente_id", "patient_id"),
    ("appointment", "profesional_id", "professional_id"),
    ("appointment", "estado", "status"),
    ("appointment", "motivo", "reason"),
    ("appointment", "motivo_registrado_en", "reason_recorded_at"),
    ("appointment", "motivo_registrado_por", "reason_recorded_by"),
    ("appointment", "motivo_cancelacion", "cancellation_reason"),
    ("appointment", "creada_por", "created_by"),
    ("appointment", "cita_origen_id", "source_appointment_id"),
    ("appointment", "creada_en", "created_at"),
    ("appointment", "actualizada_en", "updated_at"),
    ("appointment_history", "cita_id", "appointment_id"),
    ("appointment_history", "estado_anterior", "previous_status"),
    ("appointment_history", "estado_nuevo", "new_status"),
    ("appointment_history", "usuario", "changed_by"),
    ("appointment_history", "motivo", "reason"),
    ("appointment_history", "momento", "occurred_at"),
    ("clinic", "nombre", "name"),
    ("clinic", "especialidad", "specialty"),
    ("clinic", "direccion", "address"),
    ("clinic", "telefono", "phone"),
    ("clinic", "ciudad", "city"),
    ("clinic", "zona_horaria", "timezone_name"),
    ("clinic", "creada_en", "created_at"),
    ("clinic", "actualizada_en", "updated_at"),
    ("waiting_list", "paciente_id", "patient_id"),
    ("waiting_list", "especialidad", "specialty"),
    ("waiting_list", "prioridad", "priority"),
    ("waiting_list", "estado", "status"),
    ("waiting_list", "notas", "notes"),
    ("waiting_list", "ofrecida_en", "offered_at"),
    ("waiting_list", "slot_ofrecido_id", "offered_slot_id"),
    ("waiting_list", "creada_en", "created_at"),
    ("waiting_list", "actualizada_en", "updated_at"),
    ("patient", "tipo_documento", "document_type"),
    ("patient", "documento", "document_number"),
    ("patient", "nombre", "name"),
    ("patient", "telefono", "phone"),
    ("patient", "fecha_nacimiento", "birth_date"),
    ("patient", "afiliacion_activa", "afiliacion_active"),
    ("patient", "nivel_cuota_moderadora", "cuota_moderadora_level"),
    ("patient", "consentimiento_datos_clinicos", "clinical_data_consent"),
    ("patient", "consentimiento_otorgado_en", "consent_granted_at"),
    ("patient", "creada_en", "created_at"),
    ("patient", "actualizada_en", "updated_at"),
    ("professional", "clinica_id", "clinic_id"),
    ("professional", "nombre", "name"),
    ("professional", "registro", "license_number"),
    ("professional", "especialidad", "specialty"),
    ("professional", "activo", "active"),
    ("professional", "creada_en", "created_at"),
    ("professional", "actualizada_en", "updated_at"),
)

#: (old index, new index)
INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_cargo_paciente_estado", "ix_charge_patient_status"),
    ("ix_cita_estado", "ix_appointment_status"),
    ("ix_cita_paciente_estado", "ix_appointment_patient_status"),
    ("ix_cita_profesional_id", "ix_appointment_professional_id"),
    ("ix_historial_cita_momento", "ix_history_appointment_occurred_at"),
    ("ix_lista_espera_cola", "ix_waiting_list_queue"),
    ("ix_paciente_documento", "ix_patient_document_number"),
    ("ix_paciente_nombre_lower", "ix_patient_name_lower"),
    ("ix_paciente_regimen", "ix_patient_regimen"),
    ("ix_profesional_clinica_id", "ix_professional_clinic_id"),
    ("ix_profesional_especialidad", "ix_professional_specialty"),
    ("ix_slot_busqueda", "ix_slot_search"),
    ("uq_cita_idempotency", "uq_appointment_idempotency"),
    ("uq_cita_slot_activa", "uq_appointment_slot_active"),
    ("uq_lista_espera_activa", "uq_waiting_list_active"),
    ("uq_paciente_documento", "uq_patient_document"),
    ("uq_slot_profesional_inicio", "uq_slot_professional_start"),
)

#: (table, old constraint, new constraint)
CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("charge", "ck_cargo_monto_no_negativo", "ck_charge_amount_not_negative"),
    ("patient", "ck_paciente_nivel_cuota", "ck_patient_cuota_moderadora_level"),
    ("agenda_slot", "ck_slot_rango_valido", "ck_slot_valid_range"),
)


def upgrade() -> None:
    for old, new in TABLES:
        op.rename_table(old, new)
    for table, old, new in COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    for old, new in INDEXES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    for table, old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")


def downgrade() -> None:
    for table, old, new in CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    for old, new in INDEXES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    for table, old, new in COLUMNS:
        op.alter_column(table, new, new_column_name=old)
    for old, new in TABLES:
        op.rename_table(new, old)
