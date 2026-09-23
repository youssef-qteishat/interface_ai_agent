"""
Tests for the typed spine.

These are not "does Pydantic work" tests. Each one pins a decision that something
downstream depends on: that a disabled member cannot be executed, that an uncounted
locator is distinguishable from an unmatched one, that a step can never silently lose
the explanation of what it clicked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.domain.actions import (
    DISABLED_MEMBERS,
    ENABLED_MEMBERS,
    GoalComplete,
    LeftClick,
    is_terminal,
    parse_action,
    parse_computer_action,
    terminal_tool_schemas,
)
from src.domain.results import (
    BusinessOutcomeCode,
    BusinessOutcomeResult,
    Escalated,
    EscalationReason,
    Failure,
    FailureCode,
    StopReason,
    Success,
)
from src.domain.trace import Observation, ProbeResult, RecordedStep, RunTrace

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# actions: the first safety gate
# --------------------------------------------------------------------------- #


def test_valid_click_parses():
    action = parse_action({"kind": "left_click", "coordinate": [640, 400]})
    assert isinstance(action, LeftClick)
    assert action.coordinate == (640, 400)


def test_negative_coordinate_is_rejected():
    with pytest.raises(ValidationError):
        parse_action({"kind": "left_click", "coordinate": [-5, 10]})


def test_unknown_kind_is_rejected():
    with pytest.raises(ValidationError):
        parse_action({"kind": "teleport", "coordinate": [1, 1]})


@pytest.mark.parametrize("member", DISABLED_MEMBERS)
def test_disabled_members_are_rejected_by_schema(member: str):
    """The Step 0 vocabulary is enforced by the type system, not just by policy.

    A disabled member has no model at all, so it can never be executed even if the
    policy engine were misconfigured.
    """
    with pytest.raises(ValidationError):
        parse_action({"kind": member, "coordinate": [10, 10]})


def test_extra_field_is_rejected():
    """A malformed action is caught whole rather than half-applied."""
    with pytest.raises(ValidationError):
        parse_action({"kind": "type", "text": "hello", "unexpected": 1})


def test_wait_accepts_api_ceiling_but_not_beyond():
    parse_action({"kind": "wait", "duration": 300})
    with pytest.raises(ValidationError):
        parse_action({"kind": "wait", "duration": 301})


def test_click_accepts_modifier_text():
    """Anthropic sends held modifiers in `text`; parsing must not choke on it."""
    action = parse_action({"kind": "left_click", "coordinate": [1, 2], "text": "ctrl"})
    assert action.text == "ctrl"


def test_terminal_declarations_parse_and_are_flagged():
    action = parse_action({"kind": "goal_complete", "summary": "Reached review"})
    assert isinstance(action, GoalComplete)
    assert is_terminal(action)
    assert not is_terminal(parse_action({"kind": "screenshot"}))


def test_parse_computer_action_refuses_terminal_declarations():
    """Call sites that must receive something executable cannot be handed a
    declaration by mistake."""
    with pytest.raises(ValidationError):
        parse_computer_action({"kind": "goal_complete", "summary": "done"})


def test_enabled_members_all_parse():
    samples = {
        "screenshot": {},
        "zoom": {"region": [0, 0, 100, 100]},
        "left_click": {"coordinate": [1, 1]},
        "double_click": {"coordinate": [1, 1]},
        "type": {"text": "12345"},
        "key": {"text": "Return"},
        "scroll": {"scroll_direction": "down", "scroll_amount": 3},
        "wait": {"duration": 1.0},
        "cursor_position": {},
    }
    assert set(samples) == set(ENABLED_MEMBERS)
    for kind, payload in samples.items():
        assert parse_action({"kind": kind, **payload}).kind == kind


def test_terminal_tool_schemas_are_wire_ready():
    schemas = terminal_tool_schemas()
    assert [s["name"] for s in schemas] == [
        "goal_complete",
        "business_outcome",
        "request_human",
        "cannot_proceed",
    ]
    for schema in schemas:
        assert schema["description"]
        props = schema["input_schema"]["properties"]
        # `kind` is our local discriminator; the API knows the tool by name.
        assert "kind" not in props
    outcome = next(s for s in schemas if s["name"] == "business_outcome")
    assert "code" in outcome["input_schema"]["required"]


# --------------------------------------------------------------------------- #
# results: outcomes stay distinguishable
# --------------------------------------------------------------------------- #


def test_each_result_variant_carries_run_id():
    for result in (
        Success(run_id="r1", checkpoint_verified=True),
        BusinessOutcomeResult(run_id="r1", code=BusinessOutcomeCode.MEMBER_NOT_FOUND),
        Failure(run_id="r1", code=FailureCode.CHECKPOINT_FAILED),
        Escalated(run_id="r1", intervention_id="i1", reason=EscalationReason.UNKNOWN_DIALOG),
    ):
        assert result.run_id == "r1"
        assert result.status in {"success", "business_outcome", "failure", "escalated"}


def test_unknown_failure_code_is_rejected():
    """Step 11 cannot invent a spelling that the caller has never heard of."""
    with pytest.raises(ValidationError):
        Failure(run_id="r1", code="SOMETHING_WENT_WRONG")


def test_success_does_not_claim_verification_by_default():
    """A model saying 'done' is not verification; that has to be set deliberately."""
    assert Success(run_id="r1").checkpoint_verified is False


def test_failure_carries_expected_and_observed():
    failure = Failure(
        run_id="r1",
        code=FailureCode.CHECKPOINT_FAILED,
        step_index=9,
        expected={"heading": "Review New Account"},
        observed={"heading": "Open Sub-Account"},
        evidence=["steps/009-after.png"],
        stop_reason=StopReason.TERMINAL_DECLARATION,
    )
    assert failure.expected != failure.observed
    assert failure.evidence


# --------------------------------------------------------------------------- #
# trace: the contract the canonicalizer reads
# --------------------------------------------------------------------------- #


def test_example_trace_round_trips_unchanged():
    original = RunTrace.from_yaml((FIXTURES / "example_trace.yaml").read_text())
    reparsed = RunTrace.from_yaml(original.to_yaml())
    assert reparsed.model_dump() == original.model_dump()
    assert reparsed.run_id == "run_20260917_141230_a3f1"
    assert len(reparsed.steps) == 2


def test_null_match_count_is_not_zero():
    """An uncounted candidate and an unmatched candidate are different facts; only
    one of them means 'do not use this locator'."""
    probe = ProbeResult.model_validate(
        {
            "coordinate": [1, 1],
            "tag": "input",
            "candidates": [
                {"kind": "text", "tag": "button", "text": "Save", "match_count": None},
                {"kind": "text", "tag": "button", "text": "Cancel", "match_count": 0},
            ],
        }
    )
    assert probe.candidates[0].match_count is None
    assert probe.candidates[1].match_count == 0

    # And the distinction must survive serialization, since the canonicalizer reads
    # YAML rather than these objects.
    dumped = probe.model_dump(mode="json")
    assert dumped["candidates"][0]["match_count"] is None
    assert dumped["candidates"][1]["match_count"] == 0


def test_candidate_missing_required_field_is_rejected():
    with pytest.raises(ValidationError):
        ProbeResult.model_validate(
            {"coordinate": [1, 1], "tag": "button", "candidates": [{"kind": "css"}]}
        )


def test_unknown_candidate_kind_is_rejected():
    """Adding a kind to probe.js requires a matching model — deliberate friction for a
    contract the canonicalizer matches exhaustively."""
    with pytest.raises(ValidationError):
        ProbeResult.model_validate(
            {"coordinate": [1, 1], "tag": "button", "candidates": [{"kind": "vibes"}]}
        )


def test_probe_and_probe_unavailable_are_mutually_exclusive():
    base = {
        "index": 0,
        "action": {"kind": "left_click", "coordinate": [1, 1]},
        "policy": {"decision": "allow", "risk": "reversible", "rule": "action_allowlist"},
    }
    with pytest.raises(ValidationError):
        RecordedStep.model_validate(
            {
                **base,
                "probe": {"coordinate": [1, 1], "tag": "button"},
                "probe_unavailable": "no_accessibility_backend",
            }
        )


def test_step_without_probe_is_valid_but_reported():
    """A null probe is allowed (the cross-surface case) — the recorder's assertion is
    what insists on an explanation."""
    trace = RunTrace.from_yaml((FIXTURES / "example_trace.yaml").read_text())
    unexplained = trace.steps_missing_probe()
    assert unexplained == [], "fixture steps each carry a probe or a stated reason"

    trace.steps.append(
        RecordedStep.model_validate(
            {
                "index": 9,
                "action": {"kind": "left_click", "coordinate": [5, 5]},
                "policy": {"decision": "allow", "risk": "reversible", "rule": "action_allowlist"},
            }
        )
    )
    assert trace.steps_missing_probe() == [9]


def test_ambiguity_is_detectable_from_the_probe():
    """Two 'Back' buttons: every candidate matches twice, so nothing is usable as-is."""
    probe = ProbeResult.model_validate(
        {
            "coordinate": [233, 239],
            "tag": "button",
            "role": "button",
            "accessible_name": "Back",
            "candidates": [
                {"kind": "role", "role": "button", "name": "Back", "match_count": 2},
                {"kind": "text", "tag": "button", "text": "Back", "match_count": 2},
            ],
        }
    )
    assert probe.is_ambiguous
    assert probe.best_candidate is None


def test_placeholder_derived_name_is_recorded_as_such():
    probe = ProbeResult.model_validate(
        {
            "coordinate": [739, 124],
            "tag": "input",
            "role": "textbox",
            "accessible_name": "$0.00",
            "accessible_name_source": "placeholder",
            "placeholder": "$0.00",
            "candidates": [
                {"kind": "attribute", "selector": 'input[name="opening_amount"]',
                 "attribute": "name", "value": "opening_amount", "match_count": 1},
            ],
        }
    )
    assert probe.accessible_name_source == "placeholder"
    # The usable locator is the attribute one, not the name.
    assert probe.best_candidate.kind == "attribute"


def test_observation_exposes_frame_urls_for_policy():
    trace = RunTrace.from_yaml((FIXTURES / "example_trace.yaml").read_text())
    before = trace.steps[0].observation_before
    assert before is not None
    assert before.frame_urls == [
        "http://bank-sim:8001/servicing/accounts/open?member_id=12345"
    ]


def test_yaml_keeps_declaration_order():
    """A trace is read by humans; alphabetised keys would scatter each step's story."""
    trace = RunTrace.from_yaml((FIXTURES / "example_trace.yaml").read_text())
    dumped = trace.to_yaml()
    assert dumped.index("run_id") < dumped.index("goal") < dumped.index("steps")


# --------------------------------------------------------------------------- #
# the models vs. reality
# --------------------------------------------------------------------------- #
#
# The fixtures below were captured verbatim from a running surface agent. They exist
# because the first version of Observation was modelled on the plan's sketch and was
# missing `visible_text`, which the agent had been returning all along. A hand-written
# fixture would never have caught that; these keep the check permanent and offline.


def test_live_probe_payload_validates_without_massaging():
    probe = ProbeResult.model_validate_json((FIXTURES / "live_probe.json").read_text())
    assert probe.tag == "input"
    assert probe.frame_path == ["servicing-frame"]
    # The Member ID input genuinely has no accessible name — its label carries no
    # `for=` and does not wrap it. The ladder has to work anyway.
    assert probe.accessible_name is None
    assert probe.nearby_label == "Member ID"
    assert {c.kind for c in probe.candidates} >= {"contextual_text", "attribute"}
    assert probe.dom_id_stability == "generated"


def test_live_button_probe_has_role_and_name():
    probe = ProbeResult.model_validate_json((FIXTURES / "live_probe_button.json").read_text())
    assert probe.role == "button"
    assert probe.accessible_name == "Search"
    assert probe.accessible_name_source == "visible_text"
    assert probe.best_candidate is not None


def test_live_observe_payload_validates():
    obs = Observation.model_validate_json((FIXTURES / "live_observe.json").read_text())
    assert obs.main_frame_url == "http://bank-sim:8001/"
    assert obs.frame_urls[-1].endswith("/servicing/members/search")
    assert obs.headings == ["Member Search"]
    assert obs.dom_hash and obs.dom_hash.startswith("sha256:")
    assert obs.visible_text and "Member Search" in obs.visible_text
