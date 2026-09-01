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
def cita_id(api_session: Session, scenario: Scenario) -> int:
    result = book_appointment(
        api_session,
        paciente_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        usuario="setup",
    )
    api_session.commit()
    return result.cita.id


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


class TestClinica:
    def test_devuelve_la_clinica_con_sus_profesionales(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get("/clinica").json()
        assert body["nombre"] == "Clínica Escenario"
        assert body["zona_horaria"] == "America/Bogota"
        assert {p["especialidad"] for p in body["profesionales"]} == {
            "general_dentistry",
            "orthodontics",
        }


class TestPoliticas:
    def test_expone_las_reglas_de_cartera(self, client: TestClient) -> None:
        body = client.get("/politicas/cartera").json()
        assert body["cobra_no_show"] is True
        assert body["plazo_pago_dias"] == 30
        assert "general_dentistry" in body["tarifas_particular"]

    def test_deja_explicita_la_regla_de_no_bloqueo(self, client: TestClient) -> None:
        assert "never a block" in client.get("/politicas/cartera").json()["nota"]


class TestBuscarPacientes:
    def test_por_documento(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/pacientes", params={"documento": "11111111"}).json()
        assert [p["id"] for p in body] == [scenario.ana_id]

    def test_por_nombre(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/pacientes", params={"nombre": "bruno"}).json()
        assert body[0]["regimen"] == "subsidiado"

    def test_sin_criterio_responde_404_estructurado(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.get("/pacientes")
        assert response.status_code == 404
        assert response.json()["codigo"] == "PACIENTE_NO_ENCONTRADO"

    def test_un_documento_demasiado_largo_es_422(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        assert client.get("/pacientes", params={"documento": "9" * 40}).status_code == 422

    def test_no_devuelve_datos_clinicos(self, client: TestClient, scenario: Scenario) -> None:
        """The read tool must not leak clinical fields into a lookup response."""
        body = client.get("/pacientes", params={"documento": "11111111"}).json()[0]
        assert "motivo" not in body
        assert "consentimiento_datos_clinicos" not in body


class TestAfiliacion:
    def test_activa(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/pacientes/{scenario.ana_id}/afiliacion").json()
        assert body["activa"] is True
        assert body["bloquea_agendamiento"] is False

    def test_inactiva_cae_a_particular(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/pacientes/{scenario.bruno_id}/afiliacion").json()
        assert body["regimen"] == "subsidiado"
        assert body["regimen_efectivo"] == "particular"
        assert body["sugerencia"]

    def test_paciente_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/pacientes/999999/afiliacion").status_code == 404


class TestCartera:
    def test_al_dia(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/pacientes/{scenario.ana_id}/cartera").json()
        assert body["estado"] == "al_dia"
        assert body["cargos"] == []

    def test_en_mora_con_detalle(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/pacientes/{scenario.deudor_id}/cartera").json()
        assert body["estado"] == "en_mora"
        assert body["total_vencido"] == "180000"
        assert body["supera_umbral_alerta"] is True
        assert len(body["cargos"]) == 1
        assert body["antiguedad"]["61_90"] == "180000"


class TestDisponibilidad:
    def test_devuelve_cupos_futuros(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/disponibilidad").json()
        assert body
        assert all("inicio_local" in s for s in body)

    def test_expone_hora_local_y_utc(self, client: TestClient, scenario: Scenario) -> None:
        """The model reasons in local time; the system stores UTC. Both ship."""
        slot = client.get("/disponibilidad").json()[0]
        assert slot["inicio_utc"].endswith("Z") or "+00:00" in slot["inicio_utc"]
        assert slot["inicio_local"].startswith(str(scenario.fecha_futura))

    def test_filtra_por_especialidad(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/disponibilidad", params={"especialidad": "orthodontics"}).json()
        assert all(s["especialidad"] == "orthodontics" for s in body)

    def test_especialidad_invalida_es_422(self, client: TestClient, scenario: Scenario) -> None:
        assert (
            client.get("/disponibilidad", params={"especialidad": "astrologia"}).status_code == 422
        )

    def test_profesional_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/disponibilidad", params={"profesional_id": 999999}).status_code == 404


class TestCitas:
    def test_detalle_incluye_historial(self, client: TestClient, cita_id: int) -> None:
        body = client.get(f"/citas/{cita_id}").json()
        assert body["estado"] == "scheduled"
        assert len(body["historial"]) == 1

    def test_cita_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get("/citas/999999")
        assert response.status_code == 404
        assert response.json()["codigo"] == "CITA_NO_ENCONTRADA"

    def test_listado_por_paciente(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/pacientes/{scenario.ana_id}/citas").json()
        assert [c["id"] for c in body] == [cita_id]

    def test_listado_filtra_por_rango(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        lejos = (scenario.fecha_futura + timedelta(days=30)).isoformat()
        body = client.get(f"/pacientes/{scenario.ana_id}/citas", params={"desde": lejos}).json()
        assert body == []

    def test_agenda_del_dia_resume_por_estado(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/agenda/{scenario.fecha_futura}").json()
        assert body["total"] == 1
        assert body["por_estado"] == {"scheduled": 1}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


class TestAgendar:
    def test_crea_la_cita(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/citas",
            json={"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["cita"]["estado"] == "scheduled"
        assert body["cita"]["creada_por"] == ACTOR
        assert body["afiliacion"]["activa"] is True

    def test_sin_cabecera_de_actor_usa_el_valor_por_defecto(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.post(
            "/citas",
            json={"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
        ).json()
        assert body["cita"]["creada_por"] == "mcp-server"

    def test_la_mora_viaja_como_alerta_no_como_error(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.post(
            "/citas",
            json={"paciente_id": scenario.deudor_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["alerta_cartera"] is not None

    def test_cupo_ocupado_responde_409_con_alternativas(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        response = client.post(
            "/citas",
            json={"paciente_id": scenario.carla_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 409
        body = response.json()
        assert body["codigo"] == "SLOT_NO_DISPONIBLE"
        assert body["detalles"]["alternativas"]

    def test_cupo_pasado_responde_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/citas",
            json={"paciente_id": scenario.ana_id, "slot_id": scenario.slot_pasado_id},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert response.json()["codigo"] == "SLOT_EN_EL_PASADO"

    def test_ids_invalidos_son_422(self, client: TestClient, scenario: Scenario) -> None:
        assert client.post("/citas", json={"paciente_id": 0, "slot_id": -1}).status_code == 422

    def test_la_idempotencia_no_duplica(self, client: TestClient, scenario: Scenario) -> None:
        body = {
            "paciente_id": scenario.ana_id,
            "slot_id": scenario.slots_general[0],
            "idempotency_key": "abc-123",
        }
        primera = client.post("/citas", json=body, headers=HEADERS).json()
        segunda = client.post("/citas", json=body, headers=HEADERS).json()
        assert segunda["cita"]["id"] == primera["cita"]["id"]
        assert segunda["reutilizada"] is True


class TestTransiciones:
    def test_confirmar(self, client: TestClient, cita_id: int) -> None:
        body = client.post(f"/citas/{cita_id}/confirmar", headers=HEADERS).json()
        assert body["estado_anterior"] == "scheduled"
        assert body["estado_nuevo"] == "confirmed"
        assert body["libero_cupo"] is False

    def test_confirmar_dos_veces_es_409_con_las_alternativas(
        self, client: TestClient, cita_id: int
    ) -> None:
        client.post(f"/citas/{cita_id}/confirmar", headers=HEADERS)
        response = client.post(f"/citas/{cita_id}/confirmar", headers=HEADERS)
        assert response.status_code == 409
        body = response.json()
        assert body["codigo"] == "TRANSICION_INVALIDA"
        assert "waiting" in body["sugerencia"]

    def test_cancelar_exige_motivo(self, client: TestClient, cita_id: int) -> None:
        response = client.post(f"/citas/{cita_id}/cancelar", json={})
        assert response.status_code == 422
        assert response.json()["codigo"] == "ENTRADA_INVALIDA"

    def test_un_motivo_vacio_no_pasa_la_validacion(self, client: TestClient, cita_id: int) -> None:
        assert client.post(f"/citas/{cita_id}/cancelar", json={"motivo": "  "}).status_code == 422

    def test_cancelar_libera_el_cupo(self, client: TestClient, cita_id: int) -> None:
        body = client.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente viajó"},
            headers=HEADERS,
        ).json()
        assert body["libero_cupo"] is True
        assert "free again" in body["mensaje"]

    def test_cancelar_avisa_de_la_lista_de_espera(
        self, client: TestClient, cita_id: int, api_session: Session, scenario: Scenario
    ) -> None:
        join_waiting_list(
            api_session,
            paciente_id=scenario.carla_id,
            especialidad=Specialty.GENERAL_DENTISTRY,
        )
        api_session.commit()
        body = client.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente pidió otra fecha"},
            headers=HEADERS,
        ).json()
        assert body["siguiente_en_lista_espera"]["paciente_id"] == scenario.carla_id

    def test_asistencia_solo_acepta_sus_estados(self, client: TestClient, cita_id: int) -> None:
        response = client.post(
            f"/citas/{cita_id}/asistencia", json={"estado": "cancelled"}, headers=HEADERS
        )
        assert response.status_code == 422

    def test_atender_genera_el_cargo_en_la_respuesta(
        self, client: TestClient, cita_id: int
    ) -> None:
        client.post(f"/citas/{cita_id}/confirmar", headers=HEADERS)
        client.post(f"/citas/{cita_id}/asistencia", json={"estado": "waiting"}, headers=HEADERS)
        body = client.post(
            f"/citas/{cita_id}/asistencia", json={"estado": "attended"}, headers=HEADERS
        ).json()
        assert body["genero_cargo"] is True
        assert body["cargo"]["concepto"] == "cuota_moderadora"
        assert "A charge of" in body["mensaje"]

    def test_reprogramar_devuelve_la_cita_nueva(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        body = client.post(
            f"/citas/{cita_id}/reprogramar",
            json={"nuevo_slot_id": scenario.slots_general[2], "motivo": "Choque"},
            headers=HEADERS,
        ).json()
        assert body["cita"]["id"] != cita_id
        assert body["cita"]["cita_origen_id"] == cita_id
        assert body["cita"]["estado"] == "scheduled"


class TestListaEsperaApi:
    def test_inscribe_y_lista_en_orden(self, client: TestClient, scenario: Scenario) -> None:
        client.post(
            "/lista-espera",
            json={"paciente_id": scenario.ana_id, "especialidad": "orthodontics"},
        )
        client.post(
            "/lista-espera",
            json={
                "paciente_id": scenario.carla_id,
                "especialidad": "orthodontics",
                "prioridad": "urgent",
            },
        )
        body = client.get("/lista-espera", params={"especialidad": "orthodontics"}).json()
        assert [e["paciente_id"] for e in body] == [scenario.carla_id, scenario.ana_id]

    def test_inscripcion_duplicada_es_409(self, client: TestClient, scenario: Scenario) -> None:
        body = {"paciente_id": scenario.ana_id, "especialidad": "orthodontics"}
        client.post("/lista-espera", json=body)
        response = client.post("/lista-espera", json=body)
        assert response.status_code == 409
        assert response.json()["codigo"] == "YA_EN_LISTA_ESPERA"

    def test_ofrecer_devuelve_a_quien_contactar(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        client.post(
            "/lista-espera",
            json={"paciente_id": scenario.ana_id, "especialidad": "orthodontics"},
        )
        body = client.post(
            "/lista-espera/ofrecer",
            json={"slot_id": scenario.slots_orto[0]},
            headers=HEADERS,
        ).json()
        assert body["paciente_id"] == scenario.ana_id
        assert body["telefono"].startswith("+57")
        assert "Contact" in body["mensaje"]

    def test_ofrecer_con_lista_vacia_es_404(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post("/lista-espera/ofrecer", json={"slot_id": scenario.slots_orto[0]})
        assert response.status_code == 404
        assert response.json()["codigo"] == "LISTA_ESPERA_VACIA"


class TestClinico:
    def test_con_consentimiento_registra_el_motivo(self, client: TestClient, cita_id: int) -> None:
        body = client.post(
            f"/citas/{cita_id}/motivo",
            json={"motivo": "Dolor en molar inferior derecho"},
            headers=HEADERS,
        ).json()
        assert body["motivo"] == "Dolor en molar inferior derecho"

    def test_sin_consentimiento_es_403(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        cita = book_appointment(
            api_session,
            paciente_id=scenario.carla_id,
            slot_id=scenario.slots_general[1],
            usuario="setup",
        ).cita
        api_session.commit()
        response = client.post(
            f"/citas/{cita.id}/motivo", json={"motivo": "Dolor"}, headers=HEADERS
        )
        assert response.status_code == 403
        body = response.json()
        assert body["codigo"] == "CONSENTIMIENTO_REQUERIDO"
        assert "2654" in body["sugerencia"]

    def test_un_motivo_muy_corto_es_422(self, client: TestClient, cita_id: int) -> None:
        assert client.post(f"/citas/{cita_id}/motivo", json={"motivo": "a"}).status_code == 422


class TestContratoDeErrores:
    """Every failure the API can produce shares one shape."""

    @pytest.mark.parametrize(
        ("metodo", "ruta", "body"),
        [
            ("get", "/pacientes/999999/afiliacion", None),
            ("get", "/pacientes/999999/cartera", None),
            ("get", "/citas/999999", None),
            ("post", "/citas/999999/confirmar", None),
            ("post", "/citas/999999/cancelar", {"motivo": "prueba"}),
        ],
    )
    def test_todo_404_trae_codigo_mensaje_y_sugerencia(
        self,
        client: TestClient,
        scenario: Scenario,
        metodo: str,
        ruta: str,
        body: dict[str, str] | None,
    ) -> None:
        response = (
            getattr(client, metodo)(ruta, json=body) if body else getattr(client, metodo)(ruta)
        )
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"] is True
        assert payload["codigo"]
        assert payload["mensaje"]
        assert payload["sugerencia"]

    def test_un_422_usa_la_misma_envoltura_y_nombra_los_campos(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """FastAPI's default 422 body is a second error shape. It must not
        escape: one envelope, or the caller has to branch on which one it got."""
        response = client.post("/citas", json={"paciente_id": "no-es-un-numero"})
        assert response.status_code == 422
        body = response.json()
        assert "detail" not in body
        assert body["error"] is True
        assert body["codigo"] == "ENTRADA_INVALIDA"
        campos = {c["campo"] for c in body["detalles"]["campos"]}
        assert "body.paciente_id" in campos
        assert "body.slot_id" in campos
        assert "paciente_id" in body["sugerencia"]

    def test_ninguna_respuesta_de_error_filtra_internos(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        crudo = client.get("/citas/999999").text
        assert "Traceback" not in crudo
        assert "sqlalchemy" not in crudo.lower()


class TestFlujoCompleto:
    def test_agendar_confirmar_cancelar_y_ofrecer_el_cupo(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """The end-to-end path a receptionist actually walks."""
        slot = client.get("/disponibilidad", params={"especialidad": "orthodontics"}).json()[0]

        creada = client.post(
            "/citas",
            json={"paciente_id": scenario.ana_id, "slot_id": slot["slot_id"]},
            headers=HEADERS,
        ).json()["cita"]

        confirmed = client.post(f"/citas/{creada['id']}/confirmar", headers=HEADERS).json()
        assert confirmed["estado_nuevo"] == "confirmed"

        client.post(
            "/lista-espera",
            json={"paciente_id": scenario.carla_id, "especialidad": "orthodontics"},
        )

        cancelada = client.post(
            f"/citas/{creada['id']}/cancelar",
            json={"motivo": "El paciente tuvo una urgencia"},
            headers=HEADERS,
        ).json()
        assert cancelada["libero_cupo"] is True
        assert cancelada["siguiente_en_lista_espera"]["paciente_id"] == scenario.carla_id

        oferta = client.post(
            "/lista-espera/ofrecer", json={"slot_id": slot["slot_id"]}, headers=HEADERS
        ).json()
        assert oferta["paciente_id"] == scenario.carla_id

        detalle = client.get(f"/citas/{creada['id']}").json()
        assert [h["estado_nuevo"] for h in detalle["historial"]] == [
            "scheduled",
            "confirmed",
            "cancelled",
        ]
        assert all(h["usuario"] in {"mcp-server", ACTOR} for h in detalle["historial"])


class TestSlotReservable:
    """The endpoint the tool layer calls before proposing a booking."""

    def test_un_cupo_libre_se_devuelve_con_su_hora_local(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get(f"/disponibilidad/{scenario.slots_general[0]}").json()
        assert body["slot_id"] == scenario.slots_general[0]
        assert body["inicio_local"].startswith(str(scenario.fecha_futura))
        assert body["profesional"] == "Dra. General"

    def test_un_cupo_ocupado_es_409_con_alternativas(
        self, client: TestClient, cita_id: int, scenario: Scenario
    ) -> None:
        response = client.get(f"/disponibilidad/{scenario.slots_general[0]}")
        assert response.status_code == 409
        body = response.json()
        assert body["codigo"] == "SLOT_NO_DISPONIBLE"
        assert body["detalles"]["alternativas"]

    def test_un_cupo_pasado_es_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get(f"/disponibilidad/{scenario.slot_pasado_id}")
        assert response.status_code == 400
        assert response.json()["codigo"] == "SLOT_EN_EL_PASADO"

    def test_un_cupo_inexistente_es_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/disponibilidad/999999").status_code == 404

    def test_falla_por_las_mismas_razones_que_agendar(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """Both paths run the same validation, so a proposal that passes here
        cannot be rejected for a different reason on confirmation."""
        for slot_id in (scenario.slot_pasado_id, 999999):
            chequeo = client.get(f"/disponibilidad/{slot_id}")
            agendado = client.post(
                "/citas",
                json={"paciente_id": scenario.ana_id, "slot_id": slot_id},
                headers=HEADERS,
            )
            assert chequeo.status_code == agendado.status_code
            assert chequeo.json()["codigo"] == agendado.json()["codigo"]


class TestTransicionesValidas:
    """The appointment detail tells the model what it can do next."""

    def test_una_cita_agendada_lista_sus_salidas(self, client: TestClient, cita_id: int) -> None:
        body = client.get(f"/citas/{cita_id}").json()
        assert set(body["transiciones_validas"]) == {
            "confirmed",
            "cancelled",
            "rescheduled",
            "no_show",
        }

    def test_una_cita_cancelada_no_tiene_salidas(self, client: TestClient, cita_id: int) -> None:
        client.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente viajó"},
            headers=HEADERS,
        )
        assert client.get(f"/citas/{cita_id}").json()["transiciones_validas"] == []

    def test_las_salidas_siguen_a_la_transicion(self, client: TestClient, cita_id: int) -> None:
        client.post(f"/citas/{cita_id}/confirmar", headers=HEADERS)
        assert set(client.get(f"/citas/{cita_id}").json()["transiciones_validas"]) == {
            "waiting",
            "cancelled",
            "rescheduled",
            "no_show",
        }


class TestValidarReservaCompleta:
    """`/disponibilidad/{slot_id}` answers the same question `POST /citas` does."""

    def test_detecta_el_cruce_de_horario_del_paciente(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        """The other reason a booking fails, and the one that used to surface
        only on confirmation: the patient is already booked at that hour with a
        different professional."""
        book_appointment(
            api_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        )
        api_session.commit()

        response = client.get(
            f"/disponibilidad/{scenario.slots_orto[0]}",
            params={"paciente_id": scenario.ana_id},
        )
        assert response.status_code == 409
        assert response.json()["codigo"] == "PACIENTE_YA_TIENE_CITA"

    def test_sin_paciente_no_comprueba_el_cruce(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            api_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        )
        api_session.commit()
        assert client.get(f"/disponibilidad/{scenario.slots_orto[0]}").status_code == 200

    def test_excluir_cita_permite_reprogramar_sobre_uno_mismo(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        cita = book_appointment(
            api_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        ).cita
        api_session.commit()

        # The overlapping slot belongs to the appointment being moved, so with
        # the exclusion it must not count as a conflict.
        response = client.get(
            f"/disponibilidad/{scenario.slots_orto[0]}",
            params={"paciente_id": scenario.ana_id, "excluir_cita_id": cita.id},
        )
        assert response.status_code == 200

    def test_verifica_la_especialidad_esperada(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.get(
            f"/disponibilidad/{scenario.slots_general[0]}",
            params={"especialidad_esperada": "orthodontics"},
        )
        assert response.status_code == 400
        assert response.json()["codigo"] == "ESPECIALIDAD_NO_COINCIDE"

    def test_coincide_con_lo_que_hace_agendar(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        """The invariant that makes propose-time checking trustworthy: both
        paths refuse for the same code."""
        book_appointment(
            api_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        )
        api_session.commit()

        chequeo = client.get(
            f"/disponibilidad/{scenario.slots_orto[0]}",
            params={"paciente_id": scenario.ana_id},
        )
        agendado = client.post(
            "/citas",
            json={"paciente_id": scenario.ana_id, "slot_id": scenario.slots_orto[0]},
            headers=HEADERS,
        )
        assert chequeo.status_code == agendado.status_code == 409
        assert chequeo.json()["codigo"] == agendado.json()["codigo"]
