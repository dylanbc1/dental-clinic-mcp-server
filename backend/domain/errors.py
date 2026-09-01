"""Structured domain errors, the foundation of security layer 4.

The rule: **never a mute 500**. Every failure carries a stable code, a readable
message, and an actionable next step. A model told "slot not available" retries
blindly; one told "the three closest free slots are 09:00, 09:30, 11:00"
recovers on its own turn.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable error codes, part of the tool contract. Renaming one is a
    breaking change, so they stay generic and domain-shaped."""

    # --- lookup ------------------------------------------------------------
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    APPOINTMENT_NOT_FOUND = "APPOINTMENT_NOT_FOUND"
    SLOT_NOT_FOUND = "SLOT_NOT_FOUND"
    PROFESSIONAL_NOT_FOUND = "PROFESSIONAL_NOT_FOUND"

    # --- scheduling --------------------------------------------------------
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    SLOT_OUTSIDE_HOURS = "SLOT_OUTSIDE_HOURS"
    SLOT_IN_THE_PAST = "SLOT_IN_THE_PAST"
    SPECIALTY_MISMATCH = "SPECIALTY_MISMATCH"
    PATIENT_ALREADY_BOOKED = "PATIENT_ALREADY_BOOKED"

    # --- state machine -----------------------------------------------------
    INVALID_TRANSITION = "INVALID_TRANSITION"
    REASON_REQUIRED = "REASON_REQUIRED"
    APPOINTMENT_IN_FINAL_STATE = "APPOINTMENT_IN_FINAL_STATE"

    # --- waiting list ------------------------------------------------------
    WAITING_LIST_EMPTY = "WAITING_LIST_EMPTY"
    ALREADY_ON_WAITING_LIST = "ALREADY_ON_WAITING_LIST"

    # --- accounts receivable / affiliation --------------------------------
    AFFILIATION_INACTIVE = "AFFILIATION_INACTIVE"
    CARTERA_OVERDUE = "CARTERA_OVERDUE"

    # --- validation & concurrency -----------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    CONCURRENCY_CONFLICT = "CONCURRENCY_CONFLICT"

    # --- security (populated in M4/M5, declared here so the code space is
    #     defined in one place) ---------------------------------------------
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_USED = "APPROVAL_ALREADY_USED"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    ORIGIN_NOT_ALLOWED = "ORIGIN_NOT_ALLOWED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    NOT_APPROVED = "NOT_APPROVED"
    CLIENT_CANNOT_CONFIRM = "CLIENT_CANNOT_CONFIRM"

    # --- the MCP layer and the transport -----------------------------------
    #: Raised above the domain, but the code space is one space. These lived as
    #: bare strings at their raise sites until a decline surfaced one of them
    #: still written in Spanish; a code nobody can enumerate is a code nobody
    #: notices. `tests/unit/test_errors.py` now fails if a new one appears.
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    UNKNOWN_SCOPE = "UNKNOWN_SCOPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class DomainError(Exception):
    """Base class for every expected failure.

    Unexpected failures are not wrapped here. They are logged and surfaced as a
    single generic code, so a bug never leaks a stack trace to the model.
    """

    code: ErrorCode = ErrorCode.INVALID_INPUT
    http_status: int = 400

    def __init__(
        self,
        message: str,
        *,
        suggestion: str | None = None,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        """Wire format. Identical for the REST API and the MCP tool layer."""
        payload: dict[str, Any] = {
            "error": True,
            "code": str(self.code),
            "message": self.message,
        }
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code}: {self.message})"


# --------------------------------------------------------------------------
# Each class fixes its code and HTTP status, so a call site cannot mislabel a
# failure by accident.
# --------------------------------------------------------------------------


class NotFound(DomainError):
    http_status = 404


class PatientNotFound(NotFound):
    code = ErrorCode.PATIENT_NOT_FOUND


class AppointmentNotFound(NotFound):
    code = ErrorCode.APPOINTMENT_NOT_FOUND


class SlotNotFound(NotFound):
    code = ErrorCode.SLOT_NOT_FOUND


class ProfessionalNotFound(NotFound):
    code = ErrorCode.PROFESSIONAL_NOT_FOUND


class SlotUnavailable(DomainError):
    code = ErrorCode.SLOT_UNAVAILABLE
    http_status = 409


class SlotOutsideHours(DomainError):
    code = ErrorCode.SLOT_OUTSIDE_HOURS


class SlotInThePast(DomainError):
    code = ErrorCode.SLOT_IN_THE_PAST


class SpecialtyMismatch(DomainError):
    code = ErrorCode.SPECIALTY_MISMATCH


class PatientAlreadyBooked(DomainError):
    code = ErrorCode.PATIENT_ALREADY_BOOKED
    http_status = 409


class InvalidTransition(DomainError):
    code = ErrorCode.INVALID_TRANSITION
    http_status = 409


class ReasonRequired(DomainError):
    code = ErrorCode.REASON_REQUIRED


class WaitingListEmpty(NotFound):
    code = ErrorCode.WAITING_LIST_EMPTY


class AlreadyOnWaitingList(DomainError):
    code = ErrorCode.ALREADY_ON_WAITING_LIST
    http_status = 409


class AffiliationInactive(DomainError):
    code = ErrorCode.AFFILIATION_INACTIVE


class ConsentRequired(DomainError):
    """Clinical write attempted without recorded consent.

    403 rather than 400: the request is well-formed, the caller just is not
    permitted to perform it on this patient (Res. 2654/2019).
    """

    code = ErrorCode.CONSENT_REQUIRED
    http_status = 403


class ConcurrencyConflict(DomainError):
    code = ErrorCode.CONCURRENCY_CONFLICT
    http_status = 409
