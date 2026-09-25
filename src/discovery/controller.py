"""
The discovery loop.

Everything else in this layer exists so that this file can be short about safety and
long about bookkeeping. The model proposes; the policy engine decides; the surface
executes; the writer records. The controller's own job is narrower than it looks:

  * run the steps in an order that cannot be rearranged without breaking a guarantee,
  * make every ending have a NAME, and
  * never trust the model's word about whether it succeeded.

That last one is the point of the checkpoint. `goal_complete` is a proposal, not a
result. A run that reports success on the wrong screen produces an artifact that
replays garbage, and the only defence is to look at the screen ourselves.

The stopping rules exist for the opposite failure: a run that never ends. Each one has
its own code, so "it stopped" is never the whole story — a reader can always tell
whether it ran out of steps, stopped making progress, or hit the wallet.
"""

from __future__ import annotations

import asyncio
import difflib
import time
from dataclasses import dataclass, field
from typing import Any

from src.domain.actions import COORDINATE_KINDS, TERMINAL_KINDS, HumanIntervention, Wait
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
from src.domain.trace import Observation, RecordedStep, RunTrace, Timing
from src.discovery.model_provider import ActionOutcome
from src.sessions.manager import InterventionTimeout, RunEndedByHuman
from src.sessions.ownership import NotControlOwner
from src.surfaces.base import ProbeFailed, SurfaceError, SurfaceUnavailable

# Defaults. All overridable per run; these are the numbers a 20-step workflow wants.
DEFAULT_MAX_STEPS = 40
DEFAULT_WALL_CLOCK_S = 300.0
DEFAULT_MAX_USD = 5.0
MAX_INVALID_ACTIONS = 3
MAX_UNCHANGED_OBSERVATIONS = 3
MAX_SAME_TARGET_FAILURES = 2
# A run that keeps asking for a human is not making progress. Without a cap, an
# escalate/resume cycle is bounded only by the wall clock.
DEFAULT_MAX_HANDOFFS = 3

# Which escalations a human can hand back from.
#
# `IRREVERSIBLE_REQUIRES_APPROVAL` is deliberately absent. Typing `session resume` means
# "I have finished looking at the screen"; it does not mean "I authorize this commit".
# Treating the two as the same thing would let a human approve an irreversible action by
# accident, while the mechanism that exists for approving one — an `ApprovalToken` bound
# to the digest of that exact action — went unused.
RECOVERABLE_ESCALATIONS: frozenset[str] = frozenset(
    {
        EscalationReason.UNKNOWN_DIALOG,
        EscalationReason.MODEL_REQUESTED,
        EscalationReason.SESSION_EXPIRED,
        EscalationReason.POLICY_ESCALATION,
    }
)
# One bounded wait for a loading overlay before treating the screen as settled.
OVERLAY_WAIT_S = 1.5
# Caps on the per-step text diff. A whole-page navigation would otherwise copy the
# entire new page into the trace, once per step.
MAX_NEW_TEXT_PHRASES = 12
MAX_NEW_TEXT_CHARS = 300


@dataclass
class Budget:
    max_steps: int = DEFAULT_MAX_STEPS
    wall_clock_s: float = DEFAULT_WALL_CLOCK_S
    max_usd: float = DEFAULT_MAX_USD
    max_handoffs: int = DEFAULT_MAX_HANDOFFS


class _Resume:
    """Returned by `_escalate` when a human handed control back.

    A distinct sentinel rather than `None`, because `None` already means "nothing
    happened, carry on" at these call sites and this means something much stronger: the
    screen may have changed under us, so the iteration must start over from a fresh
    observation.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<resume>"


RESUME = _Resume()


@dataclass
class Checkpoint:
    """What must be on screen for `goal_complete` to be believed.

    Case-insensitive, and that is not laziness: the run declares
    `account_type: "savings"` while the page renders `Savings`, and `25.00` renders as
    `$25.00`. Matching case-sensitively would reject a screen that is in fact correct —
    the worst kind of checkpoint, since it turns a good run into a reported failure.
    """

    required_text: list[str] = field(default_factory=list)

    @classmethod
    def for_review(cls, declared_inputs: dict[str, Any]) -> Checkpoint:
        return cls(required_text=["Review New Account", *(str(v) for v in declared_inputs.values())])

    def evaluate(self, observation: Observation | None) -> tuple[bool, list[str]]:
        """Returns (passed, missing)."""
        haystack = (observation.visible_text or "").lower() if observation else ""
        missing = [needle for needle in self.required_text if needle.lower() not in haystack]
        return (not missing and bool(haystack)), missing


class DiscoveryController:
    """One discovery run."""

    def __init__(
        self,
        *,
        surface: Any,
        provider: Any,
        policy: Any,
        writer: Any,
        trace: RunTrace,
        session: Any | None = None,
        budget: Budget | None = None,
        checkpoint: Checkpoint | None = None,
        on_event: Any | None = None,
    ) -> None:
        self.surface = surface
        self.provider = provider
        self.policy = policy
        self.writer = writer
        self.trace = trace
        self.session = session
        self.budget = budget or Budget()
        self.checkpoint = checkpoint or Checkpoint()
        self.on_event = on_event or (lambda *a, **k: None)

        # The limits go onto the trace immediately, not at the end: a run that dies
        # should still say what it was allowed to do, and that is most of the question
        # when reading a run that stopped early.
        self.trace.budget.max_steps = self.budget.max_steps
        self.trace.budget.wall_clock_s = self.budget.wall_clock_s

        self.started_at = time.monotonic()
        self.invalid_actions = 0
        self.irreversible_attempts = 0
        self.handoffs = 0
        self._recent_hashes: list[str] = []
        self._target_failures: dict[str, int] = {}
        self._last_observation: Observation | None = None
        self._last_png: bytes | None = None
        # Remembered so a park that times out reports what the run actually escalated
        # for, rather than a hardcoded guess.
        self._last_escalation: EscalationReason | None = None

    # ---- the loop ----

    async def run(self) -> Any:
        """Drive until something ends the run, then return a tagged result.

        Never raises for an expected condition: a surface that died, a budget that ran
        out and a model that gave up are all *results*, because the caller needs to
        tell them apart without parsing an exception.
        """
        result: Any = None
        try:
            result = await self._loop()
        except (asyncio.CancelledError, KeyboardInterrupt):
            # The evidence is most valuable precisely when the run was interrupted.
            result = self._fail(
                FailureCode.CANCELLED, StopReason.CANCELLED, detail="interrupted"
            )
        except InterventionTimeout as exc:
            # Nobody came. Report what it was actually waiting for — an earlier version
            # hardcoded MODEL_REQUESTED here, which mislabelled every other reason.
            result = Escalated(
                run_id=self.trace.run_id,
                intervention_id=getattr(self.session, "intervention_id", None) or "unknown",
                reason=self._last_escalation or EscalationReason.MODEL_REQUESTED,
                stop_reason=StopReason.ESCALATION,
                steps_used=len(self.trace.steps),
                context=str(exc),
            )
        except RunEndedByHuman as exc:
            result = self._fail(
                FailureCode.HUMAN_ENDED_RUN, StopReason.HUMAN_ENDED_RUN, detail=str(exc)
            )
        except NotControlOwner as exc:
            result = self._fail(
                FailureCode.NOT_CONTROL_OWNER, StopReason.ESCALATION, detail=str(exc)
            )
        except SurfaceUnavailable as exc:
            result = self._fail(
                FailureCode.SURFACE_UNAVAILABLE, StopReason.PROVIDER_ERROR, detail=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised below
            # An unexpected failure is still a run that happened, and "a run that dies
            # leaves a folder someone can read" has to hold for the cases nobody
            # predicted — those are the ones worth reading. Found by a live handoff that
            # died on an API 400 and left no run-summary.json at all.
            #
            # Recorded and re-raised, not swallowed: the caller still needs the
            # traceback, and turning an unknown bug into a tidy `Failure` would hide it.
            result = self._fail(
                FailureCode.PROVIDER_ERROR,
                StopReason.PROVIDER_ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if result is not None:
                self._record_spend()
                self._close_out_session()
                try:
                    self.writer.finish(
                        self.trace,
                        result,
                        final_png=self._last_png,
                        stop_reason=result.stop_reason,
                    )
                except Exception:  # noqa: BLE001
                    # A failure while closing the folder must not replace the failure
                    # that caused it — that swap is how the real cause gets lost.
                    pass
        return result

    async def _loop(self) -> Any:
        while True:
            # 1. Control. The barrier parks the loop while a human holds the screen;
            # the assertion catches a call that somehow got past it.
            if self.session is not None:
                await self.session.barrier(deadline=self._deadline())
                self.session.assert_automation_owns()

            # 2. Look.
            observation = await self._observe()

            # 3. Global exception states, before asking the model anything: a modal it
            # has no rule for is not something to reason about, it is something to
            # hand to a human.
            interrupted = await self._handle_exception_states(observation)
            if interrupted is RESUME:
                # A human held the screen and gave it back. Everything observed above is
                # stale by definition, so start the iteration again rather than acting
                # on a pre-handoff picture — the bug the handoff mechanism exists to
                # prevent.
                continue
            if interrupted is not None:
                return interrupted

            # Stop rules that can fire before spending another turn. This is also the
            # one place progress is sampled: once per iteration, at the top. Sampling
            # again at the bottom would count the same screen twice and trip the
            # no-progress rule after one and a half steps instead of three.
            early = self._check_limits(observation, record_progress=True)
            if early is not None:
                return early

            # 4. Ask.
            batch = await self.provider.propose(observation, self._last_png)
            self.invalid_actions += len(batch.invalid)
            self.on_event("proposed", actions=[a.kind for a in batch.actions], reason=batch.reason)

            if batch.invalid and self.invalid_actions >= MAX_INVALID_ACTIONS:
                self.provider.record_results([])
                return self._fail(
                    FailureCode.INVALID_ACTIONS_EXCEEDED,
                    StopReason.INVALID_ACTIONS,
                    detail=f"{self.invalid_actions} invalid actions",
                )

            # 5. Execute, in order, stopping the batch at the first failure.
            outcomes: list[ActionOutcome] = []
            terminal = None
            failed = False
            # Only the FIRST action of a batch may reuse the observation taken at the
            # top of the loop; every later one looks again. A step's
            # `observation_before` is its precondition, and handing the whole batch the
            # turn's opening screen would claim that a click on Continue happened on a
            # form with the amount still empty — a precondition a replay would then
            # assert and fail.
            pending: Observation | None = observation

            for action, tool_use_id in zip(batch.actions, batch.tool_use_ids):
                if action.kind in TERMINAL_KINDS:
                    terminal = action
                    outcomes.append(
                        ActionOutcome(tool_use_id=tool_use_id, ok=True, detail="acknowledged")
                    )
                    continue

                if failed:
                    outcomes.append(ActionOutcome(tool_use_id=tool_use_id, ok=False, skipped=True))
                    continue

                step = await self.execute_action(
                    action, observation=pending, model_reason=batch.reason
                )
                pending = None
                outcome = self._outcome_for(step, tool_use_id)
                outcomes.append(outcome)
                if not outcome.ok:
                    failed = True

                escalation = self._escalation_for(step)
                if escalation is not None:
                    self.provider.record_results(outcomes)
                    parked = await self._escalate(escalation, step)
                    # Not reachable for the irreversible gate, which is not recoverable —
                    # but handled rather than assumed, so adding a recoverable reason to
                    # the set later cannot silently return a sentinel as a result.
                    if parked is not RESUME:
                        return parked
                    break

            # 6. Hand the results back and decide whether to continue.
            self.provider.record_results(outcomes)

            if terminal is not None:
                resolved = await self._resolve_terminal(terminal)
                if resolved is not RESUME:
                    return resolved
                continue

            limit = self._check_limits(self._last_observation)
            if limit is not None:
                return limit

    # ---- one action ----

    async def execute_action(
        self,
        action: Any,
        *,
        observation: Observation | None = None,
        model_reason: str | None = None,
    ) -> RecordedStep:
        """Observe → policy → probe → policy → act → observe → record.

        The two policy phases are deliberate and cannot be collapsed: the first
        answers "is this action allowed on this screen", which needs only the URL; the
        second answers "is this TARGET allowed", which is unknowable until the probe
        says what is under the cursor. Running only the first would let a click on the
        commit button through.
        """
        index = len(self.trace.steps)
        before = observation or await self._observe()
        before_png = self._last_png

        decision = self.policy.check_action(action, before)
        probe = None
        probe_unavailable = None

        if decision.decision == "allow" and action.kind in COORDINATE_KINDS:
            try:
                probe = await self.surface.probe(*action.coordinate)
            except ProbeFailed as exc:
                probe_unavailable = f"probe failed: {exc.code}"
            decision = self.policy.check_target(action, probe)
        elif action.kind not in COORDINATE_KINDS:
            probe_unavailable = "action carries no target coordinate"

        error = None
        dispatched_at = time.monotonic()
        if decision.decision == "allow":
            try:
                await self.surface.act(action)
            except SurfaceError as exc:
                error = f"{exc.code}: {exc.message}"
                self._note_target_failure(action)
        else:
            error = f"{decision.decision}: {decision.code}"
        dispatched_ms = int((time.monotonic() - dispatched_at) * 1000)

        after = await self._observe()
        after.dom_changed = before.dom_hash != after.dom_hash
        after.new_text, after.removed_text = _text_diff(
            before.visible_text, after.visible_text
        )
        after_png = self._last_png
        settled_ms = int((time.monotonic() - dispatched_at) * 1000)

        step = RecordedStep(
            index=index,
            action=action.model_dump(mode="json"),
            policy=decision,
            observation_before=before,
            probe=probe,
            probe_unavailable=probe_unavailable,
            observation_after=after,
            timing=Timing(dispatched_ms=dispatched_ms, settled_ms=settled_ms),
            # The batch's own note, not the model's hidden reasoning. A step that records
            # what was clicked but not why is the half a reviewer actually reads.
            model_reason=model_reason,
            error=error,
        )
        self.trace.steps.append(step)
        self.writer.record_step(
            step, before_png=before_png, after_png=after_png, trace=self.trace
        )
        self.on_event(
            "step", index=index, kind=action.kind, decision=decision.decision, error=error
        )
        return step

    # ---- helpers ----

    async def _observe(self) -> Observation:
        observation = await self.surface.observe()
        self._last_observation = observation
        self._last_png = getattr(self.surface, "last_screenshot_png", None)
        return observation

    def _outcome_for(self, step: RecordedStep, tool_use_id: str) -> ActionOutcome:
        """Turn a recorded step into what the model is told.

        A refusal is reported with its REASON. Telling the model only that something
        failed invites it to retry the same thing; telling it the action was
        irreversible usually produces a different, better next move.
        """
        if step.error is None:
            image = (
                self._last_png
                if step.action.get("kind") in {"screenshot", "zoom"}
                else self._last_png
            )
            return ActionOutcome(
                tool_use_id=tool_use_id, ok=True, detail="done", image_png=image
            )

        detail = step.error
        if step.policy.decision == "escalate":
            detail = (
                f"Refused: {step.policy.detail or step.policy.code}. This control is "
                "irreversible and requires human approval. Do not retry it — if the "
                "task is otherwise complete, declare it."
            )
        elif step.policy.decision == "deny":
            detail = f"Refused by policy: {step.policy.detail or step.policy.code}."
        return ActionOutcome(tool_use_id=tool_use_id, ok=False, detail=detail)

    def _escalation_for(self, step: RecordedStep) -> EscalationReason | None:
        """A first reach for the commit button is usually a slip; the model is told why
        and typically declares completion instead. A second is a loop, not a slip."""
        if step.policy.decision != "escalate":
            return None
        self.irreversible_attempts += 1
        if self.irreversible_attempts >= 2:
            return EscalationReason.IRREVERSIBLE_REQUIRES_APPROVAL
        return None

    def _note_target_failure(self, action: Any) -> None:
        key = f"{action.kind}:{getattr(action, 'coordinate', None)}"
        self._target_failures[key] = self._target_failures.get(key, 0) + 1

    async def _handle_exception_states(self, observation: Observation) -> Any | None:
        """Dialogs and overlays, before the model is consulted."""
        if observation.overlays:
            # One bounded wait, then carry on: a loading overlay is a timing artifact,
            # not a decision. Never an unbounded retry loop.
            await self.surface.act(Wait(duration=OVERLAY_WAIT_S))
            observation = await self._observe()

        if observation.dialogs:
            return await self._escalate(EscalationReason.UNKNOWN_DIALOG, None, observation)
        return None

    async def _escalate(
        self,
        reason: EscalationReason,
        step: RecordedStep | None,
        observation: Observation | None = None,
    ) -> Any:
        """Hand the screen to a human.

        Returns `RESUME` if a human took it and gave it back — the run continues — or an
        `Escalated` result if this is where the run ends. Which of the two depends only
        on `RECOVERABLE_ESCALATIONS`, so there is one place to read the policy from.
        """
        self._last_escalation = reason
        intervention_id = "none"
        context = None
        source = observation or self._last_observation
        if source is not None and source.dialogs:
            context = (source.dialogs[0].text or "")[:200]

        if self.session is not None:
            intervention_id = await self.session.escalate(
                str(reason),
                step_index=step.index if step else len(self.trace.steps),
                context=context,
            )
        self.on_event("escalated", reason=str(reason), intervention=intervention_id)

        if self.session is not None and reason in RECOVERABLE_ESCALATIONS:
            if self.handoffs >= self.budget.max_handoffs:
                # Reported as the cap, not as a plain escalation: "we called a human
                # three times and gave up" and "this reason is not recoverable" are
                # different stories, and a reader should not have to count steps to
                # tell them apart.
                return self._fail(
                    FailureCode.MAX_HANDOFFS_EXCEEDED,
                    StopReason.MAX_HANDOFFS,
                    detail=f"{self.handoffs} handoffs already, reason {reason}",
                )
            await self._await_human(intervention_id, reason, context=context)
            return RESUME

        return Escalated(
            run_id=self.trace.run_id,
            intervention_id=intervention_id,
            reason=reason,
            step_index=step.index if step else None,
            stop_reason=StopReason.ESCALATION,
            steps_used=len(self.trace.steps),
            context=context,
        )

    async def _await_human(
        self, intervention_id: str, reason: EscalationReason, *, context: str | None = None
    ) -> RecordedStep:
        """Park until control comes back, then record what the human changed.

        The record is a DOM diff, not a reconstruction of their clicks. We did not watch
        them work, and inventing a coordinate for a step nobody observed would put a
        fiction into the artifact the canonicalizer reads. What changed on screen is both
        true and sufficient at this stage.
        """
        before = self._last_observation or await self._observe()
        before_png = self._last_png

        # Blocks until `session resume` in another terminal, the run's deadline, or a
        # human ending the run outright. All three are handled by the caller.
        await self.session.barrier(deadline=self._deadline())

        after = await self._observe()
        after.dom_changed = before.dom_hash != after.dom_hash
        after.new_text, after.removed_text = _text_diff(
            before.visible_text, after.visible_text
        )

        self.handoffs += 1
        step = RecordedStep(
            index=len(self.trace.steps),
            actor="human",
            action=HumanIntervention(
                intervention_id=intervention_id,
                reason=str(reason),
                operator=getattr(getattr(self.session, "state", None), "operator", None),
                context=context,
            ).model_dump(mode="json"),
            # No policy decision: nobody ran the allowlist against what a person did
            # with their own hands, and an invented `allow` would be worse than none.
            policy=None,
            observation_before=before,
            probe_unavailable="a human acted; no coordinate was proposed",
            observation_after=after,
            model_reason=f"human intervention: {reason}",
        )
        self.trace.steps.append(step)
        self.writer.record_step(
            step, before_png=before_png, after_png=self._last_png, trace=self.trace
        )

        # The model's expectations about the screen are now wrong. Told as an operator
        # message about state, never as an instruction about what to conclude.
        if hasattr(self.provider, "add_system_note"):
            self.provider.add_system_note(self.session.resume_note())

        self.on_event(
            "resumed",
            index=step.index,
            handoffs=self.handoffs,
            changed=after.dom_changed,
            new_text=after.new_text[:3],
            removed_text=after.removed_text[:3],
        )
        return step

    async def _resolve_terminal(self, terminal: Any) -> Any:
        """The model says it is finished. Check."""
        kind = terminal.kind

        if kind == "goal_complete":
            observation = await self._observe()
            passed, missing = self.checkpoint.evaluate(observation)
            if passed:
                return Success(
                    run_id=self.trace.run_id,
                    outputs=getattr(terminal, "outputs", {}) or {},
                    checkpoint_verified=True,
                    stop_reason=StopReason.CHECKPOINT_VERIFIED,
                    steps_used=len(self.trace.steps),
                )
            # The model's claim and the screen disagree. The screen wins.
            return Failure(
                run_id=self.trace.run_id,
                code=FailureCode.CHECKPOINT_FAILED,
                step_index=len(self.trace.steps),
                expected={"required_text": self.checkpoint.required_text},
                observed={
                    "missing": missing,
                    "headings": observation.headings if observation else [],
                },
                stop_reason=StopReason.TERMINAL_DECLARATION,
                steps_used=len(self.trace.steps),
            )

        if kind == "business_outcome":
            # A legitimate domain answer, not a malfunction.
            raw = getattr(terminal, "code", "") or ""
            try:
                code = BusinessOutcomeCode(raw)
            except ValueError:
                code = BusinessOutcomeCode.MEMBER_NOT_FOUND
            return BusinessOutcomeResult(
                run_id=self.trace.run_id,
                code=code,
                detail=getattr(terminal, "detail", None),
                stop_reason=StopReason.TERMINAL_DECLARATION,
                steps_used=len(self.trace.steps),
            )

        if kind == "request_human":
            return await self._escalate(EscalationReason.MODEL_REQUESTED, None)

        return self._fail(
            FailureCode.POLICY_DENIED,
            StopReason.TERMINAL_DECLARATION,
            detail=getattr(terminal, "reason", "cannot proceed"),
        )

    def _check_limits(
        self, observation: Observation | None, *, record_progress: bool = False
    ) -> Any | None:
        """Every limit has its own code, so a reader never has to guess which fired.

        `record_progress` is set only by the top of the loop, so the no-progress rule
        counts one sample per iteration.
        """
        if len(self.trace.steps) >= self.budget.max_steps:
            return self._fail(FailureCode.MAX_STEPS_EXCEEDED, StopReason.MAX_STEPS)

        if time.monotonic() - self.started_at > self.budget.wall_clock_s:
            return self._fail(FailureCode.WALL_CLOCK_EXCEEDED, StopReason.WALL_CLOCK)

        usage = getattr(self.provider, "usage", None)
        if usage is not None and usage.usd_estimate > self.budget.max_usd:
            return self._fail(
                FailureCode.BUDGET_EXCEEDED,
                StopReason.BUDGET,
                detail=f"${usage.usd_estimate:.4f} over ${self.budget.max_usd:.2f}",
            )

        if any(count >= MAX_SAME_TARGET_FAILURES for count in self._target_failures.values()):
            return self._fail(FailureCode.REPEATED_TARGET_FAILURE, StopReason.NO_PROGRESS)

        # No progress: the same screen, unchanged, several times over. Both hashes are
        # consulted because a page can repaint without changing and change without
        # repainting much.
        if record_progress and observation is not None and observation.observation_hash:
            self._recent_hashes.append(observation.observation_hash)
            self._recent_hashes = self._recent_hashes[-MAX_UNCHANGED_OBSERVATIONS:]
            if (
                len(self._recent_hashes) == MAX_UNCHANGED_OBSERVATIONS
                and len(set(self._recent_hashes)) == 1
                and observation.dom_changed is not True
            ):
                return self._fail(
                    FailureCode.NO_PROGRESS,
                    StopReason.NO_PROGRESS,
                    detail=f"{MAX_UNCHANGED_OBSERVATIONS} identical observations",
                )
        return None

    def _close_out_session(self) -> None:
        """If the run ended while a human still held (or was owed) the screen, say so.

        Otherwise `intervention.json` keeps reading `HUMAN_PENDING` for a run that is
        over, and `session status` reports a parked run that nothing is waiting on —
        which is exactly what made a real Ctrl-C mid-handoff confusing to diagnose.

        The audit trail is append-only, so the park itself stays in `transitions`; this
        only adds the ending. A run that finished with the automation in control is left
        untouched: nothing about it is misleading.
        """
        session = self.session
        state = getattr(session, "state", None)
        if session is None or state is None:
            return
        if state.is_terminal or state.automation_may_act:
            return
        try:
            session.cancel(control_version=state.control_version, operator="run ended")
        except Exception:  # noqa: BLE001 - tidying must never mask the run's own result
            pass

    def _record_spend(self) -> None:
        """Copy what the run actually consumed onto the trace, just before it is closed.

        Written here rather than by the caller because the caller writes it too late:
        `finish()` has already serialized the trace by the time it returns, so a budget
        assigned afterwards never reached disk at all.

        For a scripted provider these token counts are synthetic. `provider.name` in the
        same trace says which it was, so the two are never confusable.
        """
        usage = getattr(self.provider, "usage", None)
        if usage is None:
            return
        self.trace.budget.input_tokens = usage.input_tokens
        self.trace.budget.output_tokens = usage.output_tokens
        self.trace.budget.usd_estimate = round(usage.usd_estimate, 6)

    def _fail(self, code: FailureCode, stop: StopReason, *, detail: str = "") -> Failure:
        return Failure(
            run_id=self.trace.run_id,
            code=code,
            step_index=len(self.trace.steps) or None,
            observed={"detail": detail} if detail else {},
            stop_reason=stop,
            steps_used=len(self.trace.steps),
        )

    def _deadline(self) -> float:
        return self.started_at + self.budget.wall_clock_s


def _text_diff(before: str | None, after: str | None) -> tuple[list[str], list[str]]:
    """What appeared on screen, and what left it.

    `dom_changed` says something happened; this says WHAT, which is what a reviewer
    reads the trace for. Diffed by word rather than by line because the agent flattens
    each frame to one line — a line diff would only ever report "the frame changed".

    Both directions, because an additions-only diff described a human dismissing a
    dialog as no change at all: the entire event was a disappearance.
    """
    if before == after:
        return [], []
    old, new = (before or "").split(), (after or "").split()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)

    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            added.append(" ".join(new[j1:j2])[:MAX_NEW_TEXT_CHARS])
        if tag in {"delete", "replace"}:
            removed.append(" ".join(old[i1:i2])[:MAX_NEW_TEXT_CHARS])
    return (
        [p for p in added if p][:MAX_NEW_TEXT_PHRASES],
        [p for p in removed if p][:MAX_NEW_TEXT_PHRASES],
    )
