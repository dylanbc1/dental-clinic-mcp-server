"""Write tools over MRTR, end to end on the wire.

The property under test throughout: **the first call changes nothing**. It comes
back asking a person, and only the retry carrying their answer mutates anything.
Every test checks the database afterwards rather than trusting the tool's word.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.services import book_appointment, join_waiting_list
from backend.enums import AppointmentState, SlotState, Specialty
from backend.models import AgendaSlot, Appointment
from tests.conftest import SUBJECT, MCPTestClient, Scenario, ToolCallError, as_caller

pytestmark = pytest.mark.integration

WRITE = ["read", "write"]


def count_appointments(session_: Session) -> int:
    return session_.scalar(select(func.count()).select_from(Appointment)) or 0


@pytest.fixture
def existing_appointment(backend_session: Session, scenario: Scenario) -> int:
    result = book_appointment(
        backend_session,
        patient_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        user="setup",
    )
    backend_session.commit()
    return result.appointment.id


class TestTheFirstCallDoesNotExecute:
    async def test_booking_asks_for_confirmation_and_creates_nothing(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)

        assert question["resultType"] == "input_required"
        assert question["requestState"]
        assert count_appointments(backend_session) == 0
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_the_question_describes_what_will_happen(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Read aloud to a receptionist, so it names the hour and the
        professional rather than a slot id."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            message = mcp.question_text(await mcp.ask("book_appointment", args))

        assert str(scenario.future_date) in message
        assert "Dra. General" in message
        assert "Esto va a pasar:" in message
        assert "¿Confirmas la operación?" in message

    async def test_the_question_asks_for_a_boolean_and_nothing_else(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """A form with more fields invites changing the operation instead of
        judging it."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
        key = next(iter(question["inputRequests"]))
        schema = question["inputRequests"][key]["params"]["requestedSchema"]
        assert set(schema["properties"]) == {"confirmed"}
        assert schema["properties"]["confirmed"]["type"] == "boolean"

    async def test_warns_about_the_inactive_affiliation(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.bruno_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            message = mcp.question_text(await mcp.ask("book_appointment", args))
        assert "inactiva" in message
        assert "tarifa particular" in message

    async def test_warns_about_mora_without_blocking(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.debtor_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
        message = mcp.question_text(question)
        assert "mora" in message
        assert "No impide agendar" in message
        assert question["requestState"], "the operation is still available to approve"

    async def test_cancelling_asks_without_cancelling(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        args = {"appointment_id": existing_appointment, "reason": "El paciente viajó"}
        with as_caller(SUBJECT, WRITE):
            await mcp.ask("cancel_appointment", args)
        backend_session.expire_all()
        assert (
            backend_session.get(Appointment, existing_appointment).status
            is AppointmentState.SCHEDULED
        )


class TestTheSecondCallExecutes:
    async def test_the_full_cycle_really_books(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            result = await mcp.approve("book_appointment", args)

        assert result["appointment"]["status"] == "scheduled"
        assert count_appointments(backend_session) == 1

    async def test_the_tokens_actor_lands_in_the_backend_audit(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """The audit row must name the human's subject, not "mcp-server"."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller("dra.ospina@clinica.test", WRITE):
            result = await mcp.approve("book_appointment", args)
        assert result["appointment"]["history"][0]["user"] == "dra.ospina@clinica.test"

    async def test_cancelling_frees_the_slot(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"appointment_id": existing_appointment, "reason": "El paciente viajó"}
        with as_caller(SUBJECT, WRITE):
            result = await mcp.approve("cancel_appointment", args)
        assert result["freed_slot"] is True
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_confirming(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, WRITE):
            result = await mcp.approve(
                "confirm_appointment", {"appointment_id": existing_appointment}
            )
        assert result["new_status"] == "confirmed"
        backend_session.expire_all()
        assert (
            backend_session.get(Appointment, existing_appointment).status
            is AppointmentState.CONFIRMED
        )

    async def test_record_attendance_creates_the_charge(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        from backend.domain.services import confirm_appointment, record_attendance

        confirm_appointment(backend_session, existing_appointment, user="setup")
        record_attendance(
            backend_session, existing_appointment, AppointmentState.WAITING, user="setup"
        )
        backend_session.commit()

        with as_caller(SUBJECT, WRITE):
            result = await mcp.approve(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        assert result["created_charge"] is True
        assert result["charge"]["concept"] == "cuota_moderadora"

    async def test_rescheduling_chains_the_new_appointment(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"appointment_id": existing_appointment, "new_slot_id": scenario.slots_general[2]}
        with as_caller(SUBJECT, WRITE):
            result = await mcp.approve("reschedule_appointment", args)
        assert result["appointment"]["source_appointment_id"] == existing_appointment
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_offering_a_slot_contacts_without_booking(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        join_waiting_list(
            backend_session,
            patient_id=scenario.carla_id,
            specialty=Specialty.ORTHODONTICS,
        )
        backend_session.commit()

        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask(
                "offer_slot_to_waiting_list", {"slot_id": scenario.ortho_slots[0]}
            )
            assert "NO se agenda" in mcp.question_text(question)
            offer = await mcp.respond(
                "offer_slot_to_waiting_list",
                {"slot_id": scenario.ortho_slots[0]},
                question,
            )

        assert offer["patient_id"] == scenario.carla_id
        assert offer["phone"]
        assert count_appointments(backend_session) == 0


class TestWhenThePersonSaysNo:
    async def test_an_explicit_false_aborts_without_touching_anything(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question, confirmed=False)

        assert "OPERACION_NO_APROBADA" in exc.value.text_of
        assert "Nothing was changed" in exc.value.text_of
        assert count_appointments(backend_session) == 0

    async def test_the_refusal_asks_not_to_retry(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Retrying an operation a person declined is how an agent nags."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question, confirmed=False)
        assert "Do not retry" in exc.value.text_of

    async def test_declining_the_elicitation_also_aborts(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        """The client can decline instead of answering. The call must stop."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", args, question, action="decline")
        assert count_appointments(backend_session) == 0


class TestValidatesBeforeAsking:
    """A question a human cannot act on is worse than an error.

    Every check here is repeated on the second round, because the resolver runs
    again. These exist so nobody is asked to approve something that will fail.
    """

    async def test_it_does_not_ask_about_an_already_taken_slot(
        self, mcp: MCPTestClient, scenario: Scenario, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.carla_id, "slot_id": scenario.slots_general[0]},
            )
        assert "SLOT_UNAVAILABLE" in exc.value.text_of
        assert "closest free slots" in exc.value.text_of

    async def test_it_does_not_ask_about_a_slot_in_the_past(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.ana_id, "slot_id": scenario.past_slot_id},
            )
        assert "SLOT_IN_THE_PAST" in exc.value.text_of

    async def test_it_does_not_ask_about_an_overlapping_appointment(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            backend_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        backend_session.commit()
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.ana_id, "slot_id": scenario.ortho_slots[0]},
            )
        assert "PATIENT_ALREADY_BOOKED" in exc.value.text_of

    async def test_it_does_not_ask_about_an_impossible_transition(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        assert "INVALID_TRANSITION" in exc.value.text_of
        assert "confirmed" in exc.value.text_of

    async def test_an_invented_attendance_state_is_refused(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "cancelled"}
            )
        assert "is not an attendance state" in exc.value.text_of

    async def test_a_nonexistent_appointment_fails_before_asking(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp.ask("confirm_appointment", {"appointment_id": 424242})
        assert "APPOINTMENT_NOT_FOUND" in exc.value.text_of


class TestValidationRepeatsOnExecution:
    async def test_approval_does_not_legalise_what_stopped_being_legal(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        """The state can change between the two rounds.

        The resolver runs again on the retry, so the domain refuses even though
        the human approved. Approval authorises an action; it does not freeze the
        world it saw.
        """
        from backend.domain.services import cancel_appointment

        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask(
                "confirm_appointment", {"appointment_id": existing_appointment}
            )

            cancel_appointment(
                backend_session, existing_appointment, reason="urgencia", user="otro"
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond(
                    "confirm_appointment", {"appointment_id": existing_appointment}, question
                )
        assert "INVALID_TRANSITION" in exc.value.text_of or "estado final" in exc.value.text_of

    async def test_a_slot_taken_in_between_is_detected(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)

            book_appointment(
                backend_session,
                patient_id=scenario.carla_id,
                slot_id=scenario.slots_general[0],
                user="otro",
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question)
        assert "SLOT_UNAVAILABLE" in exc.value.text_of


class TestAudit:
    async def test_the_question_and_the_execution_are_both_recorded(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            await mcp.approve("book_appointment", args)

        results = [e["result"] for e in ctx.auditor.events if e["event"] == "tool.invocation"]
        # MRTR means two calls arrive per mutation, and the log records calls.
        assert results.count("input_required") == 2
        assert results[-1] == "ok"

    async def test_the_execution_is_marked_as_approved(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            await mcp.approve("book_appointment", args)
        assert ctx.auditor.events[-1]["with_human_approval"] is True

    async def test_a_validation_refusal_lands_in_the_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        """A log that records only what succeeded cannot tell you an agent spent
        an hour asking for something impossible."""
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError):
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        event = ctx.auditor.events[-1]
        assert event["result"] == "error"
        assert event["error_code"] == "INVALID_TRANSITION"

    async def test_the_reason_is_not_copied_into_the_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        secreto = "sangrado persistente desde el martes"
        with as_caller(SUBJECT, WRITE):
            await mcp.ask(
                "cancel_appointment", {"appointment_id": existing_appointment, "reason": secreto}
            )
        assert secreto not in str(ctx.auditor.events)
        assert ctx.auditor.events[-1]["arguments"]["reason"] == "«redacted»"

    async def test_the_request_state_is_not_copied_into_the_log(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        """A logged request state is a redeemable approval sitting in a log."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask("book_appointment", args)
        assert question["requestState"] not in str(ctx.auditor.events)


class TestAClientThatCannotConfirm:
    """Not every client speaks 2026-07-28 yet, and one that does not deserves to
    be told which half of this server it can still use."""

    async def test_writes_are_refused_with_a_useful_message(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("book_appointment", args)

        message = exc.value.text_of
        assert "CLIENT_CANNOT_CONFIRM" in message
        # Not a transport error the reader cannot act on.
        assert "back-channel" not in message
        assert "elicitation" in message
        assert "Read tools work" in message

    async def test_reads_keep_working(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, WRITE):
            patients = await mcp_without_elicitation.call_tool(
                "search_patients", {"document_number": scenario.ana_document}
            )
        assert [p["id"] for p in patients] == [scenario.ana_id]

    async def test_the_clinic_is_refused_as_well(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, [*WRITE, "clinical"]), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool(
                "record_visit_reason", {"appointment_id": 1, "reason": "dolor"}
            )
        assert "CLIENT_CANNOT_CONFIRM" in exc.value.text_of

    async def test_the_refusal_names_the_negotiated_protocol(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        """So the reader can tell an old client from a misconfigured one."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, WRITE), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("book_appointment", args)
        assert "negotiated_protocol" in exc.value.text_of


@pytest.mark.anyio
class TestTheHumanQuestionLeaksNoInternalValues:
    """The question a receptionist approves is Spanish. State values, error
    codes and tool names are English. Neither belongs inside the other, so no
    internal value may appear in the text a person reads."""

    async def test_no_internal_value_appears_in_the_question(
        self, mcp: MCPTestClient, scenario: Scenario, existing_appointment: int
    ) -> None:
        proposed = (
            # A slot the `existing_appointment` fixture has not taken.
            (
                "book_appointment",
                {"patient_id": scenario.bruno_id, "slot_id": scenario.slots_general[1]},
            ),
            (
                "cancel_appointment",
                {"appointment_id": existing_appointment, "reason": "el paciente viaja"},
            ),
            ("confirm_appointment", {"appointment_id": existing_appointment}),
            # `no_show` is reachable straight from `scheduled`; `waiting` is not.
            ("record_attendance", {"appointment_id": existing_appointment, "status": "no_show"}),
        )
        internal_values = {s.value for s in AppointmentState} | {e.value for e in Specialty}
        for tool_name, args in proposed:
            with as_caller(SUBJECT, WRITE):
                question = await mcp.ask(tool_name, args)
            text = mcp.question_text(question)
            leaked = {v for v in internal_values if v in text}
            assert not leaked, f"{tool_name} showed {leaked} to a person"

    async def test_the_state_that_does_matter_is_shown_in_spanish(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        """Attendance is the one place the state carries information for the
        front desk, so it is rendered through the label map."""
        with as_caller(SUBJECT, WRITE):
            question = await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "no_show"}
            )
        assert "no asistió" in mcp.question_text(question)
