"""
Executing a capability artifact. No model, at any point.

The shape is deliberately the same as `DiscoveryController` — observe, policy, target, policy, act,
observe, record — because that is the claim being made: the safety and evidence machinery is not something
the model needed, it is something the *system* has. Replay runs through the same `PolicyEngine`, writes
through the same `EvidenceWriter`, parks through the same `SessionManager`, and returns the same
`RunResult` union. What differs is only where the actions come from: a file a human reviewed, instead of a
model deciding.

Three places where that reuse took work rather than just happening:

  * **The policy vocabulary.** `check_action` refuses anything outside `allowed_action_kinds`, which
    defaults to the computer-use verbs. The artifact's are `click`/`fill`/`select`/`check`/`read`, so the
    engine passes them in — `allowed_action_kinds` has always been a constructor parameter. The engine is
    unchanged; it is configured. The vocabulary gate still fires on anything outside those five.
  * **The irreversible gate.** `check_target` classifies risk from the element actually under the cursor,
    and replay has no cursor. So it reads the *resolved* element with `describe_element` and hands over a
    real `ProbeResult`. Skipping this because "the artifact was reviewed" is how the gate becomes
    decoration: the artifact was reviewed against a page where `Continue` was safe, and cannot know the
    button is now `danger-button`.
  * **Bounds.** No model means no token budget, but a run can still hang on a page that never settles. The
    wall clock and max-steps guards stay, because "what stops this?" should have the same answer in both
    layers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.domain.artifact import Capability, Step, substitute
from src.domain.results import (
    BusinessOutcomeResult,
    Escalated,
    EscalationReason,
    Failure,
    FailureCode,
    StopReason,
    Success,
)
from src.domain.trace import Observation, PolicyDecision, RecordedStep, RunTrace, Timing
from src.replay.conditions import ConditionResult, evaluate, wait_for
from src.replay.extract import ExtractionError, extract_outputs
from src.replay.locator_resolver import TargetAmbiguous, TargetResolutionError, resolve
from src.replay.recovery import UnrecoverableDialog, recover
from src.sessions.manager import InterventionTimeout, RunEndedByHuman
from src.surfaces.base import SurfaceError

# What the artifact's steps are allowed to be. Passed to `PolicyEngine(allowed_action_kinds=...)`, so the
# vocabulary check still runs — a step kind outside this set is denied, exactly as an unknown computer-use
# verb is on the discovery side.
REPLAY_ACTION_KINDS: frozenset[str] = frozenset({"click", "fill", "select", "check", "read"})


@dataclass
class Bounds:
    """What stops the run. No token budget — there is nothing to spend."""

    max_steps: int = 50
    wall_clock_s: float = 300.0
    # A run that keeps handing back to a human is not making progress either, and without this a
    # dialog that returns after every resume would loop until the wall clock.
    max_handoffs: int = 3


class _Parked:
    """Returned when a step handed control to a human instead of finishing.

    Not a result: the run has not ended, it is waiting. The loop retries the same step once the barrier
    opens, which is the whole point of a handoff — a person fixes the screen and the automation carries on
    from where it stopped rather than starting again.
    """


PARKED = _Parked()


@dataclass
class _Action:
    """The artifact's action, in the shape `PolicyEngine` reads.

    Not a new model: `check_action` only ever asks for `.kind`, and `action_digest` dumps whatever it is
    given. A dataclass keeps the artifact's vocabulary out of `src/domain/actions.py`, which is the
    computer-use tool's surface and has no business growing replay verbs.
    """

    kind: str
    value: str | None = None
    step_id: str = ""

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        # `RecordedStep.action` is a plain dict, and the evidence writer redacts it on the way out — so the
        # substituted value is safe to record here and is what a reviewer needs to see.
        return {"kind": self.kind, "value": self.value, "step_id": self.step_id}


@dataclass
class ReplayEngine:
    """One replay run."""

    surface: Any
    policy: Any
    writer: Any
    trace: RunTrace
    capability: Capability
    inputs: dict[str, Any]
    session: Any | None = None
    bounds: Bounds = field(default_factory=Bounds)
    on_event: Any = None

    _started: float = field(default=0.0, init=False)
    _handoffs: int = field(default=0, init=False)
    _last_png: bytes | None = field(default=None, init=False)

    # ---- the run ----

    async def run(self) -> Any:
        """Execute every step, or stop with a reason. Always leaves a summary behind."""
        self._started = time.monotonic()
        self.trace.started_at = self.trace.started_at or _now()
        self._emit("run_started", steps=len(self.capability.steps))

        result: Any
        try:
            result = await self._loop()
        except RunEndedByHuman as exc:
            result = self._failure(
                FailureCode.HUMAN_ENDED_RUN, StopReason.HUMAN_ENDED_RUN, detail=str(exc)
            )
        except InterventionTimeout as exc:
            result = self._failure(
                FailureCode.WALL_CLOCK_EXCEEDED, StopReason.WALL_CLOCK, detail=str(exc)
            )
        except Exception as exc:  # noqa: BLE001
            # A crash with no summary is the worst evidence outcome there is: the run is gone and the
            # folder cannot say why. Record it, then re-raise so the failure is not swallowed.
            result = self._failure(
                FailureCode.PROVIDER_ERROR,
                StopReason.PROVIDER_ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )
            self._finish(result)
            raise

        self._finish(result)
        return result

    async def _loop(self) -> Any:
        index = 0
        while index < len(self.capability.steps):
            step = self.capability.steps[index]
            if (exceeded := self._limit_hit(index)) is not None:
                return exceeded

            if self.session is not None:
                # Blocks while a human holds the screen, and un-pauses the surface on their `resume`.
                await self.session.barrier(deadline=self._deadline())
                self.session.assert_automation_owns()

            observation = await self._observe(step)

            # Preconditions before anything else touches the screen: a step whose screen is not there yet
            # has not failed, it has not started.
            if step.preconditions:
                met, results = await wait_for(
                    self.surface,
                    self._frame_of(step),
                    list(step.preconditions),
                    inputs=self.inputs,
                    named_dialogs=self.capability.named_dialogs,
                    timeout_ms=step.timeout_ms,
                )
                if not met:
                    return self._condition_failure(step, index, results, "precondition")
                observation = await self._observe(step)

            # Exception states get a say before the step runs, because a modal over the page means the
            # click would land somewhere nobody chose.
            handled = await self._handle_outcome_rules(observation, step, index)
            if handled is PARKED:
                continue  # a human has the screen; retry this step when they give it back
            if handled is not None:
                return handled

            outcome = await self._execute(step, index, observation)
            if outcome is PARKED:
                continue
            if outcome is not None:
                return outcome
            index += 1

        return await self._succeed()

    # ---- one step ----

    async def _execute(self, step: Step, index: int, before: Observation) -> Any | None:
        """Resolve, authorise, act, verify, record. Returns a terminal result or None to continue."""
        frame_path = self._frame_of(step)
        action = _Action(
            kind=step.action.kind,
            value=self._value_of(step),
            step_id=step.id,
        )
        target = getattr(step.action, "target", None)
        locator = None
        probe = None
        probe_unavailable = None

        # Phase 1: knowable from the screen alone.
        decision = self.policy.check_action(action, before)

        if decision.decision == "allow" and target is not None:
            try:
                locator, attempts = await resolve(self.surface, target, timeout_ms=step.timeout_ms)
            except TargetResolutionError as exc:
                self._record(step, index, action, decision, before, error=str(exc))
                code = (
                    FailureCode.TARGET_AMBIGUOUS
                    if isinstance(exc, TargetAmbiguous)
                    else FailureCode.TARGET_NOT_RESOLVED
                )
                return self._failure(
                    code,
                    StopReason.INVALID_ACTIONS,
                    step_index=index,
                    detail=f"{step.id}: {exc}",
                    observed={"attempts": [str(a) for a in exc.attempts]},
                )
            self._emit("resolved", step=step.id, via=attempts[-1].kind)

            # Phase 2: what is actually there. This is the irreversible gate.
            probe = await self.surface.describe_element(locator, frame_path)
            decision = self.policy.check_target(action, probe)
        elif target is None:
            probe_unavailable = "read-only step: no target to classify"

        if decision.decision != "allow":
            self._record(step, index, action, decision, before, probe=probe,
                         probe_unavailable=probe_unavailable,
                         error=f"{decision.decision}: {decision.code}")
            return self._refused(decision, index, step)

        error = None
        dispatched = time.monotonic()
        if target is not None:
            try:
                await self._dispatch(step, action, locator)
            except SurfaceError as exc:
                error = f"{type(exc).__name__}: {exc}"
        dispatched_ms = int((time.monotonic() - dispatched) * 1000)

        after = await self._observe(step)
        after.dom_changed = before.dom_hash != after.dom_hash

        checks = list(step.postconditions) + (step.checkpoint.all if step.checkpoint else [])
        results: list[ConditionResult] = []
        if checks and error is None:
            met, results = await wait_for(
                self.surface,
                frame_path,
                checks,
                locator=locator,
                inputs=self.inputs,
                named_dialogs=self.capability.named_dialogs,
                timeout_ms=step.timeout_ms,
                baseline=before,
            )
        else:
            met = error is None

        self._record(
            step, index, action, decision, before,
            probe=probe, probe_unavailable=probe_unavailable, after=after,
            timing=Timing(dispatched_ms=dispatched_ms,
                          settled_ms=int((time.monotonic() - dispatched) * 1000)),
            error=error,
        )

        if error is not None:
            return self._failure(
                FailureCode.SURFACE_UNAVAILABLE, StopReason.INVALID_ACTIONS,
                step_index=index, detail=f"{step.id}: {error}",
            )
        if not met:
            # A rule may explain the failure — the app refusing is a business outcome, not a broken
            # replay. Checked here rather than before, so an ordinary step is not searched for excuses.
            explained = await self._handle_outcome_rules(after, step, index)
            if explained is not None:
                return explained
            kind = "checkpoint" if step.checkpoint else "postcondition"
            return self._condition_failure(step, index, results, kind)
        return None

    async def _dispatch(self, step: Step, action: _Action, locator: Any) -> None:
        kind = action.kind
        if kind == "click":
            await self.surface.click(locator, timeout_ms=step.timeout_ms)
        elif kind == "fill":
            await self.surface.fill(locator, action.value or "", timeout_ms=step.timeout_ms)
        elif kind == "select":
            await self.surface.select(locator, action.value or "", timeout_ms=step.timeout_ms)
        elif kind == "check":
            await self.surface.check(locator, timeout_ms=step.timeout_ms)
        elif kind == "read":
            await self.surface.read(locator, timeout_ms=step.timeout_ms)
        else:  # pragma: no cover - the policy vocabulary gate refuses this first
            raise SurfaceError(f"no replay dispatch for action kind {kind!r}")

    # ---- outcome rules ----

    async def _handle_outcome_rules(
        self, observation: Observation, step: Step, index: int
    ) -> Any | None:
        """First matching rule wins, in artifact order.

        Order is the artifact's, not a scoring function's: a reviewer reading the rules top to bottom sees
        the precedence, and the `unmatched` dialog catch-all is last on purpose.
        """
        for rule in self.capability.outcome_rules:
            result = await evaluate(
                rule.when,
                observation,
                surface=self.surface,
                frame_path=self._frame_of(step),
                inputs=self.inputs,
                named_dialogs=self.capability.named_dialogs,
            )
            if not result.passed:
                continue

            self._emit("outcome_rule", step=step.id, when=rule.when.kind, matched=str(result))

            if rule.recover is not None:
                try:
                    recovered = await recover(
                        self.surface, rule, self.capability,
                        frame_path=self._frame_of(step), inputs=self.inputs,
                    )
                except UnrecoverableDialog as exc:
                    return await self._escalate(EscalationReason.UNKNOWN_DIALOG, index, str(exc))
                self.writer.event("recovery", step=step.id, strategy=recovered.strategy,
                                  attempts=recovered.attempts, cleared=recovered.cleared,
                                  detail=recovered.detail)
                self._emit("recovery", step=step.id, detail=str(recovered))
                if recovered.cleared:
                    return None
                # Bounded, and it did not clear. The rule's `else:` is the author's answer for that; with
                # none, the honest report is that the condition the rule was written for never cleared —
                # NOT an unknown dialog, which is what this used to say even for an overlay. A replay under
                # `--fault overlay` then escalated with a reason naming a dialog that was never on screen.
                if rule.else_ is None:
                    return self._failure(
                        FailureCode.NO_PROGRESS, StopReason.NO_PROGRESS,
                        step_index=index,
                        detail=f"{rule.when.kind} never cleared: {recovered.detail}",
                    )
                return self._from_return(rule.else_, index)

            if rule.return_ is not None:
                if rule.return_.status == "escalated":
                    return await self._escalate(
                        EscalationReason(rule.return_.reason or "POLICY_ESCALATION"),
                        index,
                        result.actual,
                    )
                return self._from_return(rule.return_, index)
        return None

    def _from_return(self, spec: Any, index: int) -> Any:
        """An `OutcomeReturn` becomes the matching `RunResult` member.

        The status/code pairing is validated when the artifact loads, so this does not have to guess which
        vocabulary a code belongs to. It used to be able to: the committed artifact paired
        `status: failure` with `VALIDATION_REJECTED`, a business-outcome code, and nothing noticed until
        this function tried to build a `Failure` out of it.
        """
        if spec.status == "business_outcome":
            return BusinessOutcomeResult(
                run_id=self.trace.run_id,
                code=spec.code,
                detail=spec.reason,
                stop_reason=StopReason.TERMINAL_DECLARATION,
                steps_used=len(self.trace.steps),
            )
        if spec.status == "failure":
            return self._failure(
                FailureCode(spec.code), StopReason.TERMINAL_DECLARATION,
                step_index=index, detail=spec.reason or "",
            )
        return Success(
            run_id=self.trace.run_id,
            stop_reason=StopReason.TERMINAL_DECLARATION,
            steps_used=len(self.trace.steps),
        )

    # ---- endings ----

    async def _succeed(self) -> Any:
        """Every step ran. Extract the declared outputs and report what was verified.

        `checkpoint_verified` is `True` only when a checkpoint existed *and* passed — the same rule
        discovery uses. An artifact with no checkpoint gets a success with the flag unset, because nothing
        confirmed anything.
        """
        frame_path = self.capability.entry.frame_path
        try:
            outputs = await extract_outputs(self.surface, frame_path, self.capability.contract)
        except ExtractionError as exc:
            # The contract promised these fields. Not delivering them is not a success.
            return self._failure(
                FailureCode.CHECKPOINT_FAILED, StopReason.CHECKPOINT_VERIFIED,
                detail=f"outputs could not be extracted: {exc}",
            )

        verified = any(s.checkpoint is not None for s in self.capability.steps)
        return Success(
            run_id=self.trace.run_id,
            outputs=outputs,
            checkpoint_verified=verified,
            stop_reason=StopReason.CHECKPOINT_VERIFIED if verified else StopReason.TERMINAL_DECLARATION,
            steps_used=len(self.trace.steps),
        )

    async def _escalate(self, reason: EscalationReason, index: int, context: str) -> Any:
        """Hand the screen to a human, and wait rather than giving up.

        An escalation is a pause, not an ending. The screen is parked, the operator is told what to do, and
        the next `barrier()` blocks until they resume — at which point the *same step* runs again against
        the screen they fixed. That is the control transfer the handoff exists for, and it works the same
        way here as in discovery because it is the same `SessionManager`.

        It becomes terminal in two cases: there is nobody to hand to (no session), or the run has already
        handed over `max_handoffs` times, which is the bound that stops a returning dialog from parking the
        run forever.
        """
        if self.session is None:
            return Escalated(
                run_id=self.trace.run_id,
                intervention_id="none",
                reason=reason,
                step_index=index,
                context=f"{context} (no session: nobody to hand the screen to)",
                stop_reason=StopReason.ESCALATION,
                steps_used=len(self.trace.steps),
            )

        self._handoffs += 1
        if self._handoffs > self.bounds.max_handoffs:
            return self._failure(
                FailureCode.MAX_HANDOFFS_EXCEEDED, StopReason.MAX_HANDOFFS,
                step_index=index,
                detail=f"{self.bounds.max_handoffs} handoff(s) already taken; {reason} again",
            )

        intervention = await self.session.escalate(str(reason), step_index=index, context=context)
        self._emit("escalated", reason=str(reason), intervention=intervention)
        self.writer.event("escalated", reason=str(reason), intervention=intervention,
                          step_index=index, context=context)
        return PARKED

    def _refused(self, decision: PolicyDecision, index: int, step: Step) -> Any:
        """Policy said no. `escalate` and `deny` are different answers and stay different.

        An irreversible control is `escalate` — a human *can* authorise it. Anything else is `deny`, which
        nothing can. The engine does not read code strings to tell them apart.
        """
        if decision.decision == "escalate":
            return Escalated(
                run_id=self.trace.run_id,
                intervention_id=f"policy_{step.id}",
                reason=EscalationReason(decision.code or "POLICY_ESCALATION"),
                step_index=index,
                context=decision.detail,
                stop_reason=StopReason.ESCALATION,
                steps_used=len(self.trace.steps),
            )
        return self._failure(
            FailureCode.POLICY_DENIED, StopReason.INVALID_ACTIONS,
            step_index=index, detail=f"{step.id}: {decision.detail}",
            observed={"rule": decision.rule, "code": decision.code},
        )

    def _condition_failure(
        self, step: Step, index: int, results: list[ConditionResult], kind: str
    ) -> Failure:
        """Expected vs observed, per condition. What makes a failure diagnosable without re-running it."""
        failed = [r for r in results if not r.passed]
        return Failure(
            run_id=self.trace.run_id,
            code=FailureCode.CHECKPOINT_FAILED,
            step_index=index,
            stop_reason=StopReason.CHECKPOINT_VERIFIED,
            steps_used=len(self.trace.steps),
            expected={f"{kind}:{r.kind}": r.expected for r in failed},
            observed={f"{kind}:{r.kind}": r.actual for r in failed},
        )

    def _limit_hit(self, index: int) -> Failure | None:
        if index >= self.bounds.max_steps:
            return self._failure(
                FailureCode.MAX_STEPS_EXCEEDED, StopReason.MAX_STEPS,
                detail=f"{self.bounds.max_steps} step limit",
            )
        if time.monotonic() - self._started > self.bounds.wall_clock_s:
            return self._failure(
                FailureCode.WALL_CLOCK_EXCEEDED, StopReason.WALL_CLOCK,
                detail=f"{self.bounds.wall_clock_s:.0f}s limit",
            )
        return None

    def _failure(
        self,
        code: FailureCode,
        stop: StopReason,
        *,
        step_index: int | None = None,
        detail: str = "",
        observed: dict[str, Any] | None = None,
    ) -> Failure:
        return Failure(
            run_id=self.trace.run_id,
            code=code,
            step_index=step_index,
            stop_reason=stop,
            steps_used=len(self.trace.steps),
            expected={"detail": detail} if detail else {},
            observed=observed or {},
        )

    # ---- plumbing ----

    def _value_of(self, step: Step) -> str | None:
        raw = getattr(step.action, "value", None)
        return substitute(raw, self.inputs) if raw else None

    def _frame_of(self, step: Step) -> list[str] | None:
        target = getattr(step.action, "target", None)
        return target.frame_path if target else self.capability.entry.frame_path

    async def _observe(self, step: Step) -> Observation:
        observation = await self.surface.observe(self._frame_of(step))
        self._last_png = await self._screenshot()
        return observation

    async def _screenshot(self) -> bytes | None:
        try:
            return await self.surface.capture_evidence()
        except Exception:  # noqa: BLE001 - a missing screenshot must not end a run
            return None

    def _record(
        self,
        step: Step,
        index: int,
        action: _Action,
        decision: PolicyDecision,
        before: Observation,
        *,
        probe: Any = None,
        probe_unavailable: str | None = None,
        after: Observation | None = None,
        timing: Timing | None = None,
        error: str | None = None,
    ) -> None:
        recorded = RecordedStep(
            index=index,
            action=action.model_dump(mode="json"),
            policy=decision,
            observation_before=before,
            probe=probe,
            probe_unavailable=probe_unavailable if probe is None else None,
            observation_after=after,
            timing=timing or Timing(),
            model_reason=f"artifact step {step.id!r}",
            error=error,
        )
        self.trace.steps.append(recorded)
        self.writer.record_step(recorded, after_png=self._last_png, trace=self.trace)
        self._emit("step", index=index, kind=action.kind, step=step.id,
                   decision=decision.decision, error=error)

    def _finish(self, result: Any) -> None:
        self.writer.finish(
            self.trace, result,
            final_png=self._last_png,
            stop_reason=str(result.stop_reason) if result.stop_reason else None,
        )

    def _deadline(self) -> float:
        return self._started + self.bounds.wall_clock_s

    def _emit(self, event: str, **fields: Any) -> None:
        # `event`, not `kind`: a step's own `kind` is one of the fields, and the two collided.
        if self.on_event is not None:
            self.on_event(event, **fields)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
