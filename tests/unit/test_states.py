"""State machine tests.

The whole 7x7 transition space is enumerated: there is no legal transition that
is not asserted legal, and no illegal one that is not asserted illegal. That
exhaustiveness is the point: a state machine tested by example always grows an
untested edge.
"""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.errors import ErrorCode, InvalidTransition, ReasonRequired
from backend.domain.states import (
    TRANSITIONS,
    TRANSITIONS_CREATING_CHARGE,
    TRANSITIONS_FREEING_SLOT,
    TRANSITIONS_REQUIRING_REASON,
    is_final,
    is_valid_transition,
    reachable_states,
    validate_transition,
)
from backend.enums import FINAL_STATES, AppointmentState

ALL_STATES = list(AppointmentState)

#: The transitions the sector standard (§2.1) declares legal, written out by
#: hand so the test does not simply mirror the implementation's table.
LEGAL: set[tuple[AppointmentState, AppointmentState]] = {
    (AppointmentState.SCHEDULED, AppointmentState.CONFIRMED),
    (AppointmentState.SCHEDULED, AppointmentState.CANCELLED),
    (AppointmentState.SCHEDULED, AppointmentState.RESCHEDULED),
    (AppointmentState.SCHEDULED, AppointmentState.NO_SHOW),
    (AppointmentState.CONFIRMED, AppointmentState.WAITING),
    (AppointmentState.CONFIRMED, AppointmentState.CANCELLED),
    (AppointmentState.CONFIRMED, AppointmentState.RESCHEDULED),
    (AppointmentState.CONFIRMED, AppointmentState.NO_SHOW),
    (AppointmentState.WAITING, AppointmentState.ATTENDED),
    (AppointmentState.WAITING, AppointmentState.CANCELLED),
}

ILLEGAL = {pair for pair in itertools.product(ALL_STATES, ALL_STATES) if pair not in LEGAL}


# --------------------------------------------------------------------- table


class TestTransitionTable:
    def test_the_table_covers_every_state(self) -> None:
        assert set(TRANSITIONS) == set(ALL_STATES)

    def test_the_table_matches_the_sector_standard(self) -> None:
        derived = {
            (origin, target) for origin, targets in TRANSITIONS.items() for target in targets
        }
        assert derived == LEGAL

    def test_final_states_have_no_exit(self) -> None:
        for status in FINAL_STATES:
            assert reachable_states(status) == frozenset()
            assert is_final(status)

    def test_non_final_states_do_have_an_exit(self) -> None:
        for status in set(ALL_STATES) - FINAL_STATES:
            assert reachable_states(status), f"{status} was left with no exits"

    def test_no_transition_points_at_itself(self) -> None:
        for origin, targets in TRANSITIONS.items():
            assert origin not in targets

    def test_every_state_is_reachable_from_scheduled(self) -> None:
        """No unreachable state: a state nothing can reach is dead code."""
        reachable = {AppointmentState.SCHEDULED}
        boundary = [AppointmentState.SCHEDULED]
        while boundary:
            current = boundary.pop()
            for next_up in TRANSITIONS[current]:
                if next_up not in reachable:
                    reachable.add(next_up)
                    boundary.append(next_up)
        assert reachable == set(ALL_STATES)


# ---------------------------------------------------------------- exhaustive


@pytest.mark.parametrize(("origin", "target"), sorted(LEGAL))
def test_every_legal_transition_is_accepted(
    origin: AppointmentState, target: AppointmentState
) -> None:
    assert is_valid_transition(origin, target)
    effects = validate_transition(origin, target, reason="motivo de prueba")
    assert effects.previous_status is origin
    assert effects.new_status is target
    assert effects.requires_audit is True


@pytest.mark.parametrize(("origin", "target"), sorted(ILLEGAL))
def test_every_illegal_transition_is_refused(
    origin: AppointmentState, target: AppointmentState
) -> None:
    assert not is_valid_transition(origin, target)
    with pytest.raises(InvalidTransition):
        validate_transition(origin, target, reason="motivo de prueba")


def test_the_legal_illegal_partition_covers_the_whole_space() -> None:
    assert len(LEGAL) + len(ILLEGAL) == len(ALL_STATES) ** 2 == 49


# ------------------------------------------------------------------- errores


class TestActionableErrors:
    def test_an_illegal_transition_lists_the_valid_ones(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.SCHEDULED, AppointmentState.ATTENDED)
        error = exc.value
        assert error.code is ErrorCode.INVALID_TRANSITION
        assert error.suggestion is not None
        # The suggestion must name every acceptable alternative, otherwise the
        # model has to guess, which is what layer 4 exists to prevent.
        for valid in TRANSITIONS[AppointmentState.SCHEDULED]:
            assert str(valid) in error.suggestion
        assert error.details["current_state"] == "scheduled"
        assert error.details["requested_state"] == "attended"

    def test_a_final_state_returns_its_own_code(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.ATTENDED, AppointmentState.CANCELLED)
        assert exc.value.code is ErrorCode.APPOINTMENT_IN_FINAL_STATE
        assert "book_appointment" in (exc.value.suggestion or "")

    def test_the_error_serialises_to_the_wire_shape(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            validate_transition(AppointmentState.WAITING, AppointmentState.CONFIRMED)
        payload = exc.value.to_dict()
        assert payload["error"] is True
        assert payload["code"] == "INVALID_TRANSITION"
        assert payload["message"]
        assert payload["suggestion"]
        assert "details" in payload


class TestReasonIsMandatory:
    @pytest.mark.parametrize(
        "origin", [AppointmentState.SCHEDULED, AppointmentState.CONFIRMED, AppointmentState.WAITING]
    )
    def test_cancelling_without_a_reason_fails(self, origin: AppointmentState) -> None:
        with pytest.raises(ReasonRequired) as exc:
            validate_transition(origin, AppointmentState.CANCELLED)
        assert exc.value.code is ErrorCode.REASON_REQUIRED
        assert "reason" in (exc.value.suggestion or "")

    @pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
    def test_a_blank_reason_does_not_count_as_a_reason(self, reason: str) -> None:
        with pytest.raises(ReasonRequired):
            validate_transition(
                AppointmentState.SCHEDULED, AppointmentState.CANCELLED, reason=reason
            )

    def test_cancelling_with_a_reason_passes(self) -> None:
        effects = validate_transition(
            AppointmentState.CONFIRMED, AppointmentState.CANCELLED, reason="El paciente viajó"
        )
        assert effects.releases_slot

    def test_only_cancelling_requires_a_reason(self) -> None:
        assert {AppointmentState.CANCELLED} == TRANSITIONS_REQUIRING_REASON
        # Every other legal transition must go through without one.
        for origin, target in LEGAL:
            if target is AppointmentState.CANCELLED:
                continue
            validate_transition(origin, target)


# ------------------------------------------------------------------- efectos


class TestEffects:
    @pytest.mark.parametrize(("origin", "target"), sorted(LEGAL))
    def test_the_effects_agree_with_the_tables(
        self, origin: AppointmentState, target: AppointmentState
    ) -> None:
        effects = validate_transition(origin, target, reason="x")
        assert effects.releases_slot is (target in TRANSITIONS_FREEING_SLOT)
        assert effects.genera_cargo is (target in TRANSITIONS_CREATING_CHARGE)

    def test_only_cancelling_triggers_the_waiting_list(self) -> None:
        # A reschedule moves the same patient and a no-show happens once the
        # slot has already elapsed: neither leaves a slot someone can take.
        assert validate_transition(
            AppointmentState.SCHEDULED, AppointmentState.CANCELLED, reason="x"
        ).triggers_waiting_list
        assert not validate_transition(
            AppointmentState.SCHEDULED, AppointmentState.RESCHEDULED
        ).triggers_waiting_list
        assert not validate_transition(
            AppointmentState.CONFIRMED, AppointmentState.NO_SHOW
        ).triggers_waiting_list

    def test_attending_creates_a_charge_and_holds_the_slot(self) -> None:
        effects = validate_transition(AppointmentState.WAITING, AppointmentState.ATTENDED)
        assert effects.genera_cargo
        assert not effects.releases_slot

    def test_the_effects_are_immutable(self) -> None:
        effects = validate_transition(AppointmentState.SCHEDULED, AppointmentState.CONFIRMED)
        with pytest.raises((AttributeError, TypeError)):
            effects.releases_slot = True  # type: ignore[misc]


# ------------------------------------------------------------ property-based

states = st.sampled_from(ALL_STATES)


class TestProperties:
    @given(origin=states, target=states)
    def test_the_validator_and_the_predicate_never_disagree(
        self, origin: AppointmentState, target: AppointmentState
    ) -> None:
        """The pure predicate and the raising validator must agree, always."""
        if is_valid_transition(origin, target):
            validate_transition(origin, target, reason="reason")
        else:
            with pytest.raises(InvalidTransition):
                validate_transition(origin, target, reason="reason")

    @given(origin=states, target=states)
    def test_no_input_produces_an_unexpected_exception(
        self, origin: AppointmentState, target: AppointmentState
    ) -> None:
        """Whatever happens, it is a typed domain error, never a bare crash."""
        try:
            validate_transition(origin, target)
        except (InvalidTransition, ReasonRequired) as error:
            assert error.code in {
                ErrorCode.INVALID_TRANSITION,
                ErrorCode.APPOINTMENT_IN_FINAL_STATE,
                ErrorCode.REASON_REQUIRED,
            }
            assert error.to_dict()["message"]

    @given(origin=states)
    def test_from_a_final_state_nothing_is_possible(self, origin: AppointmentState) -> None:
        if is_final(origin):
            for target in ALL_STATES:
                assert not is_valid_transition(origin, target)
