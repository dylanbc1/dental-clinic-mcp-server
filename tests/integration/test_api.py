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
from backend.domain.servicios import agendar_cita, inscribir_en_lista_espera
from backend.enums import Especialidad
from tests.conftest import Escenario

pytestmark = pytest.mark.integration

ACTOR = "api-test@clinica.test"
CABECERAS = {"X-Actor": ACTOR}


@pytest.fixture
def sesion_api(sesiones: Callable[[], Session]) -> Session:
    return sesiones()


@pytest.fixture
def cliente(sesion_api: Session) -> Iterator[TestClient]:
    def override() -> Iterator[Session]:
        yield sesion_api
        sesion_api.commit()

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def cita_id(sesion_api: Session, escenario: Escenario) -> int:
    resultado = agendar_cita(
        sesion_api,
        paciente_id=escenario.ana_id,
        slot_id=escenario.slots_general[0],
        usuario="setup",
    )
    sesion_api.commit()
    return resultado.cita.id


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


class TestClinica:
    def test_devuelve_la_clinica_con_sus_profesionales(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        cuerpo = cliente.get("/clinica").json()
        assert cuerpo["nombre"] == "Clínica Escenario"
        assert cuerpo["zona_horaria"] == "America/Bogota"
        assert {p["especialidad"] for p in cuerpo["profesionales"]} == {
            "odontologia_general",
            "ortodoncia",
        }


class TestPoliticas:
    def test_expone_las_reglas_de_cartera(self, cliente: TestClient) -> None:
        cuerpo = cliente.get("/politicas/cartera").json()
        assert cuerpo["cobra_no_show"] is True
        assert cuerpo["plazo_pago_dias"] == 30
        assert "odontologia_general" in cuerpo["tarifas_particular"]

    def test_deja_explicita_la_regla_de_no_bloqueo(self, cliente: TestClient) -> None:
        assert "never a block" in cliente.get("/politicas/cartera").json()["nota"]


class TestBuscarPacientes:
    def test_por_documento(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get("/pacientes", params={"documento": "11111111"}).json()
        assert [p["id"] for p in cuerpo] == [escenario.ana_id]

    def test_por_nombre(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get("/pacientes", params={"nombre": "bruno"}).json()
        assert cuerpo[0]["regimen"] == "subsidiado"

    def test_sin_criterio_responde_404_estructurado(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        respuesta = cliente.get("/pacientes")
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "PACIENTE_NO_ENCONTRADO"

    def test_un_documento_demasiado_largo_es_422(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        assert cliente.get("/pacientes", params={"documento": "9" * 40}).status_code == 422

    def test_no_devuelve_datos_clinicos(self, cliente: TestClient, escenario: Escenario) -> None:
        """The read tool must not leak clinical fields into a lookup response."""
        cuerpo = cliente.get("/pacientes", params={"documento": "11111111"}).json()[0]
        assert "motivo" not in cuerpo
        assert "consentimiento_datos_clinicos" not in cuerpo


class TestAfiliacion:
    def test_activa(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get(f"/pacientes/{escenario.ana_id}/afiliacion").json()
        assert cuerpo["activa"] is True
        assert cuerpo["bloquea_agendamiento"] is False

    def test_inactiva_cae_a_particular(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get(f"/pacientes/{escenario.bruno_id}/afiliacion").json()
        assert cuerpo["regimen"] == "subsidiado"
        assert cuerpo["regimen_efectivo"] == "particular"
        assert cuerpo["sugerencia"]

    def test_paciente_inexistente_es_404(self, cliente: TestClient, escenario: Escenario) -> None:
        assert cliente.get("/pacientes/999999/afiliacion").status_code == 404


class TestCartera:
    def test_al_dia(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get(f"/pacientes/{escenario.ana_id}/cartera").json()
        assert cuerpo["estado"] == "al_dia"
        assert cuerpo["cargos"] == []

    def test_en_mora_con_detalle(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get(f"/pacientes/{escenario.deudor_id}/cartera").json()
        assert cuerpo["estado"] == "en_mora"
        assert cuerpo["total_vencido"] == "180000"
        assert cuerpo["supera_umbral_alerta"] is True
        assert len(cuerpo["cargos"]) == 1
        assert cuerpo["antiguedad"]["61_90"] == "180000"


class TestDisponibilidad:
    def test_devuelve_cupos_futuros(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get("/disponibilidad").json()
        assert cuerpo
        assert all("inicio_local" in s for s in cuerpo)

    def test_expone_hora_local_y_utc(self, cliente: TestClient, escenario: Escenario) -> None:
        """The model reasons in local time; the system stores UTC. Both ship."""
        slot = cliente.get("/disponibilidad").json()[0]
        assert slot["inicio_utc"].endswith("Z") or "+00:00" in slot["inicio_utc"]
        assert slot["inicio_local"].startswith(str(escenario.fecha_futura))

    def test_filtra_por_especialidad(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = cliente.get("/disponibilidad", params={"especialidad": "ortodoncia"}).json()
        assert all(s["especialidad"] == "ortodoncia" for s in cuerpo)

    def test_especialidad_invalida_es_422(self, cliente: TestClient, escenario: Escenario) -> None:
        assert (
            cliente.get("/disponibilidad", params={"especialidad": "astrologia"}).status_code == 422
        )

    def test_profesional_inexistente_es_404(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        assert cliente.get("/disponibilidad", params={"profesional_id": 999999}).status_code == 404


class TestCitas:
    def test_detalle_incluye_historial(self, cliente: TestClient, cita_id: int) -> None:
        cuerpo = cliente.get(f"/citas/{cita_id}").json()
        assert cuerpo["estado"] == "agendada"
        assert len(cuerpo["historial"]) == 1

    def test_cita_inexistente_es_404(self, cliente: TestClient, escenario: Escenario) -> None:
        respuesta = cliente.get("/citas/999999")
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "CITA_NO_ENCONTRADA"

    def test_listado_por_paciente(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        cuerpo = cliente.get(f"/pacientes/{escenario.ana_id}/citas").json()
        assert [c["id"] for c in cuerpo] == [cita_id]

    def test_listado_filtra_por_rango(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        lejos = (escenario.fecha_futura + timedelta(days=30)).isoformat()
        cuerpo = cliente.get(f"/pacientes/{escenario.ana_id}/citas", params={"desde": lejos}).json()
        assert cuerpo == []

    def test_agenda_del_dia_resume_por_estado(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        cuerpo = cliente.get(f"/agenda/{escenario.fecha_futura}").json()
        assert cuerpo["total"] == 1
        assert cuerpo["por_estado"] == {"agendada": 1}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


class TestAgendar:
    def test_crea_la_cita(self, cliente: TestClient, escenario: Escenario) -> None:
        respuesta = cliente.post(
            "/citas",
            json={"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            headers=CABECERAS,
        )
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["cita"]["estado"] == "agendada"
        assert cuerpo["cita"]["creada_por"] == ACTOR
        assert cuerpo["afiliacion"]["activa"] is True

    def test_sin_cabecera_de_actor_usa_el_valor_por_defecto(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        cuerpo = cliente.post(
            "/citas",
            json={"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
        ).json()
        assert cuerpo["cita"]["creada_por"] == "mcp-server"

    def test_la_mora_viaja_como_alerta_no_como_error(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        respuesta = cliente.post(
            "/citas",
            json={"paciente_id": escenario.deudor_id, "slot_id": escenario.slots_general[0]},
            headers=CABECERAS,
        )
        assert respuesta.status_code == 200
        assert respuesta.json()["alerta_cartera"] is not None

    def test_cupo_ocupado_responde_409_con_alternativas(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        respuesta = cliente.post(
            "/citas",
            json={"paciente_id": escenario.carla_id, "slot_id": escenario.slots_general[0]},
            headers=CABECERAS,
        )
        assert respuesta.status_code == 409
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "SLOT_NO_DISPONIBLE"
        assert cuerpo["detalles"]["alternativas"]

    def test_cupo_pasado_responde_400(self, cliente: TestClient, escenario: Escenario) -> None:
        respuesta = cliente.post(
            "/citas",
            json={"paciente_id": escenario.ana_id, "slot_id": escenario.slot_pasado_id},
            headers=CABECERAS,
        )
        assert respuesta.status_code == 400
        assert respuesta.json()["codigo"] == "SLOT_EN_EL_PASADO"

    def test_ids_invalidos_son_422(self, cliente: TestClient, escenario: Escenario) -> None:
        assert cliente.post("/citas", json={"paciente_id": 0, "slot_id": -1}).status_code == 422

    def test_la_idempotencia_no_duplica(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = {
            "paciente_id": escenario.ana_id,
            "slot_id": escenario.slots_general[0],
            "idempotency_key": "abc-123",
        }
        primera = cliente.post("/citas", json=cuerpo, headers=CABECERAS).json()
        segunda = cliente.post("/citas", json=cuerpo, headers=CABECERAS).json()
        assert segunda["cita"]["id"] == primera["cita"]["id"]
        assert segunda["reutilizada"] is True


class TestTransiciones:
    def test_confirmar(self, cliente: TestClient, cita_id: int) -> None:
        cuerpo = cliente.post(f"/citas/{cita_id}/confirmar", headers=CABECERAS).json()
        assert cuerpo["estado_anterior"] == "agendada"
        assert cuerpo["estado_nuevo"] == "confirmada"
        assert cuerpo["libero_cupo"] is False

    def test_confirmar_dos_veces_es_409_con_las_alternativas(
        self, cliente: TestClient, cita_id: int
    ) -> None:
        cliente.post(f"/citas/{cita_id}/confirmar", headers=CABECERAS)
        respuesta = cliente.post(f"/citas/{cita_id}/confirmar", headers=CABECERAS)
        assert respuesta.status_code == 409
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "TRANSICION_INVALIDA"
        assert "en_espera" in cuerpo["sugerencia"]

    def test_cancelar_exige_motivo(self, cliente: TestClient, cita_id: int) -> None:
        respuesta = cliente.post(f"/citas/{cita_id}/cancelar", json={})
        assert respuesta.status_code == 422
        assert respuesta.json()["codigo"] == "ENTRADA_INVALIDA"

    def test_un_motivo_vacio_no_pasa_la_validacion(self, cliente: TestClient, cita_id: int) -> None:
        assert cliente.post(f"/citas/{cita_id}/cancelar", json={"motivo": "  "}).status_code == 422

    def test_cancelar_libera_el_cupo(self, cliente: TestClient, cita_id: int) -> None:
        cuerpo = cliente.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente viajó"},
            headers=CABECERAS,
        ).json()
        assert cuerpo["libero_cupo"] is True
        assert "free again" in cuerpo["mensaje"]

    def test_cancelar_avisa_de_la_lista_de_espera(
        self, cliente: TestClient, cita_id: int, sesion_api: Session, escenario: Escenario
    ) -> None:
        inscribir_en_lista_espera(
            sesion_api,
            paciente_id=escenario.carla_id,
            especialidad=Especialidad.ODONTOLOGIA_GENERAL,
        )
        sesion_api.commit()
        cuerpo = cliente.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente pidió otra fecha"},
            headers=CABECERAS,
        ).json()
        assert cuerpo["siguiente_en_lista_espera"]["paciente_id"] == escenario.carla_id

    def test_asistencia_solo_acepta_sus_estados(self, cliente: TestClient, cita_id: int) -> None:
        respuesta = cliente.post(
            f"/citas/{cita_id}/asistencia", json={"estado": "cancelada"}, headers=CABECERAS
        )
        assert respuesta.status_code == 422

    def test_atender_genera_el_cargo_en_la_respuesta(
        self, cliente: TestClient, cita_id: int
    ) -> None:
        cliente.post(f"/citas/{cita_id}/confirmar", headers=CABECERAS)
        cliente.post(
            f"/citas/{cita_id}/asistencia", json={"estado": "en_espera"}, headers=CABECERAS
        )
        cuerpo = cliente.post(
            f"/citas/{cita_id}/asistencia", json={"estado": "atendida"}, headers=CABECERAS
        ).json()
        assert cuerpo["genero_cargo"] is True
        assert cuerpo["cargo"]["concepto"] == "cuota_moderadora"
        assert "A charge of" in cuerpo["mensaje"]

    def test_reprogramar_devuelve_la_cita_nueva(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        cuerpo = cliente.post(
            f"/citas/{cita_id}/reprogramar",
            json={"nuevo_slot_id": escenario.slots_general[2], "motivo": "Choque"},
            headers=CABECERAS,
        ).json()
        assert cuerpo["cita"]["id"] != cita_id
        assert cuerpo["cita"]["cita_origen_id"] == cita_id
        assert cuerpo["cita"]["estado"] == "agendada"


class TestListaEsperaApi:
    def test_inscribe_y_lista_en_orden(self, cliente: TestClient, escenario: Escenario) -> None:
        cliente.post(
            "/lista-espera",
            json={"paciente_id": escenario.ana_id, "especialidad": "ortodoncia"},
        )
        cliente.post(
            "/lista-espera",
            json={
                "paciente_id": escenario.carla_id,
                "especialidad": "ortodoncia",
                "prioridad": "urgencia",
            },
        )
        cuerpo = cliente.get("/lista-espera", params={"especialidad": "ortodoncia"}).json()
        assert [e["paciente_id"] for e in cuerpo] == [escenario.carla_id, escenario.ana_id]

    def test_inscripcion_duplicada_es_409(self, cliente: TestClient, escenario: Escenario) -> None:
        cuerpo = {"paciente_id": escenario.ana_id, "especialidad": "ortodoncia"}
        cliente.post("/lista-espera", json=cuerpo)
        respuesta = cliente.post("/lista-espera", json=cuerpo)
        assert respuesta.status_code == 409
        assert respuesta.json()["codigo"] == "YA_EN_LISTA_ESPERA"

    def test_ofrecer_devuelve_a_quien_contactar(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        cliente.post(
            "/lista-espera",
            json={"paciente_id": escenario.ana_id, "especialidad": "ortodoncia"},
        )
        cuerpo = cliente.post(
            "/lista-espera/ofrecer",
            json={"slot_id": escenario.slots_orto[0]},
            headers=CABECERAS,
        ).json()
        assert cuerpo["paciente_id"] == escenario.ana_id
        assert cuerpo["telefono"].startswith("+57")
        assert "Contact" in cuerpo["mensaje"]

    def test_ofrecer_con_lista_vacia_es_404(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        respuesta = cliente.post("/lista-espera/ofrecer", json={"slot_id": escenario.slots_orto[0]})
        assert respuesta.status_code == 404
        assert respuesta.json()["codigo"] == "LISTA_ESPERA_VACIA"


class TestClinico:
    def test_con_consentimiento_registra_el_motivo(self, cliente: TestClient, cita_id: int) -> None:
        cuerpo = cliente.post(
            f"/citas/{cita_id}/motivo",
            json={"motivo": "Dolor en molar inferior derecho"},
            headers=CABECERAS,
        ).json()
        assert cuerpo["motivo"] == "Dolor en molar inferior derecho"

    def test_sin_consentimiento_es_403(
        self, cliente: TestClient, sesion_api: Session, escenario: Escenario
    ) -> None:
        cita = agendar_cita(
            sesion_api,
            paciente_id=escenario.carla_id,
            slot_id=escenario.slots_general[1],
            usuario="setup",
        ).cita
        sesion_api.commit()
        respuesta = cliente.post(
            f"/citas/{cita.id}/motivo", json={"motivo": "Dolor"}, headers=CABECERAS
        )
        assert respuesta.status_code == 403
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "CONSENTIMIENTO_REQUERIDO"
        assert "2654" in cuerpo["sugerencia"]

    def test_un_motivo_muy_corto_es_422(self, cliente: TestClient, cita_id: int) -> None:
        assert cliente.post(f"/citas/{cita_id}/motivo", json={"motivo": "a"}).status_code == 422


class TestContratoDeErrores:
    """Every failure the API can produce shares one shape."""

    @pytest.mark.parametrize(
        ("metodo", "ruta", "cuerpo"),
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
        cliente: TestClient,
        escenario: Escenario,
        metodo: str,
        ruta: str,
        cuerpo: dict[str, str] | None,
    ) -> None:
        respuesta = (
            getattr(cliente, metodo)(ruta, json=cuerpo)
            if cuerpo
            else getattr(cliente, metodo)(ruta)
        )
        assert respuesta.status_code == 404
        datos = respuesta.json()
        assert datos["error"] is True
        assert datos["codigo"]
        assert datos["mensaje"]
        assert datos["sugerencia"]

    def test_un_422_usa_la_misma_envoltura_y_nombra_los_campos(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        """FastAPI's default 422 body is a second error shape. It must not
        escape: one envelope, or the caller has to branch on which one it got."""
        respuesta = cliente.post("/citas", json={"paciente_id": "no-es-un-numero"})
        assert respuesta.status_code == 422
        cuerpo = respuesta.json()
        assert "detail" not in cuerpo
        assert cuerpo["error"] is True
        assert cuerpo["codigo"] == "ENTRADA_INVALIDA"
        campos = {c["campo"] for c in cuerpo["detalles"]["campos"]}
        assert "body.paciente_id" in campos
        assert "body.slot_id" in campos
        assert "paciente_id" in cuerpo["sugerencia"]

    def test_ninguna_respuesta_de_error_filtra_internos(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        crudo = cliente.get("/citas/999999").text
        assert "Traceback" not in crudo
        assert "sqlalchemy" not in crudo.lower()


class TestFlujoCompleto:
    def test_agendar_confirmar_cancelar_y_ofrecer_el_cupo(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        """The end-to-end path a receptionist actually walks."""
        slot = cliente.get("/disponibilidad", params={"especialidad": "ortodoncia"}).json()[0]

        creada = cliente.post(
            "/citas",
            json={"paciente_id": escenario.ana_id, "slot_id": slot["slot_id"]},
            headers=CABECERAS,
        ).json()["cita"]

        confirmada = cliente.post(f"/citas/{creada['id']}/confirmar", headers=CABECERAS).json()
        assert confirmada["estado_nuevo"] == "confirmada"

        cliente.post(
            "/lista-espera",
            json={"paciente_id": escenario.carla_id, "especialidad": "ortodoncia"},
        )

        cancelada = cliente.post(
            f"/citas/{creada['id']}/cancelar",
            json={"motivo": "El paciente tuvo una urgencia"},
            headers=CABECERAS,
        ).json()
        assert cancelada["libero_cupo"] is True
        assert cancelada["siguiente_en_lista_espera"]["paciente_id"] == escenario.carla_id

        oferta = cliente.post(
            "/lista-espera/ofrecer", json={"slot_id": slot["slot_id"]}, headers=CABECERAS
        ).json()
        assert oferta["paciente_id"] == escenario.carla_id

        detalle = cliente.get(f"/citas/{creada['id']}").json()
        assert [h["estado_nuevo"] for h in detalle["historial"]] == [
            "agendada",
            "confirmada",
            "cancelada",
        ]
        assert all(h["usuario"] in {"mcp-server", ACTOR} for h in detalle["historial"])


class TestSlotReservable:
    """The endpoint the tool layer calls before proposing a booking."""

    def test_un_cupo_libre_se_devuelve_con_su_hora_local(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        cuerpo = cliente.get(f"/disponibilidad/{escenario.slots_general[0]}").json()
        assert cuerpo["slot_id"] == escenario.slots_general[0]
        assert cuerpo["inicio_local"].startswith(str(escenario.fecha_futura))
        assert cuerpo["profesional"] == "Dra. General"

    def test_un_cupo_ocupado_es_409_con_alternativas(
        self, cliente: TestClient, cita_id: int, escenario: Escenario
    ) -> None:
        respuesta = cliente.get(f"/disponibilidad/{escenario.slots_general[0]}")
        assert respuesta.status_code == 409
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "SLOT_NO_DISPONIBLE"
        assert cuerpo["detalles"]["alternativas"]

    def test_un_cupo_pasado_es_400(self, cliente: TestClient, escenario: Escenario) -> None:
        respuesta = cliente.get(f"/disponibilidad/{escenario.slot_pasado_id}")
        assert respuesta.status_code == 400
        assert respuesta.json()["codigo"] == "SLOT_EN_EL_PASADO"

    def test_un_cupo_inexistente_es_404(self, cliente: TestClient, escenario: Escenario) -> None:
        assert cliente.get("/disponibilidad/999999").status_code == 404

    def test_falla_por_las_mismas_razones_que_agendar(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        """Both paths run the same validation, so a proposal that passes here
        cannot be rejected for a different reason on confirmation."""
        for slot_id in (escenario.slot_pasado_id, 999999):
            chequeo = cliente.get(f"/disponibilidad/{slot_id}")
            agendado = cliente.post(
                "/citas",
                json={"paciente_id": escenario.ana_id, "slot_id": slot_id},
                headers=CABECERAS,
            )
            assert chequeo.status_code == agendado.status_code
            assert chequeo.json()["codigo"] == agendado.json()["codigo"]


class TestTransicionesValidas:
    """The appointment detail tells the model what it can do next."""

    def test_una_cita_agendada_lista_sus_salidas(self, cliente: TestClient, cita_id: int) -> None:
        cuerpo = cliente.get(f"/citas/{cita_id}").json()
        assert set(cuerpo["transiciones_validas"]) == {
            "confirmada",
            "cancelada",
            "reprogramada",
            "no_asistio",
        }

    def test_una_cita_cancelada_no_tiene_salidas(self, cliente: TestClient, cita_id: int) -> None:
        cliente.post(
            f"/citas/{cita_id}/cancelar",
            json={"motivo": "El paciente viajó"},
            headers=CABECERAS,
        )
        assert cliente.get(f"/citas/{cita_id}").json()["transiciones_validas"] == []

    def test_las_salidas_siguen_a_la_transicion(self, cliente: TestClient, cita_id: int) -> None:
        cliente.post(f"/citas/{cita_id}/confirmar", headers=CABECERAS)
        assert set(cliente.get(f"/citas/{cita_id}").json()["transiciones_validas"]) == {
            "en_espera",
            "cancelada",
            "reprogramada",
            "no_asistio",
        }


class TestValidarReservaCompleta:
    """`/disponibilidad/{slot_id}` answers the same question `POST /citas` does."""

    def test_detecta_el_cruce_de_horario_del_paciente(
        self, cliente: TestClient, sesion_api: Session, escenario: Escenario
    ) -> None:
        """The other reason a booking fails, and the one that used to surface
        only on confirmation: the patient is already booked at that hour with a
        different professional."""
        agendar_cita(
            sesion_api,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        )
        sesion_api.commit()

        respuesta = cliente.get(
            f"/disponibilidad/{escenario.slots_orto[0]}",
            params={"paciente_id": escenario.ana_id},
        )
        assert respuesta.status_code == 409
        assert respuesta.json()["codigo"] == "PACIENTE_YA_TIENE_CITA"

    def test_sin_paciente_no_comprueba_el_cruce(
        self, cliente: TestClient, sesion_api: Session, escenario: Escenario
    ) -> None:
        agendar_cita(
            sesion_api,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        )
        sesion_api.commit()
        assert cliente.get(f"/disponibilidad/{escenario.slots_orto[0]}").status_code == 200

    def test_excluir_cita_permite_reprogramar_sobre_uno_mismo(
        self, cliente: TestClient, sesion_api: Session, escenario: Escenario
    ) -> None:
        cita = agendar_cita(
            sesion_api,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        ).cita
        sesion_api.commit()

        # The overlapping slot belongs to the appointment being moved, so with
        # the exclusion it must not count as a conflict.
        respuesta = cliente.get(
            f"/disponibilidad/{escenario.slots_orto[0]}",
            params={"paciente_id": escenario.ana_id, "excluir_cita_id": cita.id},
        )
        assert respuesta.status_code == 200

    def test_verifica_la_especialidad_esperada(
        self, cliente: TestClient, escenario: Escenario
    ) -> None:
        respuesta = cliente.get(
            f"/disponibilidad/{escenario.slots_general[0]}",
            params={"especialidad_esperada": "ortodoncia"},
        )
        assert respuesta.status_code == 400
        assert respuesta.json()["codigo"] == "ESPECIALIDAD_NO_COINCIDE"

    def test_coincide_con_lo_que_hace_agendar(
        self, cliente: TestClient, sesion_api: Session, escenario: Escenario
    ) -> None:
        """The invariant that makes propose-time checking trustworthy: both
        paths refuse for the same code."""
        agendar_cita(
            sesion_api,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        )
        sesion_api.commit()

        chequeo = cliente.get(
            f"/disponibilidad/{escenario.slots_orto[0]}",
            params={"paciente_id": escenario.ana_id},
        )
        agendado = cliente.post(
            "/citas",
            json={"paciente_id": escenario.ana_id, "slot_id": escenario.slots_orto[0]},
            headers=CABECERAS,
        )
        assert chequeo.status_code == agendado.status_code == 409
        assert chequeo.json()["codigo"] == agendado.json()["codigo"]
