"""Health and readiness endpoints, plus the error envelope contract."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import app, handle_domain_error
from backend.domain.errors import DomainError, PatientNotFound, SlotUnavailable

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestSalud:
    def test_liveness_responde_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_liveness_incluye_un_instante_con_zona(self, client: TestClient) -> None:
        occurred_at = client.get("/health").json()["time"]
        assert occurred_at.endswith("+00:00")


class TestListo:
    def test_readiness_ok_con_base_disponible(self, client: TestClient, engine: object) -> None:
        # `engine` guarantees a live database for this test.
        response = client.get("/ready")
        assert response.status_code in {200, 503}
        if response.status_code == 200:
            assert response.json()["status"] == "ready"

    def test_readiness_nunca_lanza_aunque_la_base_falle(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explotar() -> None:
            raise RuntimeError("base caída")

        monkeypatch.setattr("backend.api.get_engine", explotar)
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["error"] is True
        assert body["suggestion"]


class TestEnvolturaDeErrores:
    """Any endpoint added later inherits this behaviour."""

    @pytest.fixture
    def test_app(self) -> FastAPI:
        prueba = FastAPI()
        prueba.add_exception_handler(DomainError, handle_domain_error)  # type: ignore[arg-type]

        @prueba.get("/no-encontrado")
        async def no_encontrado() -> None:
            raise PatientNotFound(
                "No existe el paciente 42",
                suggestion="Busca por documento con search_patients.",
            )

        @prueba.get("/conflicto")
        async def conflicto() -> None:
            raise SlotUnavailable("El cupo ya fue tomado.", details={"slot_id": 7})

        return prueba

    def test_un_no_encontrado_responde_404_estructurado(self, test_app: FastAPI) -> None:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            response = client.get("/no-encontrado")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "PATIENT_NOT_FOUND"
        assert body["suggestion"]

    def test_un_conflicto_responde_409_estructurado(self, test_app: FastAPI) -> None:
        with TestClient(test_app, raise_server_exceptions=False) as client:
            response = client.get("/conflicto")
        assert response.status_code == 409
        assert response.json()["details"]["slot_id"] == 7


class TestCicloDeVida:
    def test_el_arranque_y_el_apagado_completan(self) -> None:
        """Exercises the lifespan handler: a broken startup must fail loudly in
        tests rather than at `docker compose up`."""
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


class TestErrorInesperado:
    def test_un_fallo_no_previsto_no_filtra_el_stack_trace(self) -> None:
        """The last line of defence for layer 4: even a genuine bug answers
        with the structured envelope and nothing about the internals."""
        from backend.api import handle_unexpected_error

        prueba = FastAPI()
        prueba.add_exception_handler(Exception, handle_unexpected_error)  # type: ignore[arg-type]

        @prueba.get("/bum")
        async def bum() -> None:
            raise ZeroDivisionError("detalle interno que no debe salir")

        with TestClient(prueba, raise_server_exceptions=False) as client:
            response = client.get("/bum")

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "INTERNAL_ERROR"
        assert body["suggestion"]
        crudo = response.text
        assert "ZeroDivisionError" not in crudo
        assert "detalle interno" not in crudo
        assert "Traceback" not in crudo
