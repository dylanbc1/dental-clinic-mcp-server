"""Domain vocabulary.

Spanish on purpose. These are the words Colombian clinics and IPS use, they
appear verbatim in the tool schemas, and translating them would make the model
reason about a domain nobody in the sector describes that way. Docs and comments
are English; the domain is not.
"""

from __future__ import annotations

from enum import StrEnum


class EstadoCita(StrEnum):
    """The appointment state machine of §2.1, the sector standard."""

    AGENDADA = "agendada"
    CONFIRMADA = "confirmada"
    EN_ESPERA = "en_espera"
    ATENDIDA = "atendida"
    CANCELADA = "cancelada"
    REPROGRAMADA = "reprogramada"
    NO_ASISTIO = "no_asistio"


class EstadoSlot(StrEnum):
    LIBRE = "libre"
    OCUPADO = "ocupado"
    BLOQUEADO = "bloqueado"


class Regimen(StrEnum):
    """Colombian health-system affiliation regimes."""

    CONTRIBUTIVO = "contributivo"
    SUBSIDIADO = "subsidiado"
    PARTICULAR = "particular"
    SOAT = "soat"


class TipoDocumento(StrEnum):
    CC = "CC"  # cedula de ciudadania
    TI = "TI"  # tarjeta de identidad (minors)
    CE = "CE"  # cedula de extranjeria
    PA = "PA"  # pasaporte
    RC = "RC"  # registro civil
    PPT = "PPT"  # permiso por proteccion temporal


class Especialidad(StrEnum):
    ODONTOLOGIA_GENERAL = "odontologia_general"
    ORTODONCIA = "ortodoncia"
    ENDODONCIA = "endodoncia"
    PERIODONCIA = "periodoncia"
    ODONTOPEDIATRIA = "odontopediatria"


class ConceptoCargo(StrEnum):
    COPAGO = "copago"
    CUOTA_MODERADORA = "cuota_moderadora"
    PARTICULAR = "particular"
    NO_SHOW = "no_show"


class EstadoCargo(StrEnum):
    PENDIENTE = "pendiente"
    PAGADO = "pagado"
    ANULADO = "anulado"


class EstadoCartera(StrEnum):
    AL_DIA = "al_dia"
    EN_MORA = "en_mora"


class PrioridadListaEspera(StrEnum):
    URGENCIA = "urgencia"
    ANTIGUEDAD = "antiguedad"


class EstadoListaEspera(StrEnum):
    ACTIVA = "activa"
    OFRECIDA = "ofrecida"
    ACEPTADA = "aceptada"
    RETIRADA = "retirada"


#: Terminal states: no transition leaves them.
ESTADOS_FINALES: frozenset[EstadoCita] = frozenset(
    {
        EstadoCita.ATENDIDA,
        EstadoCita.CANCELADA,
        EstadoCita.REPROGRAMADA,
        EstadoCita.NO_ASISTIO,
    }
)

#: States in which an appointment still holds its slot. Drives the partial
#: unique index that makes double-booking impossible in the database.
ESTADOS_QUE_OCUPAN_SLOT: frozenset[EstadoCita] = frozenset(
    {
        EstadoCita.AGENDADA,
        EstadoCita.CONFIRMADA,
        EstadoCita.EN_ESPERA,
        EstadoCita.ATENDIDA,
    }
)
