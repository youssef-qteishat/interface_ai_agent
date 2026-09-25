"""
Engine, extraction and recovery tests.

The ones that earn their place are the safety ones. A replay engine that executes a reviewed artifact is
easy to make *look* safe — the artifact was reviewed, after all — and the two tests that would catch it
being decoration are:

  * the policy vocabulary still refuses a kind outside the artifact's five, so the gate is configured
    rather than removed;
  * `check_target` runs on the **resolved element**, so a button that has become `danger-button` since the
    artifact was written is refused. The artifact cannot know that; only the live element can say.

Everything here is offline. The live runs live in `tests/test_replay_live.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.domain.artifact import OutcomeReturn, load_artifact
from src.domain.results import EscalationReason, FailureCode, StopReason
from src.domain.trace import (
    Budget,
    DialogInfo,
    Display,
    FrameInfo,
    Observation,
    ProbeResult,
    ProviderInfo,
    RunTrace,
)
from src.policy.engine import PolicyEngine
from src.replay.engine import PARKED, REPLAY_ACTION_KINDS, Bounds, ReplayEngine
from src.replay.extract import ExtractionError, apply_transform, mask_member_id, strip_currency

ARTIFACT = Path("artifacts/open_subaccount_review.yaml")
INPUTS = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}
# The review table as rows, so the text the conditions read and the values extraction pulls out cannot
# disagree with each other.
REVIEW_ROWS = {
    "Member:": "23456",
    "Account Type:": "Savings",
    "Opening Amount:": "$50.00",
    "Funding Source:": "****-0153",
}
REVIEW_TABLE = " ".join(f"{k} {v}" for k, v in REVIEW_ROWS.items())


@pytest.fixture(scope="module")
def capability():
    return load_artifact(ARTIFACT)


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, masked",
    [("23456", "***56"), ("12345", "***45"), ("7", "*"), ("", "")],
)
def test_masking_keeps_only_the_last_two_digits(raw, masked):
    """The output is declared `sensitive: true`. Enough to confirm which member, not enough to be one."""
    assert mask_member_id(raw) == masked


@pytest.mark.parametrize(
    "raw, stripped",
    [("$50.00", "50.00"), ("$1,250.00", "1250.00"), ("125.50", "125.50")],
)
def test_currency_stripping_leaves_the_number(raw, stripped):
    assert strip_currency(raw) == stripped


def test_a_value_with_no_number_is_an_error_not_an_empty_string():
    with pytest.raises(ExtractionError):
        strip_currency("—")


def test_an_unimplemented_transform_raises_rather_than_leaking_the_raw_value():
    """The loud-failure case that matters: a silently unapplied `mask_member_id` returns a real member id
    to a caller while the contract says it is masked. Passing the raw value through on an unknown name
    would make that the default behaviour."""
    with pytest.raises(ExtractionError, match="not implemented"):
        apply_transform("mask_account_number", "****-0153")
    # And a field with no transform is untouched, which is not the same thing as an unknown one.
    assert apply_transform(None, "Savings") == "Savings"


def test_every_transform_the_committed_spec_names_is_implemented(capability):
    """Driven off the contract, so adding a transform to a spec without implementing it fails here."""
    from src.replay.extract import TRANSFORMS

    named = {
        field.transform
        for output in capability.contract.outputs.values()
        for field in output.extract.fields.values()
        if field.transform
    }
    assert named and named <= set(TRANSFORMS), f"unimplemented: {named - set(TRANSFORMS)}"


# --------------------------------------------------------------------------- #
# a fake surface: enough to run the engine without a browser
# --------------------------------------------------------------------------- #


def tag_of(candidate_hint: str) -> str:
    """The tag a candidate is asking for, inferred from how it asks.

    The resolver verifies the resolved element against `evidence.expected_tag`, and that check is one of
    the things worth keeping real in these tests — a fake that answered `button` for everything would make
    the engine look like it resolved nine steps when it had actually failed the first.
    """
    hint = candidate_hint.lower()
    for tag in ("select", "textarea", "input", "button", "tr", "td"):
        if tag in hint:
            return tag
    return "button"


class FakeLocator:
    def __init__(self, surface: FakeSurface, selector: str = "") -> None:
        self.surface = surface
        self.selector = selector

    async def count(self) -> int:
        return 1

    async def evaluate(self, expression: str) -> Any:
        tag = self.surface.tag or tag_of(self.selector)
        if "tagName" in expression and "classList" not in expression:
            return tag
        return {
            "tag": tag,
            "classes": list(self.surface.classes),
            "text": self.surface.element_text,
            "name": self.surface.element_name,
            "type": None,
            "nameAttr": None,
            "domId": None,
            "disabled": False,
        }

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self) -> str:
        """A single cell when the selector names a row label, the whole region otherwise.

        Extraction asks for one cell at a time (`.//td[...='Member:']/following-sibling::td[1]`), and a
        fake that handed back the whole table would have `mask_member_id` masking every digit in it — which
        is exactly what it did before this.
        """
        for label, value in self.surface.rows.items():
            if f"'{label}'" in self.selector:
                return value
        return self.surface.scoped

    def locator(self, selector: str, **_: Any) -> FakeLocator:
        return FakeLocator(self.surface, selector)

    @property
    def first(self) -> FakeLocator:
        return self


class FakeFrame:
    def __init__(self, surface: FakeSurface) -> None:
        self.surface = surface

    def locator(self, selector: str, **_: Any) -> FakeLocator:
        return FakeLocator(self.surface, selector)

    def get_by_role(self, role: str = "", **__: Any) -> FakeLocator:
        # `textbox` is the ARIA role of an `<input>`; the tag and the role are different vocabularies,
        # which is exactly why the resolver checks the tag and not the role.
        return FakeLocator(self.surface, "input" if role == "textbox" else role)

    def get_by_label(self, *_: Any, **__: Any) -> FakeLocator:
        return FakeLocator(self.surface, "input")


class FakeSurface:
    """Answers every read the engine makes, and records every action it dispatches.

    `dispatched` is what the safety tests assert on: a refused step must leave it empty, because a policy
    decision that is recorded but not enforced is the failure mode worth testing for.
    """

    def __init__(
        self,
        *,
        tag: str = "",
        classes: tuple[str, ...] = (),
        element_text: str = "Continue",
        element_name: str = "",
        text: str | None = None,
        headings: tuple[str, ...] | None = None,
        dialogs: tuple[str, ...] = (),
        overlays: tuple[str, ...] = (),
        scoped: str = REVIEW_TABLE,
        rows: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        # Empty means "whatever the candidate asked for"; set it to force a mismatch.
        self.tag = tag
        self.classes = classes
        self.element_text = element_text
        self.element_name = element_name
        self.text = text
        self.headings = headings
        self.dialogs = dialogs
        self.overlays = overlays
        self.scoped = scoped
        self.rows = dict(rows or REVIEW_ROWS)
        self.url = url
        self.dispatched: list[tuple[str, Any]] = []
        self.filled: dict[str, str] = {}
        self.last_filled = ""
        self.paused = False

    # reads
    async def observe(self, frame_path: Any = None) -> Observation:
        url, headings = self._screen()
        return Observation(
            main_frame_url="http://127.0.0.1:8001/",
            frames=[
                FrameInfo(path=[], url="http://127.0.0.1:8001/"),
                FrameInfo(path=["servicing-frame"], url=self.url or url),
            ],
            # The default text carries the dispatch count so it *changes* after an action, which is what
            # the absence gate in `wait_for` polls on. A test that pins `text=` gets it verbatim.
            visible_text=self.text
            if self.text is not None
            else f"screen-{len(self.dispatched)} {REVIEW_TABLE}",
            headings=list(self.headings if self.headings is not None else headings),
            dialogs=[DialogInfo(selector=".modal-overlay", text=t) for t in self.dialogs],
            overlays=list(self.overlays),
        )

    def _screen(self) -> tuple[str, tuple[str, ...]]:
        """Which screen the workflow is on, by how many actions have been dispatched.

        Sequential rather than permissive: a fake that satisfied every postcondition at once would let a
        broken step order pass. The thresholds are the artifact's own — `results-panel` lands on Member
        Detail, `open-sub-account` on the form, and the review panel only appears after Continue.
        """
        done = len(self.dispatched)
        base = "http://127.0.0.1:8001/servicing"
        if done >= 8:
            return f"{base}/accounts/open?member_id=23456", ("Open Sub-Account", "Review New Account")
        if done >= 4:
            return f"{base}/accounts/open?member_id=23456", ("Open Sub-Account",)
        if done >= 3:
            return f"{base}/members/23456", ("Member Detail — 23456",)
        return f"{base}/members/search", ("Member Search",)

    def frame(self, frame_path: Any = None) -> FakeFrame:
        return FakeFrame(self)

    async def scoped_text(self, _frame_path: Any, _selector: str) -> str:
        return self.scoped

    async def input_value(self, locator: Any, **_: Any) -> str:
        # Both `value` postconditions assert exactly what was filled, so the fake echoes it back.
        return self.filled.get(getattr(locator, "selector", ""), self.last_filled)

    async def is_checked(self, _locator: Any, **_: Any) -> bool:
        return True

    async def describe_element(self, locator: Any, frame_path: Any = None) -> ProbeResult:
        from src.surfaces.playwright_web import ReplaySurface

        return await ReplaySurface.describe_element(self, locator, frame_path)  # type: ignore[arg-type]

    def _assert_active(self) -> None:
        return None

    async def capture_evidence(self) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake"

    # writes
    async def click(self, locator: Any, **_: Any) -> None:
        self.dispatched.append(("click", getattr(locator, "selector", "")))

    async def fill(self, locator: Any, value: str, **_: Any) -> None:
        self.dispatched.append(("fill", value))
        self.filled[getattr(locator, "selector", "")] = value
        self.last_filled = value

    async def select(self, locator: Any, value: str, **_: Any) -> None:
        self.dispatched.append(("select", value))

    async def check(self, locator: Any, **_: Any) -> None:
        self.dispatched.append(("check", None))

    async def read(self, locator: Any, **_: Any) -> str:
        self.dispatched.append(("read", None))
        return self.scoped

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False


class FakeWriter:
    """Records what the engine would persist, without touching a disk."""

    def __init__(self) -> None:
        self.run_id = "run_test"
        self.steps: list[Any] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.finished: Any = None

    def record_step(self, step: Any, **_: Any) -> Any:
        self.steps.append(step)
        return step

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        self.events.append((kind, fields))
        return fields

    def finish(self, _trace: Any, result: Any = None, **_: Any) -> None:
        self.finished = result


def build(capability, surface: FakeSurface, **kwargs: Any) -> ReplayEngine:
    writer = FakeWriter()
    trace = RunTrace(
        run_id=writer.run_id,
        goal="replay test",
        target="http://127.0.0.1:8001",
        display=Display(width=1280, height=800),
        provider=ProviderInfo(name="none", model="none"),
        budget=Budget(),
    )
    return ReplayEngine(
        surface=surface,
        policy=kwargs.pop(
            "policy",
            PolicyEngine(
                run_id=writer.run_id,
                allowed_origins=("http://127.0.0.1:8001",),
                declared_inputs=INPUTS,
                allowed_action_kinds=REPLAY_ACTION_KINDS,
            ),
        ),
        writer=writer,
        trace=trace,
        capability=capability,
        inputs=INPUTS,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# the safety tests
# --------------------------------------------------------------------------- #


def test_the_replay_vocabulary_is_exactly_the_artifact_s_action_kinds(capability):
    """Configured, not widened: every kind the artifact uses is allowed and nothing else is."""
    used = {s.action.kind for s in capability.steps}
    assert used <= REPLAY_ACTION_KINDS
    assert REPLAY_ACTION_KINDS == {"click", "fill", "select", "check", "read"}


@pytest.mark.asyncio
async def test_an_action_kind_outside_the_vocabulary_is_refused(capability):
    """The gate still fires. Replay passes its own vocabulary to the same engine, so a step kind nobody
    declared is denied exactly as an unknown computer-use verb is on the discovery side."""
    policy = PolicyEngine(
        allowed_origins=("http://127.0.0.1:8001",), allowed_action_kinds=REPLAY_ACTION_KINDS
    )
    from src.replay.engine import _Action

    decision = policy.check_action(_Action(kind="navigate"), await FakeSurface().observe())
    assert decision.decision == "deny"


@pytest.mark.asyncio
async def test_a_danger_button_is_escalated_and_never_clicked(capability):
    """The irreversible gate, at replay time.

    The artifact was reviewed against a page where `Continue` was safe. It cannot know the button now
    carries `danger-button` — only the resolved element can say, which is why `check_target` runs on
    `describe_element` rather than on what the artifact recorded.
    """
    surface = FakeSurface(classes=("action-button", "danger-button"))
    engine = build(capability, surface, bounds=Bounds(max_steps=50))

    result = await engine.run()

    assert result.status == "escalated"
    assert result.reason is EscalationReason.IRREVERSIBLE_REQUIRES_APPROVAL
    assert surface.dispatched == [], "the action must never be dispatched"


@pytest.mark.asyncio
async def test_a_control_named_open_account_is_escalated_too(capability):
    """The second, independent signal. Either alone is brittle: a tenant can rename the label and a CSS
    refactor can rename the class, so both are matched."""
    surface = FakeSurface(classes=(), element_text="Open Account")
    result = await build(capability, surface).run()

    assert result.status == "escalated"
    assert surface.dispatched == []


@pytest.mark.asyncio
async def test_a_refused_step_is_recorded_with_its_decision(capability):
    """Recorded either way. A refusal nobody can find in the evidence is indistinguishable from a step
    that never happened."""
    surface = FakeSurface(classes=("danger-button",))
    engine = build(capability, surface)
    await engine.run()

    assert engine.writer.steps, "the refused step should still be recorded"
    last = engine.writer.steps[-1]
    assert last.policy is not None and last.policy.decision == "escalate"
    assert all(s.policy is not None for s in engine.writer.steps)


# --------------------------------------------------------------------------- #
# outcomes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_clean_run_succeeds_with_outputs_extracted(capability):
    surface = FakeSurface()
    result = await build(capability, surface).run()

    assert result.status == "success", getattr(result, "observed", result)
    assert result.checkpoint_verified is True
    assert result.outputs["review"]["member"] == "***56", "the sensitive output must be masked"
    assert result.outputs["review"]["opening_amount"] == "50.00"
    # Every step with a target actually ran.
    assert len(surface.dispatched) == sum(
        1 for s in capability.steps if getattr(s.action, "target", None)
    )


@pytest.mark.asyncio
async def test_the_app_refusing_is_a_business_outcome_not_a_failure(capability):
    """`VALIDATION_REJECTED` is a `BusinessOutcomeCode`. The committed artifact paired it with
    `status: failure` until the engine tried to build a `Failure` out of it and found the code is not a
    `FailureCode` at all — a rule that could never execute, sitting in a reviewed artifact."""
    rule = next(
        r for r in capability.outcome_rules
        if r.return_ and r.return_.code == "VALIDATION_REJECTED"
    )
    assert rule.return_.status == "business_outcome"

    surface = FakeSurface(text="You must accept the account disclosure to continue.")
    result = await build(capability, surface).run()
    assert result.status == "business_outcome"
    assert result.code == "VALIDATION_REJECTED"


def test_an_outcome_return_cannot_pair_a_status_with_the_wrong_vocabulary():
    """The validator that would have caught the above when the artifact loaded, rather than when the rule
    happened to fire."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="FailureCode"):
        OutcomeReturn(status="failure", code="VALIDATION_REJECTED")
    with pytest.raises(pydantic.ValidationError, match="BusinessOutcomeCode"):
        OutcomeReturn(status="business_outcome", code="NO_PROGRESS")
    # And the right pairings still build.
    assert OutcomeReturn(status="business_outcome", code="MEMBER_NOT_FOUND").code
    assert OutcomeReturn(status="escalated", reason="UNKNOWN_DIALOG").reason


@pytest.mark.asyncio
async def test_a_not_found_member_is_a_domain_answer(capability):
    surface = FakeSurface(text="No members found. Check the Member ID and try again.")
    result = await build(capability, surface).run()

    assert result.status == "business_outcome"
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.stop_reason is StopReason.TERMINAL_DECLARATION


@pytest.mark.asyncio
async def test_a_failed_checkpoint_reports_expected_against_observed(capability):
    """What makes a failure diagnosable without re-running it."""
    surface = FakeSurface(scoped="Member: 23456 Account Type: Checking Opening Amount: $50.00")
    result = await build(capability, surface).run()

    assert result.status == "failure"
    assert result.code is FailureCode.CHECKPOINT_FAILED
    assert result.expected and result.observed


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_step_limit_stops_the_run(capability):
    result = await build(capability, FakeSurface(), bounds=Bounds(max_steps=3)).run()
    assert result.status == "failure"
    assert result.code is FailureCode.MAX_STEPS_EXCEEDED
    assert result.stop_reason is StopReason.MAX_STEPS


@pytest.mark.asyncio
async def test_the_wall_clock_stops_the_run(capability):
    result = await build(capability, FakeSurface(), bounds=Bounds(wall_clock_s=-1)).run()
    assert result.status == "failure"
    assert result.code is FailureCode.WALL_CLOCK_EXCEEDED


@pytest.mark.asyncio
async def test_a_run_always_leaves_a_summary_even_when_it_crashes(capability):
    """A crash with no summary is the worst evidence outcome there is — the run is gone and the folder
    cannot say why. This was a real bug in the discovery layer."""

    class Exploding(FakeSurface):
        async def observe(self, frame_path: Any = None) -> Observation:
            raise RuntimeError("the browser went away")

    engine = build(capability, Exploding())
    with pytest.raises(RuntimeError):
        await engine.run()

    assert engine.writer.finished is not None
    assert engine.writer.finished.code is FailureCode.PROVIDER_ERROR


# --------------------------------------------------------------------------- #
# recovery
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_named_dialog_is_dismissed_and_the_run_continues(capability):
    """Discovery watched a human dismiss `System Notice`, so the canonicalizer wrote a `dismiss` rule and
    replay handles it without anyone being asked. That is the loop closing."""
    from src.replay.recovery import recover

    rule = next(
        r for r in capability.outcome_rules
        if r.when.kind == "dialog" and getattr(r.when, "contains", None)
    )
    surface = FakeSurface(dialogs=("⚠ System Notice ERR-APP_9f2",))

    # The dialog clears once it has been clicked, which is what the sim's own dismiss button does.
    original = surface.click

    async def clearing_click(locator: Any, **kw: Any) -> None:
        await original(locator, **kw)
        surface.dialogs = ()

    surface.click = clearing_click  # type: ignore[method-assign]

    recovered = await recover(surface, rule, capability, frame_path=["servicing-frame"], inputs=INPUTS)
    assert recovered.cleared and recovered.strategy == "dismiss"


@pytest.mark.asyncio
async def test_a_dialog_the_artifact_does_not_name_is_never_dismissed(capability):
    """The rule this layer exists for. Clicking an unidentified modal away might be clicking `Confirm`."""
    from src.domain.artifact import DialogCondition, OutcomeRule, Recover
    from src.replay.recovery import UnrecoverableDialog, recover

    rogue = OutcomeRule(
        when=DialogCondition(unmatched=True),
        recover=Recover(strategy="dismiss", max_attempts=1),
    )
    surface = FakeSurface(dialogs=("⚠ Transfer Confirmation",))

    with pytest.raises(UnrecoverableDialog):
        await recover(surface, rogue, capability, frame_path=["servicing-frame"], inputs=INPUTS)
    assert surface.dispatched == [], "nothing may be clicked on a dialog nobody recognised"


@pytest.mark.asyncio
async def test_an_unnamed_dialog_escalates_rather_than_proceeding(capability):
    """End to end: the catch-all rule fires and the run parks instead of acting on the screen."""
    parked: list[str] = []

    class Session:
        async def barrier(self, **_: Any) -> None:
            if parked:
                raise AssertionError("barrier should block, not spin")

        def assert_automation_owns(self) -> None:
            return None

        async def escalate(self, reason: str, **_: Any) -> str:
            parked.append(reason)
            return "int_test"

    surface = FakeSurface(dialogs=("⚠ Transfer Confirmation",))
    engine = build(capability, surface, session=Session(), bounds=Bounds(max_handoffs=0))
    result = await engine.run()

    assert result.status == "failure"
    assert result.code is FailureCode.MAX_HANDOFFS_EXCEEDED
    assert surface.dispatched == []


@pytest.mark.asyncio
async def test_an_overlay_that_never_clears_is_no_progress_not_an_unknown_dialog(capability):
    """It used to report `UNKNOWN_DIALOG` for an overlay — a reason naming a dialog that was never on
    screen. An exhausted `wait_and_retry` with no `else:` is a condition that never cleared."""
    surface = FakeSurface(overlays=("Searching...",))
    engine = build(capability, surface)
    result = await engine.run()

    assert result.status == "failure"
    assert result.code is FailureCode.NO_PROGRESS
    assert "overlay" in str(result.expected)


@pytest.mark.asyncio
async def test_an_escalation_parks_and_the_step_is_retried(capability):
    """An escalation is a pause, not an ending: the human fixes the screen and the *same step* runs again.

    Modelled here by a session whose `escalate` clears the dialog, as an operator would.
    """
    surface = FakeSurface(dialogs=("⚠ Transfer Confirmation",))
    handoffs: list[int] = []

    class Session:
        async def barrier(self, **_: Any) -> None:
            return None

        def assert_automation_owns(self) -> None:
            return None

        async def escalate(self, reason: str, **_: Any) -> str:
            handoffs.append(1)
            surface.dialogs = ()  # the human dealt with it
            return f"int_{len(handoffs)}"

    result = await build(capability, surface, session=Session()).run()

    assert handoffs == [1], "one handoff, then the run carried on"
    assert result.status == "success", getattr(result, "observed", result)


@pytest.mark.asyncio
async def test_parking_returns_the_sentinel_and_not_a_result(capability):
    """`PARKED` is deliberately not a `RunResult`: a parked run has not ended."""
    from src.domain.results import RunResult  # noqa: F401 - documents the contrast

    assert not hasattr(PARKED, "status")
