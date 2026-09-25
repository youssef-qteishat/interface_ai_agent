"""
Controller tests — every stopping rule, offline.

The loop's contract is that a run always ends for a **named reason**. So these tests
are mostly a catalogue of endings: each fixture drives the loop into one termination
and asserts the specific code, because "it stopped" is not a useful thing to know.

The two that matter most:

  * a `goal_complete` on the wrong screen must FAIL, not succeed — otherwise the
    artifact a run produces replays garbage;
  * a refused irreversible click must never execute, whether the model tries once
    (told why, run continues) or twice (parked for a human).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.discovery.controller import Budget, Checkpoint, DiscoveryController
from src.discovery.model_provider import FakeProvider
from src.domain.trace import Display, Observation, ProviderInfo, RunTrace
from src.evidence.writer import EvidenceWriter
from src.policy.engine import PolicyEngine
from src.policy.redaction import Redactor
from src.sessions.manager import InterventionTimeout, RunEndedByHuman

DECLARED = {"member_id": "12345", "account_type": "savings", "opening_amount": "25.00"}
SEARCH_URL = "http://bank-sim:8001/servicing/members/search"
PNG = b"\x89PNG\r\n\x1a\nfake"

# Real text, captured from the running simulator. Note the case and formatting differ
# from the declared inputs ("savings" -> "Savings", "25.00" -> "$25.00"), which is why
# the checkpoint matches case-insensitively.
REVIEW_TEXT = (
    "Review New Account Member: 12345 Account Type: Savings "
    "Opening Amount: $25.00 Funding Source: ****-0042 Edit Open Account"
)
SEARCH_TEXT = "Credit Union Servicing Portal Member Search Member ID: Search"


def observation(text: str = SEARCH_TEXT, *, hash_: str = "sha256:aaa", **kw) -> Observation:
    return Observation.model_validate(
        {
            "main_frame_url": SEARCH_URL,
            "frames": [{"path": [], "url": SEARCH_URL}],
            "visible_text": text,
            "observation_hash": hash_,
            "dom_hash": f"dom:{hash_}",
            "headings": ["Member Search"],
            **kw,
        }
    )


class FakeSurface:
    """Canned screens. Returns observations from a list, repeating the last one."""

    def __init__(self, observations: list[Observation] | None = None, *, probe=None) -> None:
        self._observations = observations or [observation()]
        self._index = 0
        self.acted: list[str] = []
        self.last_screenshot_png = PNG
        self._probe = probe

    async def observe(self, **_: object) -> Observation:
        obs = self._observations[min(self._index, len(self._observations) - 1)]
        self._index += 1
        return obs

    async def act(self, action) -> dict:
        self.acted.append(action.kind)
        return {"ok": True}

    async def probe(self, x: int, y: int):
        if self._probe is None:
            from src.domain.trace import ProbeResult

            return ProbeResult.model_validate(
                {"coordinate": [x, y], "tag": "button", "role": "button",
                 "accessible_name": "Search"}
            )
        return self._probe

    async def capture_evidence(self) -> bytes:
        return PNG


class ProgressingSurface(FakeSurface):
    """A screen that changes every look.

    Needed by the limit tests: against a static screen the no-progress rule fires
    first, so a test for `MAX_STEPS_EXCEEDED` would silently be testing `NO_PROGRESS`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    async def observe(self, **_: object) -> Observation:
        self._n += 1
        return observation(hash_=f"sha256:{self._n:04d}")


def build(
    tmp_path: Path,
    script: list[dict],
    *,
    surface: FakeSurface | None = None,
    budget: Budget | None = None,
    session=None,
):
    writer = EvidenceWriter(root=tmp_path, redactor=Redactor(DECLARED))
    trace = RunTrace(
        run_id=writer.run_id,
        goal="Find member 12345 and prepare a savings sub-account; stop at review",
        target="http://bank-sim:8001/",
        display=Display(width=1280, height=800),
        provider=ProviderInfo(name="fake", model="none"),
    )
    return DiscoveryController(
        surface=surface or FakeSurface(),
        provider=FakeProvider(script),
        policy=PolicyEngine(run_id=writer.run_id, declared_inputs=DECLARED),
        writer=writer,
        trace=trace,
        session=session,
        budget=budget or Budget(max_steps=10, wall_clock_s=30, max_usd=5.0),
        checkpoint=Checkpoint.for_review(DECLARED),
    )


# --------------------------------------------------------------------------- #
# the normal path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_happy_path_succeeds_when_the_screen_agrees(tmp_path):
    surface = FakeSurface([observation(), observation(REVIEW_TEXT, hash_="sha256:bbb")])
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "left_click", "coordinate": [550, 96]}]},
            {"actions": [{"kind": "goal_complete", "summary": "reached review"}]},
        ],
        surface=surface,
    )
    result = await controller.run()

    assert result.status == "success"
    assert result.checkpoint_verified is True
    assert result.stop_reason == "CHECKPOINT_VERIFIED"


@pytest.mark.asyncio
async def test_goal_complete_on_the_wrong_screen_is_a_failure(tmp_path):
    """The heart of it: the model's claim loses to the screen.

    A run that reported success here would emit an artifact that replays garbage.
    """
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "I think I am done"}]}],
        surface=FakeSurface([observation(SEARCH_TEXT)]),  # still on search
    )
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "CHECKPOINT_FAILED"
    assert "Review New Account" in result.observed["missing"]
    assert result.expected["required_text"]


@pytest.mark.asyncio
async def test_checkpoint_matches_despite_case_and_formatting(tmp_path):
    """Declared `savings` renders as `Savings`; `25.00` renders as `$25.00`. A
    case-sensitive checkpoint would reject a screen that is actually correct."""
    checkpoint = Checkpoint.for_review(DECLARED)
    passed, missing = checkpoint.evaluate(observation(REVIEW_TEXT))
    assert passed, missing


@pytest.mark.asyncio
async def test_business_outcome_is_not_a_failure(tmp_path):
    """An unknown member is a legitimate answer from the application."""
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "business_outcome", "code": "MEMBER_NOT_FOUND",
                       "detail": "no such member"}]}],
    )
    result = await controller.run()

    assert result.status == "business_outcome"
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.stop_reason == "TERMINAL_DECLARATION"


# --------------------------------------------------------------------------- #
# the irreversible control
# --------------------------------------------------------------------------- #


def open_account_surface() -> FakeSurface:
    from src.domain.trace import ProbeResult

    probe = ProbeResult.model_validate(
        {
            "coordinate": [246, 447],
            "tag": "button",
            "role": "button",
            "accessible_name": "Open Account",
            "classes": ["danger-button"],
        }
    )
    return FakeSurface([observation(REVIEW_TEXT)], probe=probe)


@pytest.mark.asyncio
async def test_first_reach_for_the_commit_button_is_refused_not_executed(tmp_path):
    """Told why, the run continues — and the click never reached the surface."""
    surface = open_account_surface()
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
            {"actions": [{"kind": "goal_complete", "summary": "already at review"}]},
        ],
        surface=surface,
    )
    result = await controller.run()

    assert surface.acted == [], "the irreversible click must never execute"
    assert result.status == "success", "one refusal should not derail the run"

    step = controller.trace.steps[0]
    assert step.policy.decision == "escalate"
    assert step.policy.code == "IRREVERSIBLE_REQUIRES_APPROVAL"


@pytest.mark.asyncio
async def test_a_second_attempt_parks_for_a_human(tmp_path):
    """Twice is a loop, not a slip."""
    surface = open_account_surface()
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
            {"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
        ],
        surface=surface,
    )
    result = await controller.run()

    assert surface.acted == []
    assert result.status == "escalated"
    assert result.reason == "IRREVERSIBLE_REQUIRES_APPROVAL"


@pytest.mark.asyncio
async def test_the_model_is_told_why_it_was_refused(tmp_path):
    """A bare 'failed' invites a retry; the reason produces a better next move."""
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
         {"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=open_account_surface(),
    )
    await controller.run()

    told = controller.provider.recorded[0][0].detail
    assert "irreversible" in told.lower()
    assert "do not retry" in told.lower()


# --------------------------------------------------------------------------- #
# bounded execution — one code per limit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_max_steps_stops_the_run(tmp_path):
    script = [{"actions": [{"kind": "left_click", "coordinate": [10, 10]}]} for _ in range(20)]
    controller = build(
        tmp_path, script, surface=ProgressingSurface(), budget=Budget(max_steps=3, wall_clock_s=30)
    )
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "MAX_STEPS_EXCEEDED"
    assert result.stop_reason == "MAX_STEPS"


@pytest.mark.asyncio
async def test_no_progress_stops_a_loop(tmp_path):
    """The same screen, unchanged, three times over."""
    script = [{"actions": [{"kind": "left_click", "coordinate": [10, 10]}]} for _ in range(10)]
    controller = build(
        tmp_path,
        script,
        surface=FakeSurface([observation(hash_="sha256:same")]),
        budget=Budget(max_steps=20, wall_clock_s=30),
    )
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "NO_PROGRESS"


@pytest.mark.asyncio
async def test_repeated_invalid_actions_stop_the_run(tmp_path):
    bad = {"kind": "left_click_drag", "start_coordinate": [1, 1], "coordinate": [2, 2]}
    controller = build(tmp_path, [{"invalid": [bad, bad, bad]}], surface=ProgressingSurface())
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "INVALID_ACTIONS_EXCEEDED"


@pytest.mark.asyncio
async def test_budget_stops_the_run(tmp_path):
    script = [{"actions": [{"kind": "screenshot"}]} for _ in range(20)]
    controller = build(
        tmp_path,
        script,
        surface=ProgressingSurface(),
        budget=Budget(max_steps=50, wall_clock_s=60, max_usd=0.001),
    )
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "BUDGET_EXCEEDED"
    assert "over" in result.observed["detail"]


@pytest.mark.asyncio
async def test_wall_clock_stops_the_run(tmp_path):
    script = [{"actions": [{"kind": "screenshot"}]} for _ in range(20)]
    controller = build(
        tmp_path, script, surface=ProgressingSurface(), budget=Budget(max_steps=50, wall_clock_s=-1)
    )
    result = await controller.run()

    assert result.code == "WALL_CLOCK_EXCEEDED"


@pytest.mark.asyncio
async def test_cancellation_still_writes_evidence(tmp_path):
    """The evidence is most valuable exactly when the run was interrupted."""

    class SlowSurface(ProgressingSurface):
        async def observe(self, **kw):
            await asyncio.sleep(0.05)
            return await super().observe(**kw)

    controller = build(
        tmp_path,
        [{"actions": [{"kind": "left_click", "coordinate": [1, 1]}]} for _ in range(50)],
        surface=SlowSurface(),
        budget=Budget(max_steps=50, wall_clock_s=30),
    )

    task = asyncio.create_task(controller.run())
    await asyncio.sleep(0.3)
    task.cancel()
    result = await task

    assert result.status == "failure"
    assert result.code == "CANCELLED"
    assert (controller.writer.dir / "trace.yaml").exists()
    assert (controller.writer.dir / "run-summary.json").exists()


# --------------------------------------------------------------------------- #
# escalation from the screen
# --------------------------------------------------------------------------- #


class FakeSession:
    """A human on the other end of the barrier.

    `on_barrier` decides what they do: hand the screen back, end the run, or never
    arrive. All three are real outcomes of a park and each must end the run differently.

    Ownership is tracked the way `ControlState` tracks it, rather than stubbed, because
    the controller reads it to decide whether a run ended with someone still holding the
    screen.
    """

    def __init__(self, on_barrier: str = "hand_back") -> None:
        self.escalated: list[str] = []
        self.intervention_id = "int_fake"
        self.on_barrier = on_barrier
        self.barrier_calls = 0
        self.notes = 0
        self.cancelled = False
        self.state = SimpleNamespace(
            operator="tester",
            control_version=0,
            automation_may_act=True,
            is_terminal=False,
        )

    async def barrier(self, *, deadline=None) -> None:
        self.barrier_calls += 1
        if self.state.automation_may_act:
            return
        if self.on_barrier == "ends_run":
            raise RunEndedByHuman("COMPLETED")
        if self.on_barrier == "never_arrives":
            raise InterventionTimeout("nobody came")
        self.state.automation_may_act = True  # handed back
        self.state.control_version += 1

    def assert_automation_owns(self) -> None:
        return None

    async def escalate(self, reason, **kw) -> str:
        self.escalated.append(reason)
        self.state.automation_may_act = False
        self.state.control_version += 1
        return self.intervention_id

    def cancel(self, *, control_version=None, operator=None):
        self.cancelled = True
        self.state.is_terminal = True
        self.state.control_version += 1

    def resume_note(self) -> dict[str, str]:
        self.notes += 1
        return {"role": "system", "content": "a human took control"}


@pytest.mark.asyncio
async def test_an_unknown_dialog_parks_before_the_model_is_even_asked(tmp_path):
    """A modal with no rule is not something to reason about.

    The model is not consulted at all: the park happens on the observation, before the
    turn that would have asked it what to do about a dialog it has no rule for.
    """
    session = FakeSession("never_arrives")
    surface = FakeSurface(
        [observation(REVIEW_TEXT, dialogs=[{"selector": "modal-overlay",
                                            "text": "System Notice"}])]
    )
    controller = build(tmp_path, [], surface=surface, session=session)
    result = await controller.run()

    assert session.escalated == ["UNKNOWN_DIALOG"]
    assert controller.provider.turn == 0, "the model should not have been consulted"
    assert result.status == "escalated"
    assert result.reason == "UNKNOWN_DIALOG"


@pytest.mark.asyncio
async def test_an_unattended_park_reports_what_it_was_waiting_for(tmp_path):
    """Nobody came. The reason must be the one that parked the run — an earlier version
    hardcoded MODEL_REQUESTED and mislabelled every other cause."""
    session = FakeSession("never_arrives")
    surface = FakeSurface(
        [observation(dialogs=[{"selector": "m", "text": "System Notice"}])]
    )
    controller = build(tmp_path, [], surface=surface, session=session)
    result = await controller.run()

    assert result.reason == "UNKNOWN_DIALOG"
    assert "nobody came" in (result.context or "")


@pytest.mark.asyncio
async def test_a_human_who_ends_the_run_is_not_reported_as_a_timeout(tmp_path):
    """`session complete` is a decision, not an absence. Waiting for a transition out of
    a terminal owner would wait forever and then blame the human for not arriving."""
    session = FakeSession("ends_run")
    surface = FakeSurface(
        [observation(dialogs=[{"selector": "m", "text": "System Notice"}])]
    )
    controller = build(tmp_path, [], surface=surface, session=session)
    result = await controller.run()

    assert result.status == "failure"
    assert result.code == "HUMAN_ENDED_RUN"
    assert result.stop_reason == "HUMAN_ENDED_RUN"
    assert (controller.writer.dir / "trace.yaml").exists()


@pytest.mark.asyncio
async def test_request_human_parks_and_the_run_continues(tmp_path):
    """The model asks for help, gets it, and finishes the job."""
    session = FakeSession()
    surface = FakeSurface([observation(), observation(REVIEW_TEXT, hash_="sha256:bbb")])
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "request_human", "reason": "needs a decision"}]},
            {"actions": [{"kind": "goal_complete", "summary": "done after the handoff"}]},
        ],
        surface=surface,
        session=session,
    )
    result = await controller.run()

    assert session.escalated == ["MODEL_REQUESTED"]
    assert result.status == "success", "a handoff should not end the run"
    assert result.stop_reason == "CHECKPOINT_VERIFIED"
    assert controller.handoffs == 1


@pytest.mark.asyncio
async def test_an_overlay_gets_one_bounded_wait(tmp_path):
    """A loading overlay is a timing artifact, not a decision — and never an
    unbounded retry loop."""
    surface = FakeSurface(
        [observation(overlays=["Processing..."]), observation(REVIEW_TEXT)]
    )
    controller = build(
        tmp_path, [{"actions": [{"kind": "goal_complete", "summary": "done"}]}], surface=surface
    )
    await controller.run()
    assert "wait" in surface.acted


# --------------------------------------------------------------------------- #
# what every run must leave behind
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_step_carries_a_policy_decision_and_an_explanation(tmp_path):
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "left_click", "coordinate": [550, 96]},
                         {"kind": "type", "text": "12345"}]},
            {"actions": [{"kind": "goal_complete", "summary": "done"}]},
        ],
        surface=FakeSurface([observation(), observation(REVIEW_TEXT)]),
    )
    await controller.run()

    for step in controller.trace.steps:
        assert step.policy.rule, "a step with no policy decision is unauditable"
    assert controller.trace.steps_missing_probe() == []


@pytest.mark.asyncio
async def test_the_run_writes_a_complete_evidence_folder(tmp_path):
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "left_click", "coordinate": [550, 96]}]},
         {"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=FakeSurface([observation(), observation(REVIEW_TEXT)]),
    )
    result = await controller.run()

    run_dir = controller.writer.dir
    assert (run_dir / "trace.yaml").exists()
    assert (run_dir / "events.redacted.jsonl").exists()
    assert (run_dir / "run-summary.json").exists()
    # And nothing sensitive reached any of them.
    blob = (run_dir / "trace.yaml").read_text() + (run_dir / "events.redacted.jsonl").read_text()
    assert "12345" not in blob
    assert result.status == "success"


@pytest.mark.asyncio
async def test_a_failed_action_skips_the_rest_of_its_batch(tmp_path):
    """The model must not be told that later actions ran when they did not."""
    surface = open_account_surface()
    controller = build(
        tmp_path,
        [
            {"actions": [
                {"kind": "left_click", "coordinate": [246, 447]},   # refused
                {"kind": "type", "text": "12345"},                  # must be skipped
            ]},
            {"actions": [{"kind": "goal_complete", "summary": "done"}]},
        ],
        surface=surface,
    )
    await controller.run()

    outcomes = controller.provider.recorded[0]
    assert outcomes[0].ok is False
    assert outcomes[1].skipped is True
    assert "type" not in surface.acted


@pytest.mark.asyncio
async def test_each_step_records_what_appeared_on_screen(tmp_path):
    """`dom_changed` says something happened; `new_text` says what.

    Diffed by word, not by line: the agent flattens each frame to a single line, so a
    line diff would only ever report "the frame changed".
    """
    after_text = SEARCH_TEXT + " Member Name Status 12345 Martinez, J. Active"
    surface = FakeSurface(
        [observation(SEARCH_TEXT), observation(after_text, hash_="sha256:bbb")]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "key", "text": "Return"}]},
         {"actions": [{"kind": "cannot_proceed", "reason": "done looking"}]}],
        surface=surface,
    )
    await controller.run()

    step = controller.trace.steps[0]
    assert step.observation_after.dom_changed is True
    # In memory the trace holds the real value; redaction happens at the writer.
    assert step.observation_after.new_text == ["Member Name Status 12345 Martinez, J. Active"]
    assert step.observation_after.removed_text == []
    on_disk = (controller.writer.dir / "trace.yaml").read_text()
    assert "Member Name Status ${inputs.member_id} Martinez, J. Active" in on_disk


@pytest.mark.asyncio
async def test_each_action_in_a_batch_records_its_own_precondition(tmp_path):
    """A step's `observation_before` is its precondition.

    When the model batches five actions, giving them all the turn's opening screen
    would claim the click on Continue happened on a form with the amount still empty —
    a precondition a replay would assert, and fail on.
    """
    screens = [observation(f"screen {i}", hash_=f"sha256:{i:04d}") for i in range(12)]
    surface = FakeSurface(screens)
    controller = build(
        tmp_path,
        [
            {"actions": [
                {"kind": "left_click", "coordinate": [738, 124]},
                {"kind": "type", "text": "25.00"},
                {"kind": "left_click", "coordinate": [694, 212]},
            ]},
            {"actions": [{"kind": "cannot_proceed", "reason": "enough"}]},
        ],
        surface=surface,
    )
    await controller.run()

    befores = [s.observation_before.visible_text for s in controller.trace.steps]
    assert len(set(befores)) == len(befores), f"steps share a precondition: {befores}"
    # And each step's precondition is the screen its predecessor left behind.
    for earlier, later in zip(controller.trace.steps, controller.trace.steps[1:]):
        assert later.observation_before.visible_text != earlier.observation_before.visible_text


# --------------------------------------------------------------------------- #
# the human as a recorded actor
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_handoff_is_recorded_as_a_human_step(tmp_path):
    """One step, attributed to the person, saying what changed while they held it.

    Not a reconstruction of their clicks: we did not watch them work, and a coordinate
    invented for a step nobody observed would put a fiction into the artifact.
    """
    session = FakeSession()
    surface = FakeSurface(
        [
            observation(dialogs=[{"selector": "m", "text": "System Notice"}]),
            # What the human left behind: the dialog gone, the review panel showing.
            observation(REVIEW_TEXT, hash_="sha256:bbb"),
        ]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "the dialog is gone"}]}],
        surface=surface,
        session=session,
    )
    result = await controller.run()

    human = [s for s in controller.trace.steps if s.actor == "human"]
    assert len(human) == 1, "one step per handoff, not one per observation"
    step = human[0]
    assert step.policy is None, "nothing evaluated what the human did"
    assert step.action["kind"] == "human_intervention"
    assert step.action["reason"] == "UNKNOWN_DIALOG"
    assert step.action["operator"] == "tester"
    assert step.probe_unavailable, "a step with no probe must say why"
    assert step.observation_after.dom_changed is True
    assert any("Review New Account" in phrase for phrase in step.observation_after.new_text)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_the_model_is_told_the_screen_moved_under_it(tmp_path):
    """Once per handoff. The model's expectations are stale and acting on them is the
    bug the whole mechanism exists to prevent."""
    session = FakeSession()
    surface = FakeSurface(
        [observation(dialogs=[{"selector": "m", "text": "Notice"}]), observation(REVIEW_TEXT)]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "clear"}]}],
        surface=surface,
        session=session,
    )
    await controller.run()

    assert session.notes == 1
    assert controller.provider.notes[0]["role"] == "system"


@pytest.mark.asyncio
async def test_the_loop_re_observes_rather_than_acting_on_the_pre_handoff_screen(tmp_path):
    """The model must be shown the post-handoff screen, not the one that parked us."""
    session = FakeSession()
    surface = FakeSurface(
        [
            observation("before the human", dialogs=[{"selector": "m", "text": "Notice"}]),
            observation(REVIEW_TEXT, hash_="sha256:bbb"),
        ]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=surface,
        session=session,
    )
    await controller.run()

    assert controller.provider.turn == 1, "the model was asked once, after the handoff"
    assert "before the human" not in (controller._last_observation.visible_text or "")


@pytest.mark.asyncio
async def test_repeated_handoffs_are_capped(tmp_path):
    """A run that keeps needing a human is not making progress either."""
    session = FakeSession()
    # The dialog never goes away, so every iteration parks again.
    surface = FakeSurface([observation(dialogs=[{"selector": "m", "text": "Notice"}])])
    controller = build(
        tmp_path,
        [],
        surface=surface,
        session=session,
        budget=Budget(max_steps=20, wall_clock_s=30, max_handoffs=2),
    )
    result = await controller.run()

    assert controller.handoffs == 2
    assert result.status == "failure"
    assert result.code == "MAX_HANDOFFS_EXCEEDED"
    assert result.stop_reason == "MAX_HANDOFFS"


@pytest.mark.asyncio
async def test_the_irreversible_gate_is_not_recoverable_by_resuming(tmp_path):
    """The one escalation a handoff cannot clear.

    Typing `session resume` means "I have finished looking at the screen". It does not
    mean "I authorize this commit" — that needs an ApprovalToken bound to the digest of
    the exact action. Letting a resume stand in for approval would make the narrower
    mechanism pointless.
    """
    session = FakeSession()
    surface = open_account_surface()
    controller = build(
        tmp_path,
        [
            {"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
            {"actions": [{"kind": "left_click", "coordinate": [246, 447]}]},
        ],
        surface=surface,
        session=session,
    )
    result = await controller.run()

    assert result.status == "escalated"
    assert result.reason == "IRREVERSIBLE_REQUIRES_APPROVAL"
    assert controller.handoffs == 0, "the loop must not park and carry on here"
    assert surface.acted == [], "and the click still never executes"


@pytest.mark.asyncio
async def test_a_handoff_is_counted_in_the_summary(tmp_path):
    """'Was a person involved?' should not require reading the whole trace."""
    import json

    session = FakeSession()
    surface = FakeSurface(
        [observation(dialogs=[{"selector": "m", "text": "Notice"}]), observation(REVIEW_TEXT)]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=surface,
        session=session,
    )
    await controller.run()

    summary = json.loads((controller.writer.dir / "run-summary.json").read_text())
    assert summary["human_steps"] == 1
    assert summary["steps_recorded"] == 1


@pytest.mark.asyncio
async def test_an_unexpected_crash_still_leaves_a_readable_folder(tmp_path):
    """The cases nobody predicted are the ones worth reading about.

    Found the hard way: a live handoff died on an API 400 and left a trace.yaml but no
    run-summary.json, so the one file naming the outcome was missing from exactly the run
    that needed explaining.
    """
    import json

    class ExplodingSurface(FakeSurface):
        async def observe(self, **kw):
            raise RuntimeError("something nobody predicted")

    controller = build(tmp_path, [], surface=ExplodingSurface())

    with pytest.raises(RuntimeError, match="nobody predicted"):
        await controller.run()

    summary = json.loads((controller.writer.dir / "run-summary.json").read_text())
    assert summary["outcome"]["code"] == "PROVIDER_ERROR"
    assert "RuntimeError" in summary["outcome"]["observed"]["detail"]
    assert (controller.writer.dir / "trace.yaml").exists()


@pytest.mark.asyncio
async def test_a_dismissed_dialog_is_recorded_as_text_that_left_the_screen(tmp_path):
    """The diff runs both ways.

    Found on a live handoff: the operator dismissed a modal, the step recorded
    `dom_changed: true` with an empty `new_text`, and the artifact described the one
    thing that happened as nothing at all. A dismissal is entirely a disappearance.
    """
    session = FakeSession()
    surface = FakeSurface(
        [
            observation(
                "System Notice An unexpected condition was detected " + REVIEW_TEXT,
                dialogs=[{"selector": "m", "text": "System Notice"}],
            ),
            observation(REVIEW_TEXT, hash_="sha256:bbb"),
        ]
    )
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "the dialog is gone"}]}],
        surface=surface,
        session=session,
    )
    await controller.run()

    step = [s for s in controller.trace.steps if s.actor == "human"][0]
    assert step.observation_after.dom_changed is True
    assert step.observation_after.new_text == []
    assert any("System Notice" in phrase for phrase in step.observation_after.removed_text)


@pytest.mark.asyncio
async def test_the_trace_records_what_the_run_was_allowed_and_what_it_spent(tmp_path):
    """Both halves, and both must survive to disk.

    The limits are written up front, because a run that dies should still say what it was
    allowed to do — that is most of the question when reading one that stopped early. The
    spend is written just before the folder closes: an earlier version assigned it after
    `finish()`, which had already serialized the trace, so `budget` reached disk empty
    every time.
    """
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "left_click", "coordinate": [550, 96]}]},
         {"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=FakeSurface([observation(), observation(REVIEW_TEXT)]),
        budget=Budget(max_steps=10, wall_clock_s=30, max_usd=5.0),
    )
    await controller.run()

    on_disk = RunTrace.from_yaml((controller.writer.dir / "trace.yaml").read_text()).budget
    assert on_disk.max_steps == 10
    assert on_disk.wall_clock_s == 30
    assert on_disk.input_tokens > 0
    assert on_disk.usd_estimate is not None and on_disk.usd_estimate > 0


@pytest.mark.asyncio
async def test_a_run_that_dies_still_says_what_it_was_allowed(tmp_path):
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "left_click", "coordinate": [1, 1]}]} for _ in range(20)],
        surface=ProgressingSurface(),
        budget=Budget(max_steps=2, wall_clock_s=30),
    )
    await controller.run()

    on_disk = RunTrace.from_yaml((controller.writer.dir / "trace.yaml").read_text())
    assert on_disk.outcome.code == "MAX_STEPS_EXCEEDED"
    assert on_disk.budget.max_steps == 2


@pytest.mark.asyncio
async def test_a_run_that_ends_while_parked_stops_claiming_a_human_is_expected(tmp_path):
    """Ctrl-C mid-handoff left `intervention.json` reading HUMAN_PENDING for a run that
    was over, so `session status` reported a parked run nothing was waiting on."""
    session = FakeSession("never_arrives")
    surface = FakeSurface([observation(dialogs=[{"selector": "m", "text": "Notice"}])])
    controller = build(tmp_path, [], surface=surface, session=session)

    await controller.run()

    assert session.escalated == ["UNKNOWN_DIALOG"], "it really did park"
    assert session.cancelled is True, "and the park was closed out when the run ended"


@pytest.mark.asyncio
async def test_a_run_that_ends_in_control_leaves_ownership_alone(tmp_path):
    """Nothing about a normal ending is misleading, so nothing needs correcting."""
    session = FakeSession()
    controller = build(
        tmp_path,
        [{"actions": [{"kind": "goal_complete", "summary": "done"}]}],
        surface=FakeSurface([observation(REVIEW_TEXT)]),
        session=session,
    )
    result = await controller.run()

    assert result.status == "success"
    assert session.cancelled is False
