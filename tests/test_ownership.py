"""
Control transfer tests.

The property under test is not "the state machine transitions correctly" — it is
**two parties can never both act on one screen**. Most of these tests are therefore
about what is refused: stale versions, illegal moves, and actions attempted while
someone else holds control.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.sessions.manager import InterventionTimeout, RunEndedByHuman, SessionManager
from src.sessions.ownership import (
    ControlState,
    IllegalTransition,
    NotControlOwner,
    Owner,
    StaleControlVersion,
)

RUN_ID = "run_test_8"


# --------------------------------------------------------------------------- #
# the state machine
# --------------------------------------------------------------------------- #


def test_a_run_starts_in_automation_control():
    state = ControlState(run_id=RUN_ID)
    assert state.owner is Owner.AUTOMATION
    assert state.automation_may_act
    assert state.control_version == 0


def test_the_full_handoff_cycle():
    state = ControlState(run_id=RUN_ID)
    state = state.transition(Owner.HUMAN_PENDING, reason="UNKNOWN_DIALOG")
    state = state.transition(Owner.HUMAN, expected_version=1, operator="teller1")
    state = state.transition(Owner.AUTOMATION, expected_version=2)
    assert state.owner is Owner.AUTOMATION
    assert state.control_version == 3
    assert [e.to_owner for e in state.audit] == [
        Owner.HUMAN_PENDING,
        Owner.HUMAN,
        Owner.AUTOMATION,
    ]


def test_stale_version_is_refused_and_changes_nothing():
    """The core guarantee: a caller acting on an outdated view of the world loses,
    rather than silently overwriting a newer decision."""
    state = ControlState(run_id=RUN_ID).transition(Owner.HUMAN_PENDING, reason="x")
    with pytest.raises(StaleControlVersion) as exc:
        state.transition(Owner.HUMAN, expected_version=0, operator="latecomer")
    assert exc.value.expected == 0
    assert exc.value.actual == 1
    assert state.owner is Owner.HUMAN_PENDING  # untouched


def test_control_state_is_a_pure_value_not_a_lock():
    """Worth pinning so nobody later "fixes" this into a mutation.

    `transition()` on an immutable value is a pure function: two calls from the same
    starting state both succeed, because neither changed the other's input. That is
    correct, and it means the mutual-exclusion guarantee cannot live here — it lives
    in whatever holds the single current state. See the manager test below.
    """
    parked = ControlState(run_id=RUN_ID).transition(Owner.HUMAN_PENDING, reason="x")
    alice = parked.transition(Owner.HUMAN, expected_version=1, operator="alice")
    bob = parked.transition(Owner.HUMAN, expected_version=1, operator="bob")

    assert parked.owner is Owner.HUMAN_PENDING  # the original is untouched
    assert (alice.operator, bob.operator) == ("alice", "bob")


@pytest.mark.asyncio
async def test_only_one_of_two_operators_can_take_control(tmp_path):
    """Two people click Accept on the same intervention. Exactly one gets the screen.

    This is the guarantee that matters, and it is enforced by the manager, which owns
    the current state that both attempts compare against.
    """
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await manager.escalate("UNKNOWN_DIALOG")
    version = manager.state.control_version

    manager.accept(operator="alice", control_version=version)
    with pytest.raises(StaleControlVersion):
        manager.accept(operator="bob", control_version=version)

    assert manager.state.operator == "alice"
    assert manager.state.owner is Owner.HUMAN


def test_completed_runs_cannot_be_reopened():
    state = ControlState(run_id=RUN_ID).transition(Owner.COMPLETED)
    with pytest.raises(IllegalTransition):
        state.transition(Owner.AUTOMATION, expected_version=1)


def test_automation_cannot_grab_control_directly_from_pending():
    """HUMAN_PENDING -> AUTOMATION is legal (the human declined / timed out), but
    HUMAN_PENDING -> COMPLETED is not: nobody has looked at it yet."""
    state = ControlState(run_id=RUN_ID).transition(Owner.HUMAN_PENDING, reason="x")
    assert state.transition(Owner.AUTOMATION, expected_version=1).automation_may_act
    with pytest.raises(IllegalTransition):
        state.transition(Owner.COMPLETED, expected_version=1)


def test_assert_owner_raises_when_someone_else_holds_control():
    state = ControlState(run_id=RUN_ID).transition(Owner.HUMAN_PENDING, reason="x")
    with pytest.raises(NotControlOwner):
        state.assert_owner(Owner.AUTOMATION)


def test_every_transition_is_audited():
    state = ControlState(run_id=RUN_ID)
    state = state.transition(Owner.HUMAN_PENDING, reason="UNKNOWN_DIALOG")
    state = state.transition(Owner.HUMAN, expected_version=1, operator="teller1")
    entry = state.audit[-1]
    assert entry.from_owner is Owner.HUMAN_PENDING
    assert entry.to_owner is Owner.HUMAN
    assert (entry.from_version, entry.to_version) == (1, 2)
    assert entry.operator == "teller1"
    assert entry.at  # timestamped


def test_state_round_trips_through_json():
    """The file is the audit trail, so it has to survive serialization intact."""
    state = ControlState(run_id=RUN_ID).transition(Owner.HUMAN_PENDING, reason="x")
    state = state.transition(Owner.HUMAN, expected_version=1, operator="teller1")
    restored = ControlState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.owner is state.owner
    assert restored.control_version == state.control_version
    assert len(restored.audit) == len(state.audit)


# --------------------------------------------------------------------------- #
# the barrier
# --------------------------------------------------------------------------- #


class FakeAdapter:
    """Records whether the surface was paused, and counts actions that got through."""

    def __init__(self) -> None:
        self.paused = False
        self.calls: list[str] = []

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def act(self, what: str) -> None:
        self.calls.append(what)


@pytest.mark.asyncio
async def test_barrier_does_not_block_while_automation_holds_control():
    manager = SessionManager(RUN_ID)
    await asyncio.wait_for(manager.barrier(), timeout=1)


@pytest.mark.asyncio
async def test_escalation_stops_the_automation_immediately(tmp_path):
    adapter = FakeAdapter()
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=adapter)

    intervention_id = await manager.escalate("UNKNOWN_DIALOG", step_index=7)

    assert intervention_id.startswith("int_")
    assert adapter.paused, "the surface must be paused before the intervention is announced"
    assert manager.state.owner is Owner.HUMAN_PENDING
    with pytest.raises(NotControlOwner):
        manager.assert_automation_owns()


@pytest.mark.asyncio
async def test_a_parked_loop_never_reaches_the_adapter(tmp_path):
    """The whole point: while a human holds the screen, nothing the loop wants to do
    arrives at the surface."""
    adapter = FakeAdapter()
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=adapter)
    await manager.escalate("UNKNOWN_DIALOG")
    manager.accept(operator="teller1", control_version=1)

    async def loop_step() -> None:
        await manager.barrier()
        manager.assert_automation_owns()
        await adapter.act("click")

    task = asyncio.create_task(loop_step())
    await asyncio.sleep(0.2)
    assert adapter.calls == [], "an action reached the surface while a human held control"
    task.cancel()


@pytest.mark.asyncio
async def test_accept_alone_does_not_resume_the_loop(tmp_path):
    """Accepting and handing back are separate acts — the interval between them is
    the entire purpose."""
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await manager.escalate("UNKNOWN_DIALOG")
    manager.accept(operator="teller1", control_version=1)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(manager.barrier(), timeout=0.6)


@pytest.mark.asyncio
async def test_resume_releases_the_parked_loop(tmp_path):
    adapter = FakeAdapter()
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=adapter)
    await manager.escalate("UNKNOWN_DIALOG")
    manager.accept(operator="teller1", control_version=1)

    async def resume_shortly() -> None:
        await asyncio.sleep(0.1)
        await manager.resume(control_version=2)

    asyncio.create_task(resume_shortly())
    await asyncio.wait_for(manager.barrier(), timeout=2)
    assert manager.state.owner is Owner.AUTOMATION
    assert not adapter.paused


@pytest.mark.asyncio
async def test_parked_run_ends_on_budget_rather_than_hanging(tmp_path):
    """Waiting for a human is intentional; hanging forever is a bug."""
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await manager.escalate("UNKNOWN_DIALOG")
    with pytest.raises(InterventionTimeout):
        await manager.barrier(deadline=time.monotonic() + 0.3)


# --------------------------------------------------------------------------- #
# the cross-process handshake
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_intervention_file_is_written_for_the_other_process(tmp_path):
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await manager.escalate("UNKNOWN_DIALOG", step_index=7, context="modal on review")

    data = json.loads((tmp_path / "intervention.json").read_text())
    assert data["reason"] == "UNKNOWN_DIALOG"
    assert data["owner"] == "HUMAN_PENDING"
    assert data["step_index"] == 7
    assert "6080" in data["novnc_url"], "the takeover URL must be in the file"
    assert data["transitions"], "the audit trail starts at escalation"


@pytest.mark.asyncio
async def test_an_operator_in_another_process_can_take_and_return_control(tmp_path):
    """Simulates the two-terminal flow: the loop escalates, a separately-loaded
    manager (the CLI) accepts and resumes, and the loop sees it."""
    loop_side = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await loop_side.escalate("UNKNOWN_DIALOG")

    cli_side = SessionManager.load(RUN_ID, evidence_dir=tmp_path)
    assert cli_side.state.owner is Owner.HUMAN_PENDING
    cli_side.accept(operator="you", control_version=cli_side.state.control_version)
    await cli_side.resume(control_version=cli_side.state.control_version)

    # The parked loop picks the change up by polling the file.
    await asyncio.wait_for(loop_side.barrier(), timeout=3)
    assert loop_side.state.owner is Owner.AUTOMATION
    assert loop_side.state.control_version == 3


@pytest.mark.asyncio
async def test_operator_side_rejects_a_stale_version(tmp_path):
    loop_side = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await loop_side.escalate("UNKNOWN_DIALOG")
    cli_side = SessionManager.load(RUN_ID, evidence_dir=tmp_path)
    cli_side.accept(operator="you", control_version=1)

    with pytest.raises(StaleControlVersion):
        await cli_side.resume(control_version=1)  # it is 2 now


@pytest.mark.asyncio
async def test_cancel_releases_a_parked_loop(tmp_path):
    manager = SessionManager(RUN_ID, evidence_dir=tmp_path, adapter=FakeAdapter())
    await manager.escalate("UNKNOWN_DIALOG")
    manager.cancel(control_version=1, operator="you")
    await asyncio.wait_for(manager.barrier(), timeout=1)
    assert manager.state.owner is Owner.CANCELLED


def test_resume_note_tells_the_model_the_world_moved():
    """A system message, not a user turn: it carries operator authority. And it
    describes state rather than dictating a conclusion."""
    note = SessionManager(RUN_ID).resume_note()
    assert note["role"] == "system"
    assert "re-observe" in note["content"].lower()


# --------------------------------------------------------------------------- #
# the cross-process half of the handshake
# --------------------------------------------------------------------------- #


class _RecordingAdapter:
    def __init__(self) -> None:
        self.paused = False
        self.calls: list[str] = []

    async def pause(self) -> None:
        self.paused = True
        self.calls.append("pause")

    async def resume(self) -> None:
        self.paused = False
        self.calls.append("resume")


@pytest.mark.asyncio
async def test_the_barrier_unpauses_the_adapter_when_control_comes_back(tmp_path):
    """The operator's process has no adapter, so only the loop's can release it.

    Found by a live handoff: `resume` was typed in another terminal, the barrier opened,
    and then every single action came back "surface is paused; automation does not hold
    control" until the no-progress rule stopped the run. The guard was correct — nothing
    was clearing it.
    """
    adapter = _RecordingAdapter()
    loop_side = SessionManager("run_x", evidence_dir=tmp_path, adapter=adapter)
    await loop_side.escalate("UNKNOWN_DIALOG")
    assert adapter.paused is True

    # The operator, in a different process: state on disk, no surface in hand.
    operator_side = SessionManager.load("run_x", evidence_dir=tmp_path)
    assert operator_side.adapter is None
    operator_side.accept(operator="teller1")
    await operator_side.resume()

    await loop_side.barrier()

    assert adapter.paused is False, "the loop's adapter must be released, not just the gate"
    assert adapter.calls == ["pause", "resume"]


@pytest.mark.asyncio
async def test_a_human_ending_the_run_while_parked_does_not_hang(tmp_path):
    """`complete` is terminal, so waiting for a transition out of it waits forever."""
    manager = SessionManager("run_y", evidence_dir=tmp_path)
    await manager.escalate("UNKNOWN_DIALOG")

    operator = SessionManager.load("run_y", evidence_dir=tmp_path)
    operator.accept(operator="teller1")
    operator.complete(operator="teller1")

    with pytest.raises(RunEndedByHuman):
        await manager.barrier()
