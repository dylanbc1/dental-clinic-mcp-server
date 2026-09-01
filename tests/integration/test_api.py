"""REST API tests.

The API is the seam the MCP tools call, so its contract matters as much as the
domain behind it: status codes, the error envelope, and the shape of every
payload the model will read back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.api import app
from backend.database import get_session
from backend.domain.services import book_appointment, join_waiting_list
from backend.enums import Specialty
from tests.conftest import Scenario

pytestmark = pytest.mark.integration

ACTOR = "api-test@clinica.test"
HEADERS = {"X-Actor": ACTOR}


@pytest.fixture
def api_session(sessions: Callable[[], Session]) -> Session:
    return sessions()


@pytest.fixture
def client(api_session: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield api_session
        api_session.commit()

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def appointment_id(api_session: Session, scenario: Scenario) -> int:
    result = book_appointment(
        api_session,
        patient_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        user="setup",
    )
    api_session.commit()
    return result.appointment.id


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


class TestClinica:
    def test_devuelve_la_clinica_con_sus_profesionales(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get("/clinic").json()
        assert body["name"] == "Clínica Escenario"
        assert body["timezone_name"] == "America/Bogota"
        assert {p["specialty"] for p in body["professionals"]} == {
            "general_dentistry",
            "orthodontics",
        }


class TestPoliticas:
    def test_expone_las_reglas_de_cartera(self, client: TestClient) -> None:
        body = client.get("/policies/cartera").json()
        assert body["charges_no_show"] is True
        assert body["payment_term_days"] == 30
        assert "general_dentistry" in body["particular_tariffs"]

    def test_deja_explicita_la_regla_de_no_bloqueo(self, client: TestClient) -> None:
        assert "never a block" in client.get("/policies/cartera").json()["note"]


class TestBuscarPacientes:
    def test_por_documento(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/patients", params={"document_number": "11111111"}).json()
        assert [p["id"] for p in body] == [scenario.ana_id]

    def test_por_nombre(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/patients", params={"name": "bruno"}).json()
        assert body[0]["regimen"] == "subsidiado"

    def test_sin_criterio_responde_404_estructurado(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.get("/patients")
        assert response.status_code == 404
        assert response.json()["code"] == "PATIENT_NOT_FOUND"

    def test_un_documento_demasiado_largo_es_422(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        assert client.get("/patients", params={"document_number": "9" * 40}).status_code == 422

    def test_no_devuelve_datos_clinicos(self, client: TestClient, scenario: Scenario) -> None:
        """The read tool must not leak clinical fields into a lookup response."""
        body = client.get("/patients", params={"document_number": "11111111"}).json()[0]
        assert "reason" not in body
        assert "clinical_data_consent" not in body


class TestAfiliacion:
    def test_activa(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/afiliacion").json()
        assert body["active"] is True
        assert body["blocks_booking"] is False

    def test_inactiva_cae_a_particular(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.bruno_id}/afiliacion").json()
        assert body["regimen"] == "subsidiado"
        assert body["effective_regimen"] == "particular"
        assert body["suggestion"]

    def test_paciente_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/patients/999999/afiliacion").status_code == 404


class TestCartera:
    def test_al_dia(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/cartera").json()
        assert body["status"] == "al_dia"
        assert body["charges"] == []

    def test_en_mora_con_detalle(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.deudor_id}/cartera").json()
        assert body["status"] == "en_mora"
        assert body["overdue_total"] == "180000"
        assert body["above_alert_threshold"] is True
        assert len(body["charges"]) == 1
        assert body["ageing"]["61_90"] == "180000"


class TestDisponibilidad:
    def test_devuelve_cupos_futuros(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/availability").json()
        assert body
        assert all("start_local" in s for s in body)

    def test_expone_hora_local_y_utc(self, client: TestClient, scenario: Scenario) -> None:
        """The model reasons in local time; the system stores UTC. Both ship."""
        slot = client.get("/availability").json()[0]
        assert slot["start_utc"].endswith("Z") or "+00:00" in slot["start_utc"]
        assert slot["start_local"].startswith(str(scenario.fecha_futura))

    def test_filtra_por_especialidad(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/availability", params={"specialty": "orthodontics"}).json()
        assert all(s["specialty"] == "orthodontics" for s in body)

    def test_especialidad_invalida_es_422(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/availability", params={"specialty": "astrologia"}).status_code == 422

    def test_profesional_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/availability", params={"professional_id": 999999}).status_code == 404


class TestCitas:
    def test_detalle_incluye_historial(self, client: TestClient, appointment_id: int) -> None:
        body = client.get(f"/appointments/{appointment_id}").json()
        assert body["status"] == "scheduled"
        assert len(body["history"]) == 1

    def test_cita_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get("/appointments/999999")
        assert response.status_code == 404
        assert response.json()["code"] == "APPOINTMENT_NOT_FOUND"

    def test_listado_por_paciente(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/appointments").json()
        assert [c["id"] for c in body] == [appointment_id]

    def test_listado_filtra_por_rango(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        lejos = (scenario.fecha_futura + timedelta(days=30)).isoformat()
        body = client.get(
            f"/patients/{scenario.ana_id}/appointments", params={"since": lejos}
        ).json()
        assert body == []

    def test_agenda_del_dia_resume_por_estado(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/agenda/{scenario.fecha_futura}").json()
        assert body["total"] == 1
        assert body["by_status"] == {"scheduled": 1}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


class TestAgendar:
    def test_crea_la_cita(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["appointment"]["status"] == "scheduled"
        assert body["appointment"]["created_by"] == ACTOR
        assert body["afiliacion"]["active"] is True

    def test_sin_cabecera_de_actor_usa_el_valor_por_defecto(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
        ).json()
        assert body["appointment"]["created_by"] == "mcp-server"

    def test_la_mora_viaja_como_alerta_no_como_error(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.deudor_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["cartera_alert"] is not None

    def test_cupo_ocupado_responde_409_con_alternativas(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.carla_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "SLOT_UNAVAILABLE"
        assert body["details"]["alternativas"]

    def test_cupo_pasado_responde_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slot_pasado_id},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert response.json()["code"] == "SLOT_IN_THE_PAST"

    def test_ids_invalidos_son_422(self, client: TestClient, scenario: Scenario) -> None:
        assert (
            client.post("/appointments", json={"patient_id": 0, "slot_id": -1}).status_code == 422
        )

    def test_la_idempotencia_no_duplica(self, client: TestClient, scenario: Scenario) -> None:
        body = {
            "patient_id": scenario.ana_id,
            "slot_id": scenario.slots_general[0],
            "idempotency_key": "abc-123",
        }
        primera = client.post("/appointments", json=body, headers=HEADERS).json()
        segunda = client.post("/appointments", json=body, headers=HEADERS).json()
        assert segunda["appointment"]["id"] == primera["appointment"]["id"]
        assert segunda["reused"] is True


class TestTransiciones:
    def test_confirmar(self, client: TestClient, appointment_id: int) -> None:
        body = client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS).json()
        assert body["previous_status"] == "scheduled"
        assert body["new_status"] == "confirmed"
        assert body["freed_slot"] is False

    def test_confirmar_dos_veces_es_409_con_las_alternativas(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        response = client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "INVALID_TRANSITION"
        assert "waiting" in body["suggestion"]

    def test_cancelar_exige_motivo(self, client: TestClient, appointment_id: int) -> None:
        response = client.post(f"/appointments/{appointment_id}/cancel", json={})
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_INPUT"

    def test_un_motivo_vacio_no_pasa_la_validacion(
        self, client: TestClient, appointment_id: int
    ) -> None:
        assert (
            client.post(f"/appointments/{appointment_id}/cancel", json={"reason": "  "}).status_code
            == 422
        )

    def test_cancelar_libera_el_cupo(self, client: TestClient, appointment_id: int) -> None:
        body = client.post(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "El paciente viajó"},
            headers=HEADERS,
        ).json()
        assert body["freed_slot"] is True
        assert "free again" in body["message"]

    def test_cancelar_avisa_de_la_lista_de_espera(
        self, client: TestClient, appointment_id: int, api_session: Session, scenario: Scenario
    ) -> None:
        join_waiting_list(
            api_session,
            patient_id=scenario.carla_id,
            specialty=Specialty.GENERAL_DENTISTRY,
        )
        api_session.commit()
        body = client.post(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "El paciente pidió otra fecha"},
            headers=HEADERS,
        ).json()
        assert body["next_in_waiting_list"]["patient_id"] == scenario.carla_id

    def test_asistencia_solo_acepta_sus_estados(
        self, client: TestClient, appointment_id: int
    ) -> None:
        response = client.post(
            f"/appointments/{appointment_id}/attendance",
            json={"status": "cancelled"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_atender_genera_el_cargo_en_la_respuesta(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        client.post(
            f"/appointments/{appointment_id}/attendance",
            json={"status": "waiting"},
            headers=HEADERS,
        )
        body = client.post(
            f"/appointments/{appointment_id}/attendance",
            json={"status": "attended"},
            headers=HEADERS,
        ).json()
        assert body["created_charge"] is True
        assert body["charge"]["concept"] == "cuota_moderadora"
        assert "A charge of" in body["message"]

    def test_reprogramar_devuelve_la_cita_nueva(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        body = client.post(
            f"/appointments/{appointment_id}/reschedule",
            json={"new_slot_id": scenario.slots_general[2], "reason": "Choque"},
            headers=HEADERS,
        ).json()
        assert body["appointment"]["id"] != appointment_id
        assert body["appointment"]["source_appointment_id"] == appointment_id
        assert body["appointment"]["status"] == "scheduled"


class TestListaEsperaApi:
    def test_inscribe_y_lista_en_orden(self, client: TestClient, scenario: Scenario) -> None:
        client.post(
            "/waiting-list",
            json={"patient_id": scenario.ana_id, "specialty": "orthodontics"},
        )
        client.post(
            "/waiting-list",
            json={
                "patient_id": scenario.carla_id,
                "specialty": "orthodontics",
                "priority": "urgent",
            },
        )
        body = client.get("/waiting-list", params={"specialty": "orthodontics"}).json()
        assert [e["patient_id"] for e in body] == [scenario.carla_id, scenario.ana_id]

    def test_inscripcion_duplicada_es_409(self, client: TestClient, scenario: Scenario) -> None:
        body = {"patient_id": scenario.ana_id, "specialty": "orthodontics"}
        client.post("/waiting-list", json=body)
        response = client.post("/waiting-list", json=body)
        assert response.status_code == 409
        assert response.json()["code"] == "ALREADY_ON_WAITING_LIST"

    def test_ofrecer_devuelve_a_quien_contactar(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        client.post(
            "/waiting-list",
            json={"patient_id": scenario.ana_id, "specialty": "orthodontics"},
        )
        body = client.post(
            "/waiting-list/offer",
            json={"slot_id": scenario.slots_orto[0]},
            headers=HEADERS,
        ).json()
        assert body["patient_id"] == scenario.ana_id
        assert body["phone"].startswith("+57")
        assert "Contact" in body["message"]

    def test_ofrecer_con_lista_vacia_es_404(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post("/waiting-list/offer", json={"slot_id": scenario.slots_orto[0]})
        assert response.status_code == 404
        assert response.json()["code"] == "WAITING_LIST_EMPTY"


class TestClinico:
    def test_con_consentimiento_registra_el_motivo(
        self, client: TestClient, appointment_id: int
    ) -> None:
        body = client.post(
            f"/appointments/{appointment_id}/reason",
            json={"reason": "Dolor en molar inferior derecho"},
            headers=HEADERS,
        ).json()
        assert body["reason"] == "Dolor en molar inferior derecho"

    def test_sin_consentimiento_es_403(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        appointment = book_appointment(
            api_session,
            patient_id=scenario.carla_id,
            slot_id=scenario.slots_general[1],
            user="setup",
        ).appointment
        api_session.commit()
        response = client.post(
            f"/appointments/{appointment.id}/reason", json={"reason": "Dolor"}, headers=HEADERS
        )
        assert response.status_code == 403
        body = response.json()
        assert body["code"] == "CONSENT_REQUIRED"
        assert "2654" in body["suggestion"]

    def test_un_motivo_muy_corto_es_422(self, client: TestClient, appointment_id: int) -> None:
        assert (
            client.post(f"/appointments/{appointment_id}/reason", json={"reason": "a"}).status_code
            == 422
        )


class TestContratoDeErrores:
    """Every failure the API can produce shares one shape."""

    @pytest.mark.parametrize(
        ("metodo", "path", "body"),
        [
            ("get", "/patients/999999/afiliacion", None),
            ("get", "/patients/999999/cartera", None),
            ("get", "/appointments/999999", None),
            ("post", "/appointments/999999/confirm", None),
            ("post", "/appointments/999999/cancel", {"reason": "prueba"}),
        ],
    )
    def test_todo_404_trae_codigo_mensaje_y_sugerencia(
        self,
        client: TestClient,
        scenario: Scenario,
        metodo: str,
        path: str,
        body: dict[str, str] | None,
    ) -> None:
        response = (
            getattr(client, metodo)(path, json=body) if body else getattr(client, metodo)(path)
        )
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"] is True
        assert payload["code"]
        assert payload["message"]
        assert payload["suggestion"]

    def test_un_422_usa_la_misma_envoltura_y_nombra_los_campos(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """FastAPI's default 422 body is a second error shape. It must not
        escape: one envelope, or the caller has to branch on which one it got."""
        response = client.post("/appointments", json={"patient_id": "no-es-un-numero"})
        assert response.status_code == 422
        body = response.json()
        assert "detail" not in body
        assert body["error"] is True
        assert body["code"] == "INVALID_INPUT"
        fields = {c["field"] for c in body["details"]["fields"]}
        assert "body.patient_id" in fields
        assert "body.slot_id" in fields
        assert "patient_id" in body["suggestion"]

    def test_ninguna_respuesta_de_error_filtra_internos(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        crudo = client.get("/appointments/999999").text
        assert "Traceback" not in crudo
        assert "sqlalchemy" not in crudo.lower()


class TestFlujoCompleto:
    def test_agendar_confirmar_cancelar_y_ofrecer_el_cupo(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """The end-to-end path a receptionist actually walks."""
        slot = client.get("/availability", params={"specialty": "orthodontics"}).json()[0]

        creada = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": slot["slot_id"]},
            headers=HEADERS,
        ).json()["appointment"]

        confirmed = client.post(f"/appointments/{creada['id']}/confirm", headers=HEADERS).json()
        assert confirmed["new_status"] == "confirmed"

        client.post(
            "/waiting-list",
            json={"patient_id": scenario.carla_id, "specialty": "orthodontics"},
        )

        cancelada = client.post(
            f"/appointments/{creada['id']}/cancel",
            json={"reason": "El paciente tuvo una urgencia"},
            headers=HEADERS,
        ).json()
        assert cancelada["freed_slot"] is True
        assert cancelada["next_in_waiting_list"]["patient_id"] == scenario.carla_id

        oferta = client.post(
            "/waiting-list/offer", json={"slot_id": slot["slot_id"]}, headers=HEADERS
        ).json()
        assert oferta["patient_id"] == scenario.carla_id

        detalle = client.get(f"/appointments/{creada['id']}").json()
        assert [h["new_status"] for h in detalle["history"]] == [
            "scheduled",
            "confirmed",
            "cancelled",
        ]
        assert all(h["user"] in {"mcp-server", ACTOR} for h in detalle["history"])


class TestSlotReservable:
    """The endpoint the tool layer calls before proposing a booking."""

    def test_un_cupo_libre_se_devuelve_con_su_hora_local(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get(f"/availability/{scenario.slots_general[0]}").json()
        assert body["slot_id"] == scenario.slots_general[0]
        assert body["start_local"].startswith(str(scenario.fecha_futura))
        assert body["professional"] == "Dra. General"

    def test_un_cupo_ocupado_es_409_con_alternativas(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        response = client.get(f"/availability/{scenario.slots_general[0]}")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "SLOT_UNAVAILABLE"
        assert body["details"]["alternativas"]

    def test_un_cupo_pasado_es_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get(f"/availability/{scenario.slot_pasado_id}")
        assert response.status_code == 400
        assert response.json()["code"] == "SLOT_IN_THE_PAST"

    def test_un_cupo_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/availability/999999").status_code == 404

    def test_falla_por_las_mismas_razones_que_agendar(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """Both paths run the same validation, so a proposal that passes here
        cannot be rejected for a different reason on confirmation."""
        for slot_id in (scenario.slot_pasado_id, 999999):
            chequeo = client.get(f"/availability/{slot_id}")
            agendado = client.post(
                "/appointments",
                json={"patient_id": scenario.ana_id, "slot_id": slot_id},
                headers=HEADERS,
            )
            assert chequeo.status_code == agendado.status_code
            assert chequeo.json()["code"] == agendado.json()["code"]


class TestTransicionesValidas:
    """The appointment detail tells the model what it can do next."""

    def test_una_cita_agendada_lista_sus_salidas(
        self, client: TestClient, appointment_id: int
    ) -> None:
        body = client.get(f"/appointments/{appointment_id}").json()
        assert set(body["valid_transitions"]) == {
            "confirmed",
            "cancelled",
            "rescheduled",
            "no_show",
        }

    def test_una_cita_cancelada_no_tiene_salidas(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "El paciente viajó"},
            headers=HEADERS,
        )
        assert client.get(f"/appointments/{appointment_id}").json()["valid_transitions"] == []

    def test_las_salidas_siguen_a_la_transicion(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        assert set(client.get(f"/appointments/{appointment_id}").json()["valid_transitions"]) == {
            "waiting",
            "cancelled",
            "rescheduled",
            "no_show",
        }


class TestValidarReservaCompleta:
    """`/availability/{slot_id}` answers the same question `POST /appointments` does."""

    def test_detecta_el_cruce_de_horario_del_paciente(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        """The other reason a booking fails, and the one that used to surface
        only on confirmation: the patient is already booked at that hour with a
        different professional."""
        book_appointment(
            api_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        api_session.commit()

        response = client.get(
            f"/availability/{scenario.slots_orto[0]}",
            params={"patient_id": scenario.ana_id},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "PATIENT_ALREADY_BOOKED"

    def test_sin_paciente_no_comprueba_el_cruce(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            api_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        api_session.commit()
        assert client.get(f"/availability/{scenario.slots_orto[0]}").status_code == 200

    def test_excluir_cita_permite_reprogramar_sobre_uno_mismo(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        appointment = book_appointment(
            api_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        ).appointment
        api_session.commit()

        # The overlapping slot belongs to the appointment being moved, so with
        # the exclusion it must not count as a conflict.
        response = client.get(
            f"/availability/{scenario.slots_orto[0]}",
            params={"patient_id": scenario.ana_id, "exclude_appointment_id": appointment.id},
        )
        assert response.status_code == 200

    def test_verifica_la_especialidad_esperada(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.get(
            f"/availability/{scenario.slots_general[0]}",
            params={"expected_specialty": "orthodontics"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "SPECIALTY_MISMATCH"

    def test_coincide_con_lo_que_hace_agendar(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        """The invariant that makes propose-time checking trustworthy: both
        paths refuse for the same code."""
        book_appointment(
            api_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        api_session.commit()

        chequeo = client.get(
            f"/availability/{scenario.slots_orto[0]}",
            params={"patient_id": scenario.ana_id},
        )
        agendado = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slots_orto[0]},
            headers=HEADERS,
        )
        assert chequeo.status_code == agendado.status_code == 409
        assert chequeo.json()["code"] == agendado.json()["code"]
