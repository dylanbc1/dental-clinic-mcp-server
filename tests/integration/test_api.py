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


class TestClinic:
    def test_returns_the_clinic_with_its_professionals(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get("/clinic").json()
        assert body["name"] == "Clínica Escenario"
        assert body["timezone_name"] == "America/Bogota"
        assert {p["specialty"] for p in body["professionals"]} == {
            "general_dentistry",
            "orthodontics",
        }


class TestPolicies:
    def test_exposes_the_cartera_rules(self, client: TestClient) -> None:
        body = client.get("/policies/cartera").json()
        assert body["charges_no_show"] is True
        assert body["payment_term_days"] == 30
        assert "general_dentistry" in body["particular_tariffs"]

    def test_states_the_no_blocking_rule_explicitly(self, client: TestClient) -> None:
        assert "never a block" in client.get("/policies/cartera").json()["note"]


class TestSearchPatients:
    def test_by_document(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/patients", params={"document_number": "11111111"}).json()
        assert [p["id"] for p in body] == [scenario.ana_id]

    def test_by_name(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/patients", params={"name": "bruno"}).json()
        assert body[0]["regimen"] == "subsidiado"

    def test_with_no_criterion_it_answers_a_structured_404(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.get("/patients")
        assert response.status_code == 404
        assert response.json()["code"] == "PATIENT_NOT_FOUND"

    def test_a_document_that_is_too_long_is_422(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        assert client.get("/patients", params={"document_number": "9" * 40}).status_code == 422

    def test_it_returns_no_clinical_data(self, client: TestClient, scenario: Scenario) -> None:
        """The read tool must not leak clinical fields into a lookup response."""
        body = client.get("/patients", params={"document_number": "11111111"}).json()[0]
        assert "reason" not in body
        assert "clinical_data_consent" not in body


class TestAffiliation:
    def test_active(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/affiliation").json()
        assert body["active"] is True
        assert body["blocks_booking"] is False

    def test_inactive_falls_back_to_particular(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get(f"/patients/{scenario.bruno_id}/affiliation").json()
        assert body["regimen"] == "subsidiado"
        assert body["effective_regimen"] == "particular"
        assert body["suggestion"]

    def test_nonexistent_patient_is_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/patients/999999/affiliation").status_code == 404


class TestCartera:
    def test_al_dia(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/cartera").json()
        assert body["status"] == "al_dia"
        assert body["charges"] == []

    def test_en_mora_with_detail(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get(f"/patients/{scenario.debtor_id}/cartera").json()
        assert body["status"] == "en_mora"
        assert body["overdue_total"] == "180000"
        assert body["above_alert_threshold"] is True
        assert len(body["charges"]) == 1
        assert body["ageing"]["61_90"] == "180000"


class TestAvailability:
    def test_returns_future_slots(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/availability").json()
        assert body
        assert all("start_local" in s for s in body)

    def test_exposes_local_time_and_utc(self, client: TestClient, scenario: Scenario) -> None:
        """The model reasons in local time; the system stores UTC. Both ship."""
        slot = client.get("/availability").json()[0]
        assert slot["start_utc"].endswith("Z") or "+00:00" in slot["start_utc"]
        assert slot["start_local"].startswith(str(scenario.future_date))

    def test_filters_by_specialty(self, client: TestClient, scenario: Scenario) -> None:
        body = client.get("/availability", params={"specialty": "orthodontics"}).json()
        assert all(s["specialty"] == "orthodontics" for s in body)

    def test_an_invalid_specialty_is_422(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/availability", params={"specialty": "astrologia"}).status_code == 422

    def test_a_nonexistent_professional_is_404(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        assert client.get("/availability", params={"professional_id": 999999}).status_code == 404


class TestAppointments:
    def test_detail_includes_the_history(self, client: TestClient, appointment_id: int) -> None:
        body = client.get(f"/appointments/{appointment_id}").json()
        assert body["status"] == "scheduled"
        assert len(body["history"]) == 1

    def test_nonexistent_appointment_is_404(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get("/appointments/999999")
        assert response.status_code == 404
        assert response.json()["code"] == "APPOINTMENT_NOT_FOUND"

    def test_listing_by_patient(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/patients/{scenario.ana_id}/appointments").json()
        assert [c["id"] for c in body] == [appointment_id]

    def test_the_listing_filters_by_range(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        far_off = (scenario.future_date + timedelta(days=30)).isoformat()
        body = client.get(
            f"/patients/{scenario.ana_id}/appointments", params={"since": far_off}
        ).json()
        assert body == []

    def test_the_day_agenda_summarises_by_status(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        body = client.get(f"/agenda/{scenario.future_date}").json()
        assert body["total"] == 1
        assert body["by_status"] == {"scheduled": 1}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


class TestBooking:
    def test_creates_the_appointment(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["appointment"]["status"] == "scheduled"
        assert body["appointment"]["created_by"] == ACTOR
        assert body["affiliation"]["active"] is True

    def test_without_an_actor_header_it_uses_the_default(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]},
        ).json()
        assert body["appointment"]["created_by"] == "mcp-server"

    def test_mora_travels_as_an_alert_not_an_error(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.debtor_id, "slot_id": scenario.slots_general[0]},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["cartera_alert"] is not None

    def test_taken_slot_answers_409_with_alternatives(
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
        assert body["details"]["alternatives"]

    def test_past_slot_answers_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.past_slot_id},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert response.json()["code"] == "SLOT_IN_THE_PAST"

    def test_invalid_ids_are_422(self, client: TestClient, scenario: Scenario) -> None:
        assert (
            client.post("/appointments", json={"patient_id": 0, "slot_id": -1}).status_code == 422
        )

    def test_idempotency_does_not_duplicate(self, client: TestClient, scenario: Scenario) -> None:
        body = {
            "patient_id": scenario.ana_id,
            "slot_id": scenario.slots_general[0],
            "idempotency_key": "abc-123",
        }
        first = client.post("/appointments", json=body, headers=HEADERS).json()
        second = client.post("/appointments", json=body, headers=HEADERS).json()
        assert second["appointment"]["id"] == first["appointment"]["id"]
        assert second["reused"] is True


class TestTransitions:
    def test_confirming(self, client: TestClient, appointment_id: int) -> None:
        body = client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS).json()
        assert body["previous_status"] == "scheduled"
        assert body["new_status"] == "confirmed"
        assert body["freed_slot"] is False

    def test_confirming_twice_is_409_with_the_alternatives(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        response = client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "INVALID_TRANSITION"
        assert "waiting" in body["suggestion"]

    def test_cancelling_requires_a_reason(self, client: TestClient, appointment_id: int) -> None:
        response = client.post(f"/appointments/{appointment_id}/cancel", json={})
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_INPUT"

    def test_an_empty_reason_does_not_pass_validation(
        self, client: TestClient, appointment_id: int
    ) -> None:
        assert (
            client.post(f"/appointments/{appointment_id}/cancel", json={"reason": "  "}).status_code
            == 422
        )

    def test_cancelling_frees_the_slot(self, client: TestClient, appointment_id: int) -> None:
        body = client.post(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "El paciente viajó"},
            headers=HEADERS,
        ).json()
        assert body["freed_slot"] is True
        assert "free again" in body["message"]

    def test_cancelling_warns_about_the_waiting_list(
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

    def test_attendance_only_accepts_its_own_states(
        self, client: TestClient, appointment_id: int
    ) -> None:
        response = client.post(
            f"/appointments/{appointment_id}/attendance",
            json={"status": "cancelled"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_attending_returns_the_charge_in_the_response(
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

    def test_rescheduling_returns_the_new_appointment(
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


class TestWaitingListApi:
    def test_enrols_and_lists_in_order(self, client: TestClient, scenario: Scenario) -> None:
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

    def test_a_duplicate_enrolment_is_409(self, client: TestClient, scenario: Scenario) -> None:
        body = {"patient_id": scenario.ana_id, "specialty": "orthodontics"}
        client.post("/waiting-list", json=body)
        response = client.post("/waiting-list", json=body)
        assert response.status_code == 409
        assert response.json()["code"] == "ALREADY_ON_WAITING_LIST"

    def test_offering_returns_who_to_contact(self, client: TestClient, scenario: Scenario) -> None:
        client.post(
            "/waiting-list",
            json={"patient_id": scenario.ana_id, "specialty": "orthodontics"},
        )
        body = client.post(
            "/waiting-list/offer",
            json={"slot_id": scenario.ortho_slots[0]},
            headers=HEADERS,
        ).json()
        assert body["patient_id"] == scenario.ana_id
        assert body["phone"].startswith("+57")
        assert "Contact" in body["message"]

    def test_offering_with_an_empty_list_is_404(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        response = client.post("/waiting-list/offer", json={"slot_id": scenario.ortho_slots[0]})
        assert response.status_code == 404
        assert response.json()["code"] == "WAITING_LIST_EMPTY"


class TestClinical:
    def test_with_consent_it_records_the_reason(
        self, client: TestClient, appointment_id: int
    ) -> None:
        body = client.post(
            f"/appointments/{appointment_id}/reason",
            json={"reason": "Dolor en molar inferior derecho"},
            headers=HEADERS,
        ).json()
        assert body["reason"] == "Dolor en molar inferior derecho"

    def test_without_consent_it_is_403(
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

    def test_a_reason_that_is_too_short_is_422(
        self, client: TestClient, appointment_id: int
    ) -> None:
        assert (
            client.post(f"/appointments/{appointment_id}/reason", json={"reason": "a"}).status_code
            == 422
        )


class TestErrorContract:
    """Every failure the API can produce shares one shape."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "/patients/999999/affiliation", None),
            ("get", "/patients/999999/cartera", None),
            ("get", "/appointments/999999", None),
            ("post", "/appointments/999999/confirm", None),
            ("post", "/appointments/999999/cancel", {"reason": "prueba manual"}),
        ],
    )
    def test_every_404_carries_code_message_and_suggestion(
        self,
        client: TestClient,
        scenario: Scenario,
        method: str,
        path: str,
        body: dict[str, str] | None,
    ) -> None:
        response = (
            getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        )
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"] is True
        assert payload["code"]
        assert payload["message"]
        assert payload["suggestion"]

    def test_a_422_uses_the_same_envelope_and_names_the_fields(
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

    def test_no_error_response_leaks_internals(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        raw = client.get("/appointments/999999").text
        assert "Traceback" not in raw
        assert "sqlalchemy" not in raw.lower()


class TestFullFlow:
    def test_book_confirm_cancel_and_offer_the_slot(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """The end-to-end path a receptionist actually walks."""
        slot = client.get("/availability", params={"specialty": "orthodontics"}).json()[0]

        created = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": slot["slot_id"]},
            headers=HEADERS,
        ).json()["appointment"]

        confirmed = client.post(f"/appointments/{created['id']}/confirm", headers=HEADERS).json()
        assert confirmed["new_status"] == "confirmed"

        client.post(
            "/waiting-list",
            json={"patient_id": scenario.carla_id, "specialty": "orthodontics"},
        )

        cancelled = client.post(
            f"/appointments/{created['id']}/cancel",
            json={"reason": "El paciente tuvo una urgencia"},
            headers=HEADERS,
        ).json()
        assert cancelled["freed_slot"] is True
        assert cancelled["next_in_waiting_list"]["patient_id"] == scenario.carla_id

        offer = client.post(
            "/waiting-list/offer", json={"slot_id": slot["slot_id"]}, headers=HEADERS
        ).json()
        assert offer["patient_id"] == scenario.carla_id

        detail = client.get(f"/appointments/{created['id']}").json()
        assert [h["new_status"] for h in detail["history"]] == [
            "scheduled",
            "confirmed",
            "cancelled",
        ]
        assert all(h["user"] in {"mcp-server", ACTOR} for h in detail["history"])


class TestBookableSlot:
    """The endpoint the tool layer calls before proposing a booking."""

    def test_a_free_slot_is_returned_with_its_local_time(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        body = client.get(f"/availability/{scenario.slots_general[0]}").json()
        assert body["slot_id"] == scenario.slots_general[0]
        assert body["start_local"].startswith(str(scenario.future_date))
        assert body["professional"] == "Dra. General"

    def test_a_taken_slot_is_409_with_alternatives(
        self, client: TestClient, appointment_id: int, scenario: Scenario
    ) -> None:
        response = client.get(f"/availability/{scenario.slots_general[0]}")
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "SLOT_UNAVAILABLE"
        assert body["details"]["alternatives"]

    def test_a_past_slot_is_400(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get(f"/availability/{scenario.past_slot_id}")
        assert response.status_code == 400
        assert response.json()["code"] == "SLOT_IN_THE_PAST"

    def test_a_nonexistent_slot_is_404(self, client: TestClient, scenario: Scenario) -> None:
        assert client.get("/availability/999999").status_code == 404

    def test_fails_for_the_same_reasons_as_booking(
        self, client: TestClient, scenario: Scenario
    ) -> None:
        """Both paths run the same validation, so a proposal that passes here
        cannot be rejected for a different reason on confirmation."""
        for slot_id in (scenario.past_slot_id, 999999):
            check = client.get(f"/availability/{slot_id}")
            booked = client.post(
                "/appointments",
                json={"patient_id": scenario.ana_id, "slot_id": slot_id},
                headers=HEADERS,
            )
            assert check.status_code == booked.status_code
            assert check.json()["code"] == booked.json()["code"]


class TestValidTransitions:
    """The appointment detail tells the model what it can do next."""

    def test_a_scheduled_appointment_lists_its_exits(
        self, client: TestClient, appointment_id: int
    ) -> None:
        body = client.get(f"/appointments/{appointment_id}").json()
        assert set(body["valid_transitions"]) == {
            "confirmed",
            "cancelled",
            "rescheduled",
            "no_show",
        }

    def test_a_cancelled_appointment_has_no_exits(
        self, client: TestClient, appointment_id: int
    ) -> None:
        client.post(
            f"/appointments/{appointment_id}/cancel",
            json={"reason": "El paciente viajó"},
            headers=HEADERS,
        )
        assert client.get(f"/appointments/{appointment_id}").json()["valid_transitions"] == []

    def test_the_exits_follow_the_transition(self, client: TestClient, appointment_id: int) -> None:
        client.post(f"/appointments/{appointment_id}/confirm", headers=HEADERS)
        assert set(client.get(f"/appointments/{appointment_id}").json()["valid_transitions"]) == {
            "waiting",
            "cancelled",
            "rescheduled",
            "no_show",
        }


class TestFullBookingValidation:
    """`/availability/{slot_id}` answers the same question `POST /appointments` does."""

    def test_detects_the_patients_overlapping_appointment(
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
            f"/availability/{scenario.ortho_slots[0]}",
            params={"patient_id": scenario.ana_id},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "PATIENT_ALREADY_BOOKED"

    def test_without_a_patient_it_does_not_check_the_overlap(
        self, client: TestClient, api_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            api_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        api_session.commit()
        assert client.get(f"/availability/{scenario.ortho_slots[0]}").status_code == 200

    def test_excluding_the_appointment_allows_rescheduling_onto_itself(
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
            f"/availability/{scenario.ortho_slots[0]}",
            params={"patient_id": scenario.ana_id, "exclude_appointment_id": appointment.id},
        )
        assert response.status_code == 200

    def test_it_checks_the_expected_specialty(self, client: TestClient, scenario: Scenario) -> None:
        response = client.get(
            f"/availability/{scenario.slots_general[0]}",
            params={"expected_specialty": "orthodontics"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "SPECIALTY_MISMATCH"

    def test_matches_what_booking_does(
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

        check = client.get(
            f"/availability/{scenario.ortho_slots[0]}",
            params={"patient_id": scenario.ana_id},
        )
        booked = client.post(
            "/appointments",
            json={"patient_id": scenario.ana_id, "slot_id": scenario.ortho_slots[0]},
            headers=HEADERS,
        )
        assert check.status_code == booked.status_code == 409
        assert check.json()["code"] == booked.json()["code"]
