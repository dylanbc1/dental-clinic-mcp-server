"""Structured error tests (security layer 4).

The contract these tests defend: a caller, human or model, always receives a
stable code, a message and, wherever the domain can offer one, a next step.
Never a mute 500, never a leaked stack trace.
"""

from __future__ import annotations

import inspect

import pytest

from backend.domain import errors as mod
from backend.domain.errors import (
    ConcurrencyConflict,
    DomainError,
    ErrorCode,
    InvalidTransition,
    NotFound,
    PatientNotFound,
    SlotUnavailable,
)

CONCRETE_CLASSES = [
    obj
    for _, obj in inspect.getmembers(mod, inspect.isclass)
    if issubclass(obj, DomainError) and obj not in {DomainError, NotFound}
]


class TestErrorShape:
    def test_the_minimum_is_a_code_and_a_message(self) -> None:
        payload = PatientNotFound("No existe el paciente 42").to_dict()
        assert payload == {
            "error": True,
            "code": "PATIENT_NOT_FOUND",
            "message": "No existe el paciente 42",
        }

    def test_includes_suggestion_and_details_when_present(self) -> None:
        error = SlotUnavailable(
            "El cupo ya fue tomado.",
            suggestion="Los más cercanos son 09:00, 09:30 y 11:00.",
            details={"slot_id": 88, "alternatives": [12, 13]},
        )
        payload = error.to_dict()
        assert payload["suggestion"].startswith("Los más cercanos")
        assert payload["details"]["slot_id"] == 88

    def test_optional_keys_are_omitted_not_set_to_null(self) -> None:
        payload = PatientNotFound("x").to_dict()
        assert "suggestion" not in payload
        assert "details" not in payload

    def test_the_code_can_be_forced_at_construction(self) -> None:
        error = InvalidTransition("x", code=ErrorCode.APPOINTMENT_IN_FINAL_STATE)
        assert error.to_dict()["code"] == "APPOINTMENT_IN_FINAL_STATE"

    def test_it_is_still_an_ordinary_exception(self) -> None:
        with pytest.raises(DomainError, match="algo pasó"):
            raise PatientNotFound("algo pasó")


class TestHierarchyContract:
    @pytest.mark.parametrize("klass", CONCRETE_CLASSES, ids=lambda c: c.__name__)
    def test_every_class_pins_its_own_code(self, klass: type[DomainError]) -> None:
        assert klass.code is not DomainError.code or klass is DomainError

    @pytest.mark.parametrize("klass", CONCRETE_CLASSES, ids=lambda c: c.__name__)
    def test_every_error_serialises_without_blowing_up(self, klass: type[DomainError]) -> None:
        payload = klass("mensaje de prueba").to_dict()
        assert payload["error"] is True
        assert payload["message"] == "mensaje de prueba"
        assert payload["code"] in set(ErrorCode)

    @pytest.mark.parametrize("klass", CONCRETE_CLASSES, ids=lambda c: c.__name__)
    def test_the_http_status_is_a_client_error_code(self, klass: type[DomainError]) -> None:
        # Every modelled failure is the caller's to fix. A domain error that
        # mapped to 5xx would mean the server broke, which is a different thing.
        assert 400 <= klass.http_status < 500

    def test_not_found_is_404(self) -> None:
        assert PatientNotFound.http_status == 404

    def test_conflicts_are_409(self) -> None:
        assert SlotUnavailable.http_status == 409
        assert ConcurrencyConflict.http_status == 409
        assert InvalidTransition.http_status == 409

    def test_there_are_no_duplicate_codes_across_classes(self) -> None:
        seen: dict[ErrorCode, str] = {}
        for klass in CONCRETE_CLASSES:
            previous = seen.get(klass.code)
            assert previous is None, f"{klass.__name__} repeats the code of {previous}"
            seen[klass.code] = klass.__name__


class TestCodeSpace:
    def test_the_codes_are_stable_strings(self) -> None:
        # They are part of the tool contract: renaming one breaks callers.
        for code in ErrorCode:
            assert str(code) == code.value == code.name

    def test_the_security_scheme_is_already_declared(self) -> None:
        """Declared in M2 even though layers 3-5 land later, so the code space
        is defined in one place instead of growing ad hoc."""
        for expected in (
            "NOT_AUTHENTICATED",
            "INSUFFICIENT_SCOPE",
            "APPROVAL_REQUIRED",
            "APPROVAL_EXPIRED",
            "APPROVAL_ALREADY_USED",
            "CONSENT_REQUIRED",
            "ORIGIN_NOT_ALLOWED",
            "RATE_LIMIT_EXCEEDED",
        ):
            assert expected in set(ErrorCode)
