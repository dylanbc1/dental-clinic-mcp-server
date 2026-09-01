"""Service layer: domain rules applied to persisted state.

Everything under `backend/domain/` up to here is pure. This module is where
that pure logic meets the database, and it is deliberately the *only* place
where an appointment changes state. Three invariants hold for every mutation:

1. The transition is validated by :mod:`backend.domain.states` first. There is
   no code path that writes ``cita.estado`` without going through it.
2. The audit row is written in the same unit of work as the change it records,
   so an audit gap is not possible.
3. Slot release, charge creation and waiting-list triggering are derived from
   :class:`EfectosTransicion`, never re-decided by the caller.

The MCP tool layer calls the REST API, which calls these functions. It never
reaches the ORM directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from backend.domain.afiliacion import ResultadoAfiliacion, validar_afiliacion
from backend.domain.cartera import (
    POLITICA_POR_DEFECTO,
    CargoPendiente,
    PoliticaCartera,
    ResumenCartera,
    alerta_al_agendar,
    calcular_cargo_por_atencion,
    calcular_cargo_por_no_show,
    resumir_cartera,
)
from backend.domain.errors import (
    CitaNoEncontrada,
    ConflictoConcurrencia,
    ConsentimientoRequerido,
    EspecialidadNoCoincide,
    ListaEsperaVacia,
    PacienteNoEncontrado,
    PacienteYaTieneCita,
    ProfesionalNoEncontrado,
    SlotEnElPasado,
    SlotNoDisponible,
    SlotNoEncontrado,
    YaEnListaEspera,
)
from backend.domain.states import EfectosTransicion, validar_transicion
from backend.domain.time import a_local, ahora_utc
from backend.domain.waiting_list import EntradaListaEspera, siguiente_en_lista
from backend.enums import (
    ESTADOS_QUE_OCUPAN_SLOT,
    ConceptoCargo,
    Especialidad,
    EstadoCargo,
    EstadoCita,
    EstadoListaEspera,
    EstadoSlot,
    PrioridadListaEspera,
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

#: How long a patient has to settle a charge before it counts as arrears.
PLAZO_PAGO = timedelta(days=30)

#: How many alternative slots an error offers when the requested one is taken.
ALTERNATIVAS_SUGERIDAS = 3


# --------------------------------------------------------------------------- #
# Read-side result objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SlotDisponible:
    slot_id: int
    profesional_id: int
    profesional: str
    especialidad: Especialidad
    inicio: datetime
    fin: datetime

    @property
    def etiqueta(self) -> str:
        """Human-facing label in clinic local time, which is what the model reads."""
        local = a_local(self.inicio)
        return f"{local:%Y-%m-%d %H:%M} ({self.profesional})"


@dataclass(frozen=True, slots=True)
class ResultadoAgendamiento:
    cita: Cita
    afiliacion: ResultadoAfiliacion
    alerta_cartera: str | None
    #: True when an identical idempotency key had already created this
    #: appointment. The caller reports success, not a duplicate.
    reutilizada: bool = False


@dataclass(frozen=True, slots=True)
class ResultadoTransicion:
    cita: Cita
    efectos: EfectosTransicion
    cargo_generado: Cargo | None = None
    siguiente_en_espera: ListaEspera | None = None


@dataclass(frozen=True, slots=True)
class OfertaCupo:
    entrada: ListaEspera
    paciente: Paciente
    slot: AgendaSlot
    posicion_original: int


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


def obtener_paciente(session: Session, paciente_id: int) -> Paciente:
    paciente = session.get(Paciente, paciente_id)
    if paciente is None:
        raise PacienteNoEncontrado(
            f"There is no patient with id {paciente_id}.",
            sugerencia="Look them up first with buscar_paciente, by documento or by name.",
            detalles={"paciente_id": paciente_id},
        )
    return paciente


def obtener_cita(session: Session, cita_id: int) -> Cita:
    cita = session.scalar(
        select(Cita)
        .where(Cita.id == cita_id)
        .options(
            selectinload(Cita.paciente),
            selectinload(Cita.profesional),
            selectinload(Cita.slot),
            selectinload(Cita.historial),
        )
    )
    if cita is None:
        raise CitaNoEncontrada(
            f"There is no appointment with id {cita_id}.",
            sugerencia="List the patient's appointments with listar_citas_paciente.",
            detalles={"cita_id": cita_id},
        )
    return cita


def obtener_profesional(session: Session, profesional_id: int) -> Profesional:
    profesional = session.get(Profesional, profesional_id)
    if profesional is None:
        raise ProfesionalNoEncontrado(
            f"There is no professional with id {profesional_id}.",
            sugerencia="See the list in the clinica://info resource.",
            detalles={"profesional_id": profesional_id},
        )
    return profesional


def obtener_clinica(session: Session) -> Clinica:
    clinica = session.scalar(select(Clinica).order_by(Clinica.id).limit(1))
    if clinica is None:  # pragma: no cover - only on an unseeded database
        raise PacienteNoEncontrado(
            "The database has no clinic configured.",
            sugerencia="Run `make seed` to load the synthetic data.",
        )
    return clinica


def buscar_pacientes(
    session: Session,
    *,
    documento: str | None = None,
    nombre: str | None = None,
    limite: int = 10,
) -> list[Paciente]:
    """Search by document (exact) or name (case-insensitive substring).

    Document match is exact on purpose: a partial document number is how you
    hand the wrong person's record to an agent.
    """
    consulta = select(Paciente)
    if documento:
        consulta = consulta.where(Paciente.documento == documento.strip())
    elif nombre:
        patron = f"%{nombre.strip().lower()}%"
        consulta = consulta.where(func.lower(Paciente.nombre).like(patron))
    else:
        raise PacienteNoEncontrado(
            "You must give a documento or a name to search by.",
            sugerencia="Call buscar_paciente with 'documento' or with 'nombre'.",
        )
    return list(session.scalars(consulta.order_by(Paciente.nombre).limit(limite)))


def consultar_disponibilidad(
    session: Session,
    *,
    especialidad: Especialidad | None = None,
    fecha: date | None = None,
    profesional_id: int | None = None,
    limite: int = 20,
    ahora: datetime | None = None,
) -> list[SlotDisponible]:
    """Free slots, never in the past, ordered chronologically."""
    referencia = ahora or ahora_utc()
    consulta = (
        select(AgendaSlot, Profesional)
        .join(Profesional, AgendaSlot.profesional_id == Profesional.id)
        .where(
            AgendaSlot.estado == EstadoSlot.LIBRE,
            AgendaSlot.inicio > referencia,
            Profesional.activo.is_(True),
        )
    )
    if especialidad is not None:
        consulta = consulta.where(Profesional.especialidad == especialidad)
    if fecha is not None:
        consulta = consulta.where(AgendaSlot.fecha == fecha)
    if profesional_id is not None:
        obtener_profesional(session, profesional_id)
        consulta = consulta.where(AgendaSlot.profesional_id == profesional_id)

    filas = session.execute(consulta.order_by(AgendaSlot.inicio).limit(limite)).all()
    return [
        SlotDisponible(
            slot_id=slot.id,
            profesional_id=profesional.id,
            profesional=profesional.nombre,
            especialidad=profesional.especialidad,
            inicio=slot.inicio,
            fin=slot.fin,
        )
        for slot, profesional in filas
    ]


def listar_citas_paciente(
    session: Session,
    paciente_id: int,
    *,
    desde: date | None = None,
    hasta: date | None = None,
    limite: int = 50,
) -> list[Cita]:
    obtener_paciente(session, paciente_id)
    consulta = (
        select(Cita)
        .join(AgendaSlot, Cita.slot_id == AgendaSlot.id)
        .where(Cita.paciente_id == paciente_id)
        .options(selectinload(Cita.slot), selectinload(Cita.profesional))
    )
    if desde is not None:
        consulta = consulta.where(AgendaSlot.fecha >= desde)
    if hasta is not None:
        consulta = consulta.where(AgendaSlot.fecha <= hasta)
    return list(session.scalars(consulta.order_by(AgendaSlot.inicio.desc()).limit(limite)))


def validar_afiliacion_paciente(session: Session, paciente_id: int) -> ResultadoAfiliacion:
    paciente = obtener_paciente(session, paciente_id)
    return validar_afiliacion(
        paciente.regimen,
        paciente.afiliacion_activa,
        nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
    )


def _cargos_pendientes(session: Session, paciente_id: int) -> list[CargoPendiente]:
    filas = session.scalars(
        select(Cargo).where(Cargo.paciente_id == paciente_id, Cargo.estado == EstadoCargo.PENDIENTE)
    )
    return [
        CargoPendiente(
            cargo_id=c.id,
            concepto=c.concepto,
            monto=c.monto,
            vencimiento=c.vencimiento,
            estado=c.estado,
        )
        for c in filas
    ]


def consultar_cartera(
    session: Session,
    paciente_id: int,
    *,
    hoy: date | None = None,
    politica: PoliticaCartera = POLITICA_POR_DEFECTO,
) -> ResumenCartera:
    obtener_paciente(session, paciente_id)
    return resumir_cartera(
        paciente_id,
        _cargos_pendientes(session, paciente_id),
        hoy=hoy or a_local(ahora_utc()).date(),
        politica=politica,
    )


def agenda_del_dia(session: Session, fecha: date) -> list[Cita]:
    """Every appointment of a day, whatever its state. The front desk view."""
    return list(
        session.scalars(
            select(Cita)
            .join(AgendaSlot, Cita.slot_id == AgendaSlot.id)
            .where(AgendaSlot.fecha == fecha)
            .options(
                selectinload(Cita.slot),
                selectinload(Cita.paciente),
                selectinload(Cita.profesional),
            )
            .order_by(AgendaSlot.inicio)
        )
    )


def entradas_lista_espera(
    session: Session, especialidad: Especialidad | None = None
) -> list[ListaEspera]:
    consulta = select(ListaEspera).where(ListaEspera.estado == EstadoListaEspera.ACTIVA)
    if especialidad is not None:
        consulta = consulta.where(ListaEspera.especialidad == especialidad)
    return list(session.scalars(consulta.options(selectinload(ListaEspera.paciente))))


# --------------------------------------------------------------------------- #
# Write side
# --------------------------------------------------------------------------- #


def _auditar(
    session: Session,
    cita: Cita,
    *,
    estado_anterior: EstadoCita | None,
    estado_nuevo: EstadoCita,
    usuario: str,
    motivo: str | None = None,
) -> CitaHistorial:
    """Append one audit row. Same unit of work as the change it describes."""
    registro = CitaHistorial(
        cita_id=cita.id,
        estado_anterior=estado_anterior,
        estado_nuevo=estado_nuevo,
        usuario=usuario,
        motivo=motivo,
        momento=ahora_utc(),
    )
    session.add(registro)
    return registro


def slot_reservable(session: Session, slot_id: int, *, ahora: datetime | None = None) -> AgendaSlot:
    """The slot, if it can still be booked. Raises the structured error if not.

    Shared by the booking path and by the read endpoint the tool layer calls
    before proposing, so both refuse for exactly the same reasons.
    """
    ahora = ahora or ahora_utc()
    slot = session.get(AgendaSlot, slot_id)
    if slot is None:
        raise SlotNoEncontrado(
            f"There is no slot with id {slot_id}.",
            sugerencia="Check current slots with consultar_disponibilidad.",
            detalles={"slot_id": slot_id},
        )
    if slot.inicio <= ahora:
        raise SlotEnElPasado(
            f"The slot at {a_local(slot.inicio):%Y-%m-%d %H:%M} is in the past.",
            sugerencia="Ask for future availability with consultar_disponibilidad.",
            detalles={"slot_id": slot_id, "inicio": slot.inicio.isoformat()},
        )
    if slot.estado is not EstadoSlot.LIBRE:
        alternativas = consultar_disponibilidad(
            session,
            especialidad=slot.profesional.especialidad,
            limite=ALTERNATIVAS_SUGERIDAS,
            ahora=ahora,
        )
        raise SlotNoDisponible(
            f"The slot at {a_local(slot.inicio):%Y-%m-%d %H:%M} is no longer free.",
            sugerencia=(
                "The closest free slots are: " + ", ".join(a.etiqueta for a in alternativas) + "."
                if alternativas
                else "There are no free slots coming up for that specialty."
            ),
            detalles={
                "slot_id": slot_id,
                "alternativas": [
                    {"slot_id": a.slot_id, "inicio": a.inicio.isoformat()} for a in alternativas
                ],
            },
        )
    return slot


def validar_reserva(
    session: Session,
    slot_id: int,
    *,
    paciente_id: int | None = None,
    especialidad_esperada: Especialidad | None = None,
    excluir_cita_id: int | None = None,
    ahora: datetime | None = None,
) -> AgendaSlot:
    """Everything that must hold for a booking to succeed, in one place.

    Called twice: once by the tool layer before proposing, so a human is never
    asked to approve an operation that cannot work, and once by `agendar_cita`
    at the moment of effect, because the state can change in between. Sharing
    the function is what guarantees both refuse for the same reasons.
    """
    referencia = ahora or ahora_utc()
    slot = slot_reservable(session, slot_id, ahora=referencia)

    if especialidad_esperada is not None and slot.profesional.especialidad != especialidad_esperada:
        raise EspecialidadNoCoincide(
            f"The slot is for {slot.profesional.especialidad}, not {especialidad_esperada}.",
            sugerencia=(
                f"Ask for availability with especialidad='{especialidad_esperada}' and book again."
            ),
            detalles={
                "especialidad_del_cupo": str(slot.profesional.especialidad),
                "especialidad_pedida": str(especialidad_esperada),
            },
        )

    if paciente_id is not None:
        consulta = (
            select(Cita)
            .join(AgendaSlot, Cita.slot_id == AgendaSlot.id)
            .where(
                Cita.paciente_id == paciente_id,
                Cita.estado.in_(ESTADOS_QUE_OCUPAN_SLOT),
                AgendaSlot.inicio < slot.fin,
                AgendaSlot.fin > slot.inicio,
            )
        )
        if excluir_cita_id is not None:
            consulta = consulta.where(Cita.id != excluir_cita_id)
        solapada = session.scalar(consulta)
        if solapada is not None:
            raise PacienteYaTieneCita(
                "The patient already has an appointment that overlaps that hour.",
                sugerencia=(
                    f"Cancel or reschedule appointment {solapada.id} before booking "
                    "another one at the same time."
                ),
                detalles={"cita_existente_id": solapada.id},
            )

    return slot


def agendar_cita(
    session: Session,
    *,
    paciente_id: int,
    slot_id: int,
    usuario: str,
    idempotency_key: str | None = None,
    ahora: datetime | None = None,
    especialidad_esperada: Especialidad | None = None,
) -> ResultadoAgendamiento:
    """Book a free slot for a patient.

    Outstanding debt produces a *warning*, never a refusal: clinics do not turn
    patients away over an unpaid copayment (§2.3).
    """
    referencia = ahora or ahora_utc()

    if idempotency_key:
        existente = session.scalar(select(Cita).where(Cita.idempotency_key == idempotency_key))
        if existente is not None:
            # The retry of a call that already succeeded is a success.
            return ResultadoAgendamiento(
                cita=existente,
                afiliacion=validar_afiliacion_paciente(session, existente.paciente_id),
                alerta_cartera=None,
                reutilizada=True,
            )

    paciente = obtener_paciente(session, paciente_id)
    slot = validar_reserva(
        session,
        slot_id,
        paciente_id=paciente_id,
        especialidad_esperada=especialidad_esperada,
        ahora=referencia,
    )

    afiliacion = validar_afiliacion(
        paciente.regimen,
        paciente.afiliacion_activa,
        nivel_cuota_moderadora=paciente.nivel_cuota_moderadora,
    )
    alerta = alerta_al_agendar(consultar_cartera(session, paciente_id))

    cita = Cita(
        paciente_id=paciente_id,
        profesional_id=slot.profesional_id,
        slot_id=slot.id,
        estado=EstadoCita.AGENDADA,
        creada_por=usuario,
        idempotency_key=idempotency_key,
    )
    slot.estado = EstadoSlot.OCUPADO
    session.add(cita)

    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the race against another agent between the check and the write.
        # The database is what turns that into a conflict rather than a
        # duplicate. The caller's transaction scope performs the rollback.
        raise ConflictoConcurrencia(
            "Another process took that slot while the appointment was being created.",
            sugerencia="Check availability again and book a different slot.",
            detalles={"slot_id": slot_id},
        ) from exc

    _auditar(
        session,
        cita,
        estado_anterior=None,
        estado_nuevo=EstadoCita.AGENDADA,
        usuario=usuario,
    )
    session.flush()
    return ResultadoAgendamiento(cita=cita, afiliacion=afiliacion, alerta_cartera=alerta)


def _crear_cargo(
    session: Session,
    cita: Cita,
    *,
    concepto: ConceptoCargo,
    monto: Decimal,
    descripcion: str,
    hoy: date,
) -> Cargo:
    cargo = Cargo(
        paciente_id=cita.paciente_id,
        cita_id=cita.id,
        concepto=concepto,
        monto=monto,
        descripcion=descripcion,
        estado=EstadoCargo.PENDIENTE,
        vencimiento=hoy + PLAZO_PAGO,
    )
    session.add(cargo)
    return cargo


def cambiar_estado(
    session: Session,
    cita_id: int,
    nuevo_estado: EstadoCita,
    *,
    usuario: str,
    motivo: str | None = None,
    politica: PoliticaCartera = POLITICA_POR_DEFECTO,
    hoy: date | None = None,
) -> ResultadoTransicion:
    """The single entry point for every appointment state change.

    Validates, audits, and applies the derived effects: releasing the slot,
    creating the charge, surfacing the next patient on the waiting list.
    """
    cita = obtener_cita(session, cita_id)
    anterior = cita.estado
    efectos = validar_transicion(anterior, nuevo_estado, motivo=motivo)

    cita.estado = nuevo_estado
    if nuevo_estado is EstadoCita.CANCELADA:
        cita.motivo_cancelacion = motivo

    if efectos.libera_slot:
        cita.slot.estado = EstadoSlot.LIBRE

    _auditar(
        session,
        cita,
        estado_anterior=anterior,
        estado_nuevo=nuevo_estado,
        usuario=usuario,
        motivo=motivo,
    )

    cargo: Cargo | None = None
    if efectos.genera_cargo:
        fecha_cargo = hoy or a_local(cita.slot.inicio).date()
        calculado = None
        if nuevo_estado is EstadoCita.ATENDIDA:
            afiliacion = validar_afiliacion(
                cita.paciente.regimen,
                cita.paciente.afiliacion_activa,
                nivel_cuota_moderadora=cita.paciente.nivel_cuota_moderadora,
            )
            calculado = calcular_cargo_por_atencion(
                afiliacion,
                str(cita.profesional.especialidad),
                nivel_cuota_moderadora=cita.paciente.nivel_cuota_moderadora,
            )
        else:  # EstadoCita.NO_ASISTIO
            calculado = calcular_cargo_por_no_show(
                estaba_confirmada=anterior is EstadoCita.CONFIRMADA, politica=politica
            )
        if calculado is not None:
            cargo = _crear_cargo(
                session,
                cita,
                concepto=calculado.concepto,
                monto=calculado.monto,
                descripcion=calculado.descripcion,
                hoy=fecha_cargo,
            )

    siguiente: ListaEspera | None = None
    if efectos.dispara_lista_espera:
        siguiente = _siguiente_candidato(
            session, cita.profesional.especialidad, excluir=cita.paciente_id
        )

    session.flush()
    return ResultadoTransicion(
        cita=cita, efectos=efectos, cargo_generado=cargo, siguiente_en_espera=siguiente
    )


def _siguiente_candidato(
    session: Session, especialidad: Especialidad, *, excluir: int | None = None
) -> ListaEspera | None:
    """Peek at the head of the waiting list. Returns ``None`` when empty.

    Peeking must not fail: a cancellation with nobody waiting is a perfectly
    normal outcome, not an error the caller has to handle.
    """
    entradas = entradas_lista_espera(session, especialidad)
    if not entradas:
        return None
    dominio = [a_entrada_dominio(e) for e in entradas]
    try:
        elegida = siguiente_en_lista(
            dominio,
            especialidad,
            excluir_pacientes=frozenset({excluir}) if excluir is not None else frozenset(),
        )
    except ListaEsperaVacia:
        return None
    return next(e for e in entradas if e.id == elegida.entrada_id)


def a_entrada_dominio(fila: ListaEspera) -> EntradaListaEspera:
    """Adapt a persisted row to the pure ordering type of `lista_espera`."""
    return EntradaListaEspera(
        entrada_id=fila.id,
        paciente_id=fila.paciente_id,
        especialidad=fila.especialidad,
        prioridad=fila.prioridad,
        creada_en=fila.creada_en,
        estado=fila.estado,
    )


def confirmar_cita(session: Session, cita_id: int, *, usuario: str) -> ResultadoTransicion:
    return cambiar_estado(session, cita_id, EstadoCita.CONFIRMADA, usuario=usuario)


def cancelar_cita(
    session: Session, cita_id: int, *, motivo: str, usuario: str
) -> ResultadoTransicion:
    return cambiar_estado(session, cita_id, EstadoCita.CANCELADA, usuario=usuario, motivo=motivo)


def registrar_asistencia(
    session: Session, cita_id: int, estado: EstadoCita, *, usuario: str
) -> ResultadoTransicion:
    return cambiar_estado(session, cita_id, estado, usuario=usuario)


def reprogramar_cita(
    session: Session,
    cita_id: int,
    nuevo_slot_id: int,
    *,
    usuario: str,
    motivo: str | None = None,
    ahora: datetime | None = None,
) -> ResultadoTransicion:
    """Move an appointment to another slot.

    Two effects in one operation, which is exactly why it needs a human gate:
    the old slot is freed and a new one is taken. The new appointment keeps a
    pointer back to the original so the chain stays auditable.
    """
    referencia = ahora or ahora_utc()
    original = obtener_cita(session, cita_id)
    nuevo_slot = validar_reserva(
        session,
        nuevo_slot_id,
        paciente_id=original.paciente_id,
        excluir_cita_id=original.id,
        ahora=referencia,
    )

    resultado = cambiar_estado(
        session,
        cita_id,
        EstadoCita.REPROGRAMADA,
        usuario=usuario,
        motivo=motivo or "Reschedule requested",
    )

    nueva = Cita(
        paciente_id=original.paciente_id,
        profesional_id=nuevo_slot.profesional_id,
        slot_id=nuevo_slot.id,
        estado=EstadoCita.AGENDADA,
        creada_por=usuario,
        cita_origen_id=original.id,
    )
    nuevo_slot.estado = EstadoSlot.OCUPADO
    session.add(nueva)
    session.flush()

    _auditar(
        session,
        nueva,
        estado_anterior=None,
        estado_nuevo=EstadoCita.AGENDADA,
        usuario=usuario,
        motivo=f"Rescheduled from appointment {original.id}",
    )
    session.flush()
    return ResultadoTransicion(
        cita=nueva, efectos=resultado.efectos, siguiente_en_espera=resultado.siguiente_en_espera
    )


def ofrecer_cupo_lista_espera(
    session: Session, slot_id: int, *, usuario: str, ahora: datetime | None = None
) -> OfertaCupo:
    """Offer a freed slot to the next patient in the queue.

    This does not book anything: it records the offer and returns who to
    contact. Booking is a separate, separately-approved decision.
    """
    referencia = ahora or ahora_utc()
    slot = slot_reservable(session, slot_id, ahora=referencia)
    especialidad = slot.profesional.especialidad

    entradas = entradas_lista_espera(session, especialidad)
    dominio = [a_entrada_dominio(e) for e in entradas]
    elegida = siguiente_en_lista(dominio, especialidad)
    fila = next(e for e in entradas if e.id == elegida.entrada_id)
    posicion = [d.entrada_id for d in sorted(dominio, key=lambda d: d.clave_orden)].index(
        elegida.entrada_id
    ) + 1

    fila.estado = EstadoListaEspera.OFRECIDA
    fila.ofrecida_en = referencia
    fila.slot_ofrecido_id = slot.id
    session.flush()

    return OfertaCupo(
        entrada=fila,
        paciente=fila.paciente,
        slot=slot,
        posicion_original=posicion,
    )


def inscribir_en_lista_espera(
    session: Session,
    *,
    paciente_id: int,
    especialidad: Especialidad,
    prioridad: PrioridadListaEspera = PrioridadListaEspera.ANTIGUEDAD,
    notas: str | None = None,
) -> ListaEspera:
    obtener_paciente(session, paciente_id)
    existente = session.scalar(
        select(ListaEspera).where(
            ListaEspera.paciente_id == paciente_id,
            ListaEspera.especialidad == especialidad,
            ListaEspera.estado == EstadoListaEspera.ACTIVA,
        )
    )
    if existente is not None:
        raise YaEnListaEspera(
            f"The patient is already on the waiting list for {especialidad}.",
            sugerencia="Check their current position before enrolling them again.",
            detalles={"entrada_id": existente.id},
        )
    entrada = ListaEspera(
        paciente_id=paciente_id,
        especialidad=especialidad,
        prioridad=prioridad,
        estado=EstadoListaEspera.ACTIVA,
        notas=notas,
    )
    session.add(entrada)
    session.flush()
    return entrada


def registrar_motivo_consulta(session: Session, cita_id: int, motivo: str, *, usuario: str) -> Cita:
    """Attach a reason for consultation to an appointment. **Clinical data.**

    The one operation that crosses from administrative into clinical territory
    (Res. 2654/2019), so it refuses without recorded informed consent whatever
    the caller's scope. The scope check is necessary but not sufficient.
    """
    cita = obtener_cita(session, cita_id)
    paciente = cita.paciente

    if not paciente.consentimiento_datos_clinicos:
        raise ConsentimientoRequerido(
            (
                f"Patient {paciente.nombre} has no informed consent on file for the "
                "handling of clinical data."
            ),
            sugerencia=(
                "Obtain and record informed consent before writing the reason for "
                "consultation. This is a requirement of Resolución 2654/2019, not a "
                "validation of this system."
            ),
            detalles={"paciente_id": paciente.id, "cita_id": cita_id},
        )

    cita.motivo = motivo.strip()
    cita.motivo_registrado_en = ahora_utc()
    cita.motivo_registrado_por = usuario

    # Clinical writes are audited even though no state changed: the regulation
    # cares about who touched clinical data, not about the state machine.
    _auditar(
        session,
        cita,
        estado_anterior=cita.estado,
        estado_nuevo=cita.estado,
        usuario=usuario,
        motivo="Reason for consultation recorded (clinical data)",
    )
    session.flush()
    return cita
