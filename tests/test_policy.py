"""
Policy engine tests.

These pin the refusals the safety story rests on. The two that matter most:

  * the irreversible control cannot be pressed, and an approval for one action does
    not transfer to another;
  * a refused action never reaches the adapter — the gate sits before dispatch, not
    as a label attached afterwards.
"""

from __future__ import annotations

import time

import pytest

from src.domain.actions import CursorPosition, LeftClick, Screenshot, Type, Wait
from src.domain.trace import Observation, PolicyDecision, ProbeResult
from src.policy.engine import ApprovalToken, PolicyEngine, action_digest

RUN_ID = "run_test"
DECLARED = {"member_id": "12345", "account_type": "savings", "opening_amount": "25.00"}


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine(run_id=RUN_ID, declared_inputs=DECLARED)


def observation(*urls: str) -> Observation:
    """An observation whose frames sit at the given URLs."""
    return Observation.model_validate(
        {
            "main_frame_url": urls[0] if urls else None,
            "frames": [{"path": [], "url": u} for u in urls],
        }
    )


SAFE_SCREEN = "http://bank-sim:8001/servicing/members/search"


def probe_of(name: str | None = None, classes: list[str] | None = None,
             tag: str = "button") -> ProbeResult:
    return ProbeResult.model_validate(
        {
            "coordinate": [100, 100],
            "tag": tag,
            "role": "button",
            "accessible_name": name,
            "classes": classes or [],
        }
    )


OPEN_ACCOUNT_PROBE = probe_of("Open Account", ["danger-button"])


# --------------------------------------------------------------------------- #
# the irreversible control
# --------------------------------------------------------------------------- #


def test_open_account_click_is_escalated_not_allowed(engine: PolicyEngine):
    """The model can see the button. It cannot press it."""
    click = LeftClick(coordinate=(695, 460))
    decision = engine.check_target(click, OPEN_ACCOUNT_PROBE)
    assert decision.decision == "escalate"
    assert decision.code == "IRREVERSIBLE_REQUIRES_APPROVAL"
    assert decision.risk == "irreversible"


def test_irreversible_is_detected_by_class_alone(engine: PolicyEngine):
    """tenant_b could rename the label; the class still gives it away."""
    decision = engine.check_target(
        LeftClick(coordinate=(1, 1)), probe_of("Proceed", ["danger-button"])
    )
    assert decision.decision == "escalate"


def test_irreversible_is_detected_by_name_alone(engine: PolicyEngine):
    """A CSS refactor could rename the class; the accessible name still gives it away."""
    decision = engine.check_target(
        LeftClick(coordinate=(1, 1)), probe_of("Open Account", ["btn", "primary"])
    )
    assert decision.decision == "escalate"


def test_valid_approval_token_allows_the_exact_action(engine: PolicyEngine):
    click = LeftClick(coordinate=(695, 460))
    token = ApprovalToken(
        run_id=RUN_ID,
        action_digest=action_digest(click),
        operator="teller1",
        expires_at=time.time() + 300,
    )
    decision = engine.check_target(click, OPEN_ACCOUNT_PROBE, approval=token)
    assert decision.decision == "allow"
    assert decision.rule == "approval_token"
    assert "teller1" in (decision.detail or "")


def test_approval_does_not_transfer_to_a_different_action(engine: PolicyEngine):
    """Approval is for ONE action. Otherwise it is just a switch that turns the
    engine off for a while."""
    approved = LeftClick(coordinate=(695, 460))
    token = ApprovalToken(
        run_id=RUN_ID,
        action_digest=action_digest(approved),
        operator="teller1",
        expires_at=time.time() + 300,
    )
    other_click = LeftClick(coordinate=(100, 200))
    decision = engine.check_target(other_click, OPEN_ACCOUNT_PROBE, approval=token)
    assert decision.decision == "escalate"


def test_expired_token_does_not_authorize(engine: PolicyEngine):
    click = LeftClick(coordinate=(695, 460))
    token = ApprovalToken(
        run_id=RUN_ID,
        action_digest=action_digest(click),
        operator="teller1",
        expires_at=time.time() - 1,
    )
    assert engine.check_target(click, OPEN_ACCOUNT_PROBE, approval=token).decision == "escalate"


def test_token_from_another_run_does_not_authorize(engine: PolicyEngine):
    click = LeftClick(coordinate=(695, 460))
    token = ApprovalToken(
        run_id="some-other-run",
        action_digest=action_digest(click),
        operator="teller1",
        expires_at=time.time() + 300,
    )
    assert engine.check_target(click, OPEN_ACCOUNT_PROBE, approval=token).decision == "escalate"


def test_ordinary_button_is_allowed(engine: PolicyEngine):
    decision = engine.check_target(
        LeftClick(coordinate=(1, 1)), probe_of("Continue", ["action-button"])
    )
    assert decision.decision == "allow"
    assert decision.risk == "reversible"


def test_click_without_a_probe_is_refused(engine: PolicyEngine):
    """No probe means the engine cannot rule out the commit button. Unknown is not
    the same as harmless."""
    decision = engine.check_target(LeftClick(coordinate=(1, 1)), None)
    assert decision.decision == "deny"
    assert decision.code == "TARGET_UNKNOWN"


# --------------------------------------------------------------------------- #
# origin and route
# --------------------------------------------------------------------------- #


def test_dev_route_is_denied(engine: PolicyEngine):
    """The network does NOT block /dev/ — this rule is the control, not a backup."""
    decision = engine.check_action(
        Screenshot(), observation("http://bank-sim:8001/dev/fault-profile")
    )
    assert decision.decision == "deny"
    assert decision.code == "ROUTE_NOT_ALLOWED"


def test_dev_route_in_a_child_frame_is_denied(engine: PolicyEngine):
    """The workflow lives in the iframe, so checking only the main frame would check
    the one frame that never changes."""
    decision = engine.check_action(
        Screenshot(),
        observation("http://bank-sim:8001/", "http://bank-sim:8001/dev/audit-log"),
    )
    assert decision.decision == "deny"
    assert decision.code == "ROUTE_NOT_ALLOWED"


def test_foreign_origin_is_denied(engine: PolicyEngine):
    decision = engine.check_action(Screenshot(), observation("http://evil.example/steal"))
    assert decision.decision == "deny"
    assert decision.code == "ORIGIN_NOT_ALLOWED"


def test_unparseable_url_fails_closed(engine: PolicyEngine):
    decision = engine.check_action(Screenshot(), observation("not-a-url"))
    assert decision.decision == "deny"


def test_permitted_screen_is_allowed(engine: PolicyEngine):
    assert engine.check_action(Screenshot(), observation(SAFE_SCREEN)).decision == "allow"


# --------------------------------------------------------------------------- #
# vocabulary and typed input
# --------------------------------------------------------------------------- #


def test_action_outside_the_vocabulary_is_denied():
    """Second gate: even if the Step 4 schema were bypassed, policy refuses."""
    engine = PolicyEngine(
        run_id=RUN_ID, declared_inputs=DECLARED, allowed_action_kinds=("screenshot",)
    )
    decision = engine.check_action(LeftClick(coordinate=(1, 1)), observation(SAFE_SCREEN))
    assert decision.decision == "deny"
    assert decision.code == "ACTION_NOT_ALLOWED"


def test_declared_input_value_may_be_typed(engine: PolicyEngine):
    assert engine.check_action(Type(text="12345"), observation(SAFE_SCREEN)).decision == "allow"
    assert engine.check_action(Type(text="25.00"), observation(SAFE_SCREEN)).decision == "allow"


def test_undeclared_value_cannot_be_typed(engine: PolicyEngine):
    """The prompt-injection story: a value that was never an input cannot be entered,
    no matter what the page told the model to do."""
    decision = engine.check_action(
        Type(text="sk-ant-api03-secret"), observation(SAFE_SCREEN)
    )
    assert decision.decision == "deny"
    assert decision.code == "TYPED_VALUE_NOT_DECLARED"


def test_refusal_does_not_echo_the_rejected_value(engine: PolicyEngine):
    """The rejected text may be exactly the secret an injected instruction was after;
    logging it in the decision would leak it into the trace."""
    secret = "sk-ant-api03-supersecret"
    decision = engine.check_action(Type(text=secret), observation(SAFE_SCREEN))
    assert secret not in (decision.detail or "")


def test_another_members_id_cannot_be_typed(engine: PolicyEngine):
    """Only the member this run declared — not any well-formed member id."""
    decision = engine.check_action(Type(text="99999"), observation(SAFE_SCREEN))
    assert decision.decision == "deny"


# --------------------------------------------------------------------------- #
# shape of every decision
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action",
    [Screenshot(), CursorPosition(), Wait(duration=1.0)],
)
def test_read_only_actions_are_allowed_and_classified(engine: PolicyEngine, action):
    decision = engine.check_target(action, None)
    assert decision.decision == "allow"
    assert decision.risk == "read_only"


def test_every_decision_names_a_rule_and_validates(engine: PolicyEngine):
    decisions = [
        engine.check_action(Screenshot(), observation(SAFE_SCREEN)),
        engine.check_action(Screenshot(), observation("http://evil.example/")),
        engine.check_action(Type(text="nope"), observation(SAFE_SCREEN)),
        engine.check_target(LeftClick(coordinate=(1, 1)), OPEN_ACCOUNT_PROBE),
        engine.check_target(LeftClick(coordinate=(1, 1)), probe_of("Continue")),
    ]
    for decision in decisions:
        assert decision.rule, "a decision with no rule is unauditable"
        # Must survive the round trip into the trace.
        assert PolicyDecision.model_validate(decision.model_dump())


def test_digest_is_stable_and_action_specific():
    a = LeftClick(coordinate=(695, 460))
    b = LeftClick(coordinate=(695, 460))
    c = LeftClick(coordinate=(695, 461))
    assert action_digest(a) == action_digest(b)
    assert action_digest(a) != action_digest(c)


# --------------------------------------------------------------------------- #
# the gate sits before dispatch
# --------------------------------------------------------------------------- #


class RecordingAdapter:
    """Stands in for the surface. If policy works, it never hears about a refusal."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def act(self, action: object) -> None:
        self.calls.append(action)


def dispatch(engine: PolicyEngine, adapter: RecordingAdapter, action, obs, probe=None):
    """The controller's contract in miniature: check, then act only on allow."""
    first = engine.check_action(action, obs)
    if first.decision != "allow":
        return first
    second = engine.check_target(action, probe)
    if second.decision != "allow":
        return second
    adapter.act(action)
    return second


def test_refused_actions_never_reach_the_adapter(engine: PolicyEngine):
    adapter = RecordingAdapter()
    refusals = [
        (Type(text="sk-ant-secret"), observation(SAFE_SCREEN), None),
        (Screenshot(), observation("http://bank-sim:8001/dev/fault-profile"), None),
        (LeftClick(coordinate=(1, 1)), observation(SAFE_SCREEN), OPEN_ACCOUNT_PROBE),
    ]
    for action, obs, probe in refusals:
        assert dispatch(engine, adapter, action, obs, probe).decision != "allow"
    assert adapter.calls == [], "a refused action reached the surface"


def test_allowed_action_does_reach_the_adapter(engine: PolicyEngine):
    adapter = RecordingAdapter()
    click = LeftClick(coordinate=(550, 96))
    decision = dispatch(
        engine, adapter, click, observation(SAFE_SCREEN), probe_of("Search")
    )
    assert decision.decision == "allow"
    assert adapter.calls == [click]
