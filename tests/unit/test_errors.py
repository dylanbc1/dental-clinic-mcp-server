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
    CodigoError,
    ConflictoConcurrencia,
    ErrorDominio,
    NoEncontrado,
    PacienteNoEncontrado,
    SlotNoDisponible,
    TransicionInvalida,
)

CLASES_CONCRETAS = [
    obj
    for _, obj in inspect.getmembers(mod, inspect.isclass)
    if issubclass(obj, ErrorDominio) and obj not in {ErrorDominio, NoEncontrado}
]


class TestFormaDelError:
    def test_lo_minimo_es_codigo_y_mensaje(self) -> None:
        payload = PacienteNoEncontrado("No existe el paciente 42").to_dict()
        assert payload == {
            "error": True,
            "codigo": "PACIENTE_NO_ENCONTRADO",
            "mensaje": "No existe el paciente 42",
        }

    def test_incluye_sugerencia_y_detalles_cuando_los_hay(self) -> None:
        error = SlotNoDisponible(
            "El cupo ya fue tomado.",
            sugerencia="Los más cercanos son 09:00, 09:30 y 11:00.",
            detalles={"slot_id": 88, "alternativas": [12, 13]},
        )
        payload = error.to_dict()
        assert payload["sugerencia"].startswith("Los más cercanos")
        assert payload["detalles"]["slot_id"] == 88

    def test_las_claves_opcionales_se_omiten_no_se_ponen_en_null(self) -> None:
        payload = PacienteNoEncontrado("x").to_dict()
        assert "sugerencia" not in payload
        assert "detalles" not in payload

    def test_el_codigo_se_puede_forzar_en_construccion(self) -> None:
        error = TransicionInvalida("x", codigo=CodigoError.CITA_EN_ESTADO_FINAL)
        assert error.to_dict()["codigo"] == "CITA_EN_ESTADO_FINAL"

    def test_sigue_siendo_una_excepcion_normal(self) -> None:
        with pytest.raises(ErrorDominio, match="algo pasó"):
            raise PacienteNoEncontrado("algo pasó")


class TestContratoDeLaJerarquia:
    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_toda_clase_fija_un_codigo_propio(self, clase: type[ErrorDominio]) -> None:
        assert clase.codigo is not ErrorDominio.codigo or clase is ErrorDominio

    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_todo_error_serializa_sin_reventar(self, clase: type[ErrorDominio]) -> None:
        payload = clase("mensaje de prueba").to_dict()
        assert payload["error"] is True
        assert payload["mensaje"] == "mensaje de prueba"
        assert payload["codigo"] in set(CodigoError)

    @pytest.mark.parametrize("clase", CLASES_CONCRETAS, ids=lambda c: c.__name__)
    def test_el_status_http_es_un_codigo_de_error_del_cliente(
        self, clase: type[ErrorDominio]
    ) -> None:
        # Every modelled failure is the caller's to fix. A domain error that
        # mapped to 5xx would mean the server broke, which is a different thing.
        assert 400 <= clase.http_status < 500

    def test_los_no_encontrados_son_404(self) -> None:
        assert PacienteNoEncontrado.http_status == 404

    def test_los_conflictos_son_409(self) -> None:
        assert SlotNoDisponible.http_status == 409
        assert ConflictoConcurrencia.http_status == 409
        assert TransicionInvalida.http_status == 409

    def test_no_hay_codigos_duplicados_entre_clases(self) -> None:
        vistos: dict[CodigoError, str] = {}
        for clase in CLASES_CONCRETAS:
            anterior = vistos.get(clase.codigo)
            assert anterior is None, f"{clase.__name__} repite el código de {anterior}"
            vistos[clase.codigo] = clase.__name__


class TestEspacioDeCodigos:
    def test_los_codigos_son_str_estables(self) -> None:
        # They are part of the tool contract: renaming one breaks callers.
        for codigo in CodigoError:
            assert str(codigo) == codigo.value == codigo.name

    def test_el_espacio_de_seguridad_ya_esta_declarado(self) -> None:
        """Declared in M2 even though layers 3-5 land later, so the code space
        is defined in one place instead of growing ad hoc."""
        for esperado in (
            "NO_AUTENTICADO",
            "SCOPE_INSUFICIENTE",
            "APROBACION_REQUERIDA",
            "APROBACION_EXPIRADA",
            "APROBACION_YA_USADA",
            "CONSENTIMIENTO_REQUERIDO",
            "ORIGEN_NO_PERMITIDO",
            "RATE_LIMIT_EXCEDIDO",
        ):
            assert esperado in set(CodigoError)
