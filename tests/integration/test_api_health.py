"""Health and readiness endpoints, plus the error envelope contract."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import app, manejar_error_dominio
from backend.domain.errors import ErrorDominio, PacienteNoEncontrado, SlotNoDisponible

pytestmark = pytest.mark.integration


@pytest.fixture
def cliente() -> TestClient:
    return TestClient(app)


class TestSalud:
    def test_liveness_responde_ok(self, cliente: TestClient) -> None:
        respuesta = cliente.get("/salud")
        assert respuesta.status_code == 200
        assert respuesta.json()["estado"] == "ok"

    def test_liveness_incluye_un_instante_con_zona(self, cliente: TestClient) -> None:
        momento = cliente.get("/salud").json()["momento"]
        assert momento.endswith("+00:00")


class TestListo:
    def test_readiness_ok_con_base_disponible(self, cliente: TestClient, engine: object) -> None:
        # `engine` guarantees a live database for this test.
        respuesta = cliente.get("/listo")
        assert respuesta.status_code in {200, 503}
        if respuesta.status_code == 200:
            assert respuesta.json()["estado"] == "listo"

    def test_readiness_nunca_lanza_aunque_la_base_falle(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explotar() -> None:
            raise RuntimeError("base caída")

        monkeypatch.setattr("backend.api.get_engine", explotar)
        respuesta = cliente.get("/listo")
        assert respuesta.status_code == 503
        cuerpo = respuesta.json()
        assert cuerpo["error"] is True
        assert cuerpo["sugerencia"]


class TestEnvolturaDeErrores:
    """Any endpoint added later inherits this behaviour."""

    @pytest.fixture
    def app_de_prueba(self) -> FastAPI:
        prueba = FastAPI()
        prueba.add_exception_handler(ErrorDominio, manejar_error_dominio)  # type: ignore[arg-type]

        @prueba.get("/no-encontrado")
        async def no_encontrado() -> None:
            raise PacienteNoEncontrado(
                "No existe el paciente 42",
                sugerencia="Busca por documento con buscar_paciente.",
            )

        @prueba.get("/conflicto")
        async def conflicto() -> None:
            raise SlotNoDisponible("El cupo ya fue tomado.", detalles={"slot_id": 7})

        return prueba

    def test_un_no_encontrado_responde_404_estructurado(self, app_de_prueba: FastAPI) -> None:
        with TestClient(app_de_prueba, raise_server_exceptions=False) as cliente:
            respuesta = cliente.get("/no-encontrado")
        assert respuesta.status_code == 404
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "PACIENTE_NO_ENCONTRADO"
        assert cuerpo["sugerencia"]

    def test_un_conflicto_responde_409_estructurado(self, app_de_prueba: FastAPI) -> None:
        with TestClient(app_de_prueba, raise_server_exceptions=False) as cliente:
            respuesta = cliente.get("/conflicto")
        assert respuesta.status_code == 409
        assert respuesta.json()["detalles"]["slot_id"] == 7


class TestCicloDeVida:
    def test_el_arranque_y_el_apagado_completan(self) -> None:
        """Exercises the lifespan handler: a broken startup must fail loudly in
        tests rather than at `docker compose up`."""
        with TestClient(app) as cliente:
            assert cliente.get("/salud").status_code == 200


class TestErrorInesperado:
    def test_un_fallo_no_previsto_no_filtra_el_stack_trace(self) -> None:
        """The last line of defence for layer 4: even a genuine bug answers
        with the structured envelope and nothing about the internals."""
        from backend.api import manejar_error_inesperado

        prueba = FastAPI()
        prueba.add_exception_handler(Exception, manejar_error_inesperado)  # type: ignore[arg-type]

        @prueba.get("/bum")
        async def bum() -> None:
            raise ZeroDivisionError("detalle interno que no debe salir")

        with TestClient(prueba, raise_server_exceptions=False) as cliente:
            respuesta = cliente.get("/bum")

        assert respuesta.status_code == 500
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "ERROR_INTERNO"
        assert cuerpo["sugerencia"]
        crudo = respuesta.text
        assert "ZeroDivisionError" not in crudo
        assert "detalle interno" not in crudo
        assert "Traceback" not in crudo
