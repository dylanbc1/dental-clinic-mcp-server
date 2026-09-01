"""Structured domain errors, the foundation of security layer 4.

The rule: **never a mute 500**. Every failure carries a stable code, a readable
message, and an actionable next step. A model told "slot not available" retries
blindly; one told "the three closest free slots are 09:00, 09:30, 11:00"
recovers on its own turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class CodigoError(StrEnum):
    """Stable error codes, part of the tool contract. Renaming one is a
    breaking change, so they stay generic and domain-shaped."""

    # --- lookup ------------------------------------------------------------
    PACIENTE_NO_ENCONTRADO = "PACIENTE_NO_ENCONTRADO"
    CITA_NO_ENCONTRADA = "CITA_NO_ENCONTRADA"
    SLOT_NO_ENCONTRADO = "SLOT_NO_ENCONTRADO"
    PROFESIONAL_NO_ENCONTRADO = "PROFESIONAL_NO_ENCONTRADO"

    # --- scheduling --------------------------------------------------------
    SLOT_NO_DISPONIBLE = "SLOT_NO_DISPONIBLE"
    SLOT_FUERA_DE_HORARIO = "SLOT_FUERA_DE_HORARIO"
    SLOT_EN_EL_PASADO = "SLOT_EN_EL_PASADO"
    ESPECIALIDAD_NO_COINCIDE = "ESPECIALIDAD_NO_COINCIDE"
    PACIENTE_YA_TIENE_CITA = "PACIENTE_YA_TIENE_CITA"

    # --- state machine -----------------------------------------------------
    TRANSICION_INVALIDA = "TRANSICION_INVALIDA"
    MOTIVO_REQUERIDO = "MOTIVO_REQUERIDO"
    CITA_EN_ESTADO_FINAL = "CITA_EN_ESTADO_FINAL"

    # --- waiting list ------------------------------------------------------
    LISTA_ESPERA_VACIA = "LISTA_ESPERA_VACIA"
    YA_EN_LISTA_ESPERA = "YA_EN_LISTA_ESPERA"

    # --- accounts receivable / affiliation --------------------------------
    AFILIACION_INACTIVA = "AFILIACION_INACTIVA"
    CARTERA_EN_MORA = "CARTERA_EN_MORA"

    # --- validation & concurrency -----------------------------------------
    ENTRADA_INVALIDA = "ENTRADA_INVALIDA"
    CONFLICTO_CONCURRENCIA = "CONFLICTO_CONCURRENCIA"

    # --- security (populated in M4/M5, declared here so the code space is
    #     defined in one place) ---------------------------------------------
    NO_AUTENTICADO = "NO_AUTENTICADO"
    SCOPE_INSUFICIENTE = "SCOPE_INSUFICIENTE"
    APROBACION_REQUERIDA = "APROBACION_REQUERIDA"
    APROBACION_INVALIDA = "APROBACION_INVALIDA"
    APROBACION_EXPIRADA = "APROBACION_EXPIRADA"
    APROBACION_YA_USADA = "APROBACION_YA_USADA"
    CONSENTIMIENTO_REQUERIDO = "CONSENTIMIENTO_REQUERIDO"
    ORIGEN_NO_PERMITIDO = "ORIGEN_NO_PERMITIDO"
    RATE_LIMIT_EXCEDIDO = "RATE_LIMIT_EXCEDIDO"


class ErrorDominio(Exception):
    """Base class for every expected failure.

    Unexpected failures are not wrapped here. They are logged and surfaced as a
    single generic code, so a bug never leaks a stack trace to the model.
    """

    codigo: CodigoError = CodigoError.ENTRADA_INVALIDA
    http_status: int = 400

    def __init__(
        self,
        mensaje: str,
        *,
        sugerencia: str | None = None,
        detalles: dict[str, Any] | None = None,
        codigo: CodigoError | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.sugerencia = sugerencia
        self.detalles = detalles or {}
        if codigo is not None:
            self.codigo = codigo

    def to_dict(self) -> dict[str, Any]:
        """Wire format. Identical for the REST API and the MCP tool layer."""
        payload: dict[str, Any] = {
            "error": True,
            "codigo": str(self.codigo),
            "mensaje": self.mensaje,
        }
        if self.sugerencia:
            payload["sugerencia"] = self.sugerencia
        if self.detalles:
            payload["detalles"] = self.detalles
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.codigo}: {self.mensaje})"


# --------------------------------------------------------------------------
# Each class fixes its code and HTTP status, so a call site cannot mislabel a
# failure by accident.
# --------------------------------------------------------------------------


class NoEncontrado(ErrorDominio):
    http_status = 404


class PacienteNoEncontrado(NoEncontrado):
    codigo = CodigoError.PACIENTE_NO_ENCONTRADO


class CitaNoEncontrada(NoEncontrado):
    codigo = CodigoError.CITA_NO_ENCONTRADA


class SlotNoEncontrado(NoEncontrado):
    codigo = CodigoError.SLOT_NO_ENCONTRADO


class ProfesionalNoEncontrado(NoEncontrado):
    codigo = CodigoError.PROFESIONAL_NO_ENCONTRADO


class SlotNoDisponible(ErrorDominio):
    codigo = CodigoError.SLOT_NO_DISPONIBLE
    http_status = 409


class SlotFueraDeHorario(ErrorDominio):
    codigo = CodigoError.SLOT_FUERA_DE_HORARIO


class SlotEnElPasado(ErrorDominio):
    codigo = CodigoError.SLOT_EN_EL_PASADO


class EspecialidadNoCoincide(ErrorDominio):
    codigo = CodigoError.ESPECIALIDAD_NO_COINCIDE


class PacienteYaTieneCita(ErrorDominio):
    codigo = CodigoError.PACIENTE_YA_TIENE_CITA
    http_status = 409


class TransicionInvalida(ErrorDominio):
    codigo = CodigoError.TRANSICION_INVALIDA
    http_status = 409


class MotivoRequerido(ErrorDominio):
    codigo = CodigoError.MOTIVO_REQUERIDO


class ListaEsperaVacia(NoEncontrado):
    codigo = CodigoError.LISTA_ESPERA_VACIA


class YaEnListaEspera(ErrorDominio):
    codigo = CodigoError.YA_EN_LISTA_ESPERA
    http_status = 409


class AfiliacionInactiva(ErrorDominio):
    codigo = CodigoError.AFILIACION_INACTIVA


class ConsentimientoRequerido(ErrorDominio):
    """Clinical write attempted without recorded consent.

    403 rather than 400: the request is well-formed, the caller just is not
    permitted to perform it on this patient (Res. 2654/2019).
    """

    codigo = CodigoError.CONSENTIMIENTO_REQUERIDO
    http_status = 403


class ConflictoConcurrencia(ErrorDominio):
    codigo = CodigoError.CONFLICTO_CONCURRENCIA
    http_status = 409
