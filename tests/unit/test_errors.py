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

CLASES_CONCRETAS = [
    obj
    for _, obj in inspect.getmembers(mod, inspect.isclass)
    if issubclass(obj, DomainError) and obj not in {DomainError, NotFound}
]


class TestFormaDelError:
    def test_lo_minimo_es_codigo_y_mensaje(self) -> None:
        payload = PatientNotFound("No existe el paciente 42").to_dict()
        assert payload == {
            "error": True,
            "code": "PATIENT_NOT_FOUND",
            "message": "No existe el paciente 42",
        }

    def test_incluye_sugerencia_y_detalles_cuando_los_hay(self) -> None:
        error = SlotUnavailable(
            "El cupo ya fue tomado.",
            suggestion="Los más cercanos son 09:00, 09:30 y 11:00.",
            details={"slot_id": 88, "alternativas": [12, 13]},
        )
        payload = error.to_dict()
        assert payload["suggestion"].startswith("Los más cercanos")
        assert payload["details"]["slot_id"] == 88

    def test_las_claves_opcionales_se_omiten_no_se_ponen_en_null(self) -> None:
        payload = PatientNotFound("x").to_dict()
        assert "suggestion" not in payload
        assert "details" not in payload

    def test_el_codigo_se_puede_forzar_en_construccion(self) -> None:
        error = InvalidTransition("x", code=ErrorCode.APPOINTMENT_IN_FINAL_STATE)
        assert error.to_dict()["code"] == "APPOINTMENT_IN_FINAL_STATE"

    def test_sigue_siendo_una_excepcion_normal(self) -> None:
        with pytest.raises(DomainError, match="algo pasó"):
            raise PatientNotFound("algo pasó")


class TestContratoDeLaJerarquia:
    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_toda_clase_fija_un_codigo_propio(self, clase: type[DomainError]) -> None:
        assert clase.code is not DomainError.code or clase is DomainError

    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_todo_error_serializa_sin_reventar(self, clase: type[DomainError]) -> None:
        payload = clase("mensaje de prueba").to_dict()
        assert payload["error"] is True
        assert payload["message"] == "mensaje de prueba"
        assert payload["code"] in set(ErrorCode)

    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_el_status_http_es_un_codigo_de_error_del_cliente(
        self, clase: type[DomainError]
    ) -> None:
        # Every modelled failure is the caller's to fix. A domain error that
        # mapped to 5xx would mean the server broke, which is a different thing.
        assert 400 <= clase.http_status < 500

    def test_los_no_encontrados_son_404(self) -> None:
        assert PatientNotFound.http_status == 404

    def test_los_conflictos_son_409(self) -> None:
        assert SlotUnavailable.http_status == 409
        assert ConcurrencyConflict.http_status == 409
        assert InvalidTransition.http_status == 409

    def test_no_hay_codigos_duplicados_entre_clases(self) -> None:
        vistos: dict[ErrorCode, str] = {}
        for clase in CLASES_CONCRETAS:
            previous = vistos.get(clase.code)
            assert previous is None, f"{clase.__name__} repite el código de {previous}"
            vistos[clase.code] = clase.__name__


class TestEspacioDeCodigos:
    def test_los_codigos_son_str_estables(self) -> None:
        # They are part of the tool contract: renaming one breaks callers.
        for code in ErrorCode:
            assert str(code) == code.value == code.name

    def test_el_espacio_de_seguridad_ya_esta_declarado(self) -> None:
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
