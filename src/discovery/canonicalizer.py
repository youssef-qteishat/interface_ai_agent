"""
Trace in, capability out.

A trace says what happened once, at some coordinates, with a human filling two holes. A capability says
what should happen every time, in terms replay can resolve. The distance between those is this module.

It reduces hard: twenty-one recorded steps become seven. Most of what goes is noise — waits and
screenshots the model asked for to see where it was — but two of the removals are judgments worth
stating, because they are where a naive canonicalizer produces something that looks right and replays
wrong:

  * **A rejected attempt and its retry are one step.** The model clicked Continue, the app refused, a
    human fixed the cause, the model clicked the identical thing again. The artifact needs one click —
    and the refusal message, because it names the precondition that was missing.
  * **A human step is not a step.** It is either a recoverable condition the artifact can describe, or
    a hole the artifact must admit to. Deciding which is `detect_gaps`.

What this module deliberately does NOT derive is the contract — the input types, the output extractors,
the capability's name and risk. Those come from a `CapabilitySpec`. Deriving a public API from one
observed run is overfitting: every input would come out `type: string`, and `account_type` being an
enum of exactly two values is a fact about the domain, not about the afternoon this run happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.discovery.locator_ranking import rank_candidates
from src.domain.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    Capability,
    CapabilityMeta,
    CheckedCondition,
    Checkpoint,
    ClickAction,
    Condition,
    Contract,
    DialogCondition,
    Entry,
    FillAction,
    Gap,
    GapEvidence,
    HeadingCondition,
    InputSpec,
    OutcomeReturn,
    OutcomeRule,
    OutputSpec,
    Policy,
    Provenance,
    ReadAction,
    Recover,
    Step,
    Target,
    TargetEvidence,
    TextAbsentCondition,
    TextCondition,
    UrlCondition,
    ValueCondition,
)
from src.domain.trace import Observation, RecordedStep, RunTrace

# Steps that exist so the model could see, not so the workflow could advance. A `wait` step in
# particular must never survive: replay waits on conditions, never on a clock.
NOISE_KINDS = frozenset({"wait", "screenshot", "zoom", "cursor_position"})

INPUT_REF = re.compile(r"\$\{inputs\.([A-Za-z_][A-Za-z0-9_]*)\}")

# Anything matching these must never reach an artifact. Mostly guaranteed upstream — the ranker cannot
# emit a coordinate and redaction ran at discovery time — but asserted anyway, because "it cannot
# happen" is what every leak was before it happened.
GENERATED_ID = re.compile(r"\b(?:inp|amt|sel|chk|fund|modal|btn)_[0-9a-f]{6,}\b")
SECRET_SHAPED = re.compile(r"(?i)\b(?:cookie|set-cookie|authorization|bearer|sk-[A-Za-z0-9]{8,})\b")


class CanonicalizationError(Exception):
    """The trace cannot become a valid artifact, or the artifact would leak."""


@dataclass(frozen=True)
class CapabilitySpec:
    """The half of an artifact a single run cannot know.

    Naming, risk classification, input types and output extractors are all judgments about the domain.
    A trace can tell you `member_id` was typed into a field; it cannot tell you it is always five
    digits, or that the review panel's rows map to these four output fields.
    """

    id: str
    title: str
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    risk: str = "reversible"
    version: str = "1.0.0"
    # Conditions this run never met — "No members found" was never on screen, because the member was
    # found. They belong to the capability all the same.
    extra_outcome_rules: tuple[OutcomeRule, ...] = ()


# --------------------------------------------------------------------------- #
# reading the trace
# --------------------------------------------------------------------------- #


def _inner_url(observation: Observation | None) -> str | None:
    """The URL of the deepest frame.

    Not `main_frame_url`: the shell never navigates, so a URL assertion reading it would be true on
    every screen of the workflow and therefore assert nothing.
    """
    if observation is None:
        return None
    nested = [f.url for f in observation.frames if f.path]
    return nested[-1] if nested else observation.main_frame_url


def _path_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    return (parts.path + (f"?{parts.query}" if parts.query else "")) or None


def _origins(trace: RunTrace) -> list[str]:
    seen: list[str] = []
    for step in trace.steps:
        for observation in (step.observation_before, step.observation_after):
            for url in (observation.frame_urls if observation else []):
                parts = urlsplit(url)
                origin = f"{parts.scheme}://{parts.netloc}"
                if parts.netloc and origin not in seen:
                    seen.append(origin)
    return seen


def _referenced_input_names(trace: RunTrace) -> set[str]:
    """Every input the run knows about — including from the goal.

    The goal matters more than it looks. `${inputs.account_type}` appears exactly once in the whole
    trace, in the goal string, because the model never touched the dropdown. That single mention is
    what lets the gap detector notice the input is declared and unset.
    """
    blob = trace.goal + trace.to_yaml()
    return set(INPUT_REF.findall(blob))


# --------------------------------------------------------------------------- #
# the reduction
# --------------------------------------------------------------------------- #


def _slug(step: RecordedStep, probe: Any, used: set[str]) -> str:
    """A readable, stable id.

    Three sources in order, and the first two exclusions are the same judgments the locator ladder
    makes: a placeholder-derived name (`$0.00`) names nothing, and a name that is itself an input
    reference would put the member id in a step id. Falls back to the enclosing region, then the
    index, so a nameless control still gets something a reviewer can point at.
    """
    names = []
    if probe:
        if probe.accessible_name_source != "placeholder":
            names.append(probe.accessible_name)
        names.append(probe.nearby_label)
        # `div#results-panel` -> `results-panel`. A bare tag (`form`) names nothing, so it is skipped
        # in favour of the index fallback.
        region = probe.enclosing_region or ""
        if "#" in region:
            names.append(region.split("#", 1)[1])
    name = next((n for n in names if n and not INPUT_REF.search(n)), None)
    if name:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    else:
        base = f"{step.action['kind'].replace('_', '-')}-{step.index}"
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}-{n}", n + 1
    used.add(candidate)
    return candidate


def _target(step: RecordedStep) -> Target:
    kept, rejected = rank_candidates(step.probe)
    if not kept:
        raise CanonicalizationError(
            f"trace step {step.index}: no candidate survived ranking, so no replayable target can be "
            f"written. Rejections: " + "; ".join(f"{r.kind}: {r.reason}" for r in rejected)
        )
    return Target(
        frame_path=step.probe.frame_path,
        candidates=kept,
        evidence=TargetEvidence(
            discovery_coordinate=tuple(step.action["coordinate"]),
            expected_tag=step.probe.tag,
            expected_role=step.probe.role,
            rejected=rejected,
        ),
    )


def _postconditions(step: RecordedStep, value: str | None, absent: str | None) -> list[Condition]:
    """Three rules only.

    Deliberately narrow: a postcondition derived from an incidental detail of one run becomes a
    false failure on the next. A navigation, a new heading and "the value I typed is in the field"
    are the three things that are true by construction rather than by coincidence.
    """
    conditions: list[Condition] = []
    before, after = step.observation_before, step.observation_after

    before_url, after_url = _inner_url(before), _inner_url(after)
    if after_url and after_url != before_url:
        path = _path_of(after_url)
        if path:
            conditions.append(UrlCondition(contains=path))

    old_headings = set(before.headings if before else [])
    new_headings = [h for h in (after.headings if after else []) if h not in old_headings]
    if new_headings:
        conditions.append(HeadingCondition(contains=new_headings[0]))

    if value is not None:
        conditions.append(ValueCondition(equals=value))

    # From a deduped attempt: the message the app showed the first time must be gone this time.
    if absent:
        conditions.append(TextAbsentCondition(contains=absent))

    return conditions


def _reduce(trace: RunTrace) -> tuple[list[Step], dict[str, int], list[RecordedStep], str | None]:
    """Trace steps → artifact steps.

    Returns the steps, a map of step id → originating trace index (the gap detector needs it),
    the human steps for classification, and the validation message a dedupe found.
    """
    steps: list[Step] = []
    derived_from: dict[str, int] = {}
    humans: list[RecordedStep] = []
    used_ids: set[str] = set()
    dedup_message: str | None = None

    # (coordinate, kind) -> index into `steps`, for spotting a retry of the same target.
    seen_targets: dict[tuple, int] = {}
    index = 0

    while index < len(trace.steps):
        step = trace.steps[index]
        kind = step.action.get("kind")

        if step.actor == "human":
            humans.append(step)
            index += 1
            continue

        if kind in NOISE_KINDS:
            index += 1
            continue

        if kind not in {"left_click", "double_click"}:
            # `type` is only ever reached through the click that focused the field (below); anything
            # else has no target and no place in the artifact.
            index += 1
            continue

        # Collapse click-then-type: the click supplies the target, the type supplies the value.
        value: str | None = None
        following = trace.steps[index + 1] if index + 1 < len(trace.steps) else None
        if following is not None and following.action.get("kind") == "type":
            value = following.action.get("text")

        signature = (tuple(step.action["coordinate"]), kind)
        previous = seen_targets.get(signature)
        if previous is not None:
            # The same target, twice. The first attempt was rejected by the app — keep its message as
            # the evidence, and let the surviving step assert the message is gone.
            rejected_text = (step_new_text(trace.steps[derived_from_index(steps, previous, derived_from)]))
            dedup_message = dedup_message or rejected_text
            if rejected_text:
                steps[previous].postconditions.append(TextAbsentCondition(contains=rejected_text))
            index += 2 if value is not None else 1
            continue

        target = _target(step)
        step_id = _slug(step, step.probe, used_ids)
        action = (
            FillAction(value=value, target=target)
            if value is not None
            else ClickAction(target=target)
        )
        steps.append(
            Step(
                id=step_id,
                action=action,
                postconditions=_postconditions(step, value, absent=None),
            )
        )
        derived_from[step_id] = step.index
        seen_targets[signature] = len(steps) - 1
        index += 2 if value is not None else 1

    return steps, derived_from, humans, dedup_message


def step_new_text(step: RecordedStep) -> str | None:
    after = step.observation_after
    return after.new_text[0] if after and after.new_text else None


def derived_from_index(steps: list[Step], position: int, derived_from: dict[str, int]) -> int:
    return derived_from[steps[position].id]


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #


def _evidence_before(trace: RunTrace, human_index: int) -> tuple[int | None, str | None]:
    """Walk back from a human step to the last thing the screen said.

    The operator ticked a checkbox, which changes no visible text — so the intervention itself records
    nothing. What localises it is the message that drove them to intervene, a few steps earlier.
    Bounded by the previous human step, so one intervention cannot borrow another's evidence.
    """
    for index in range(human_index - 1, -1, -1):
        step = trace.steps[index]
        if step.actor == "human":
            break
        text = step_new_text(step)
        if text:
            return step.index, text
    return None, None


def detect_gaps(
    trace: RunTrace,
    capability: Capability,
    derived_from: dict[str, int],
    humans: list[RecordedStep],
) -> list[Gap]:
    """Everything the artifact needs and the trace could not supply.

    Two rules, from two unrelated causes, and the second is the one nobody expects:

      A. a human acted and nothing observable changed, so their action cannot be derived;
      B. an input is declared and no step enters it — which happens when a default silently
         coincided with what the run wanted.
    """
    gaps: list[Gap] = []

    # Rule A — the invisible intervention.
    for human in humans:
        after = human.observation_after
        changed = bool(after and (after.dom_changed or after.new_text or after.removed_text))
        if changed:
            continue  # visible: it is a recoverable condition, and _outcome_rules describes it
        trace_step, evidence = _evidence_before(trace, human.index)
        gaps.append(
            Gap(
                step_after=_step_preceding(trace_step, capability.steps, derived_from),
                detected_from=GapEvidence(
                    trace_step=trace_step, human_step=human.index, evidence=evidence
                ),
                reason=(
                    "A human intervened and no observable state changed, so the action they took "
                    "could not be derived. The message that preceded the intervention names the "
                    "control."
                ),
            )
        )

    # Rule B — declared but never entered.
    for name in capability.unbound_inputs:
        gaps.append(
            Gap(
                # Genuinely unknowable: the trace never touched this control, so nothing says which
                # screen it is on. Guessing a position would be worse than admitting the author has
                # to place it.
                step_after=None,
                detected_from=GapEvidence(
                    evidence=f"no step enters {name}; the run never set it, so a default was accepted"
                ),
                reason=(
                    f"{name} is a declared input that no step enters. Replay with a different value "
                    f"would silently use whatever the form defaults to, and fail only at the "
                    f"checkpoint. The authored step's position must be chosen by hand."
                ),
            )
        )

    return gaps


def _step_preceding(
    evidence_trace_step: int | None, steps: list[Step], derived_from: dict[str, int]
) -> str | None:
    """Where the missing step belongs: immediately BEFORE the step that was rejected.

    The human intervened because an action failed, so the thing they did has to happen earlier than
    that action, not after it. Anchoring to the rejected step itself would tell an author to insert
    the fix after the click it was supposed to enable.
    """
    if evidence_trace_step is None:
        return None
    rejected = next(
        (n for n, st in enumerate(steps) if derived_from.get(st.id) == evidence_trace_step), None
    )
    if rejected is None or rejected == 0:
        return None
    return steps[rejected - 1].id


# --------------------------------------------------------------------------- #
# outcome rules
# --------------------------------------------------------------------------- #


def _outcome_rules(
    trace: RunTrace, humans: list[RecordedStep], dedup_message: str | None, spec: CapabilitySpec
) -> list[OutcomeRule]:
    rules: list[OutcomeRule] = []

    # The app's own refusal, seen when the first Continue was rejected.
    if dedup_message:
        rules.append(
            OutcomeRule(
                when=TextCondition(contains=dedup_message),
                # A business outcome, not a failure: the application legitimately refused, which is what
                # `BusinessOutcomeCode` documents. It was `status: failure` until the engine tried to
                # execute the rule and found `VALIDATION_REJECTED` is not a `FailureCode` at all.
                return_=OutcomeReturn(status="business_outcome", code="VALIDATION_REJECTED"),
            )
        )

    # A human dismissed something and the screen visibly lost it: that is describable, so describe it.
    for human in humans:
        after = human.observation_after
        removed = (after.removed_text[0] if after and after.removed_text else None)
        if not removed:
            continue
        rules.append(
            OutcomeRule(
                when=DialogCondition(contains=_dialog_key(removed)),
                recover=Recover(strategy="dismiss", max_attempts=1),
                else_=OutcomeReturn(status="escalated", reason="UNKNOWN_DIALOG"),
            )
        )

    rules.extend(spec.extra_outcome_rules)

    # Always last, always present. A dialog the artifact cannot name is the one case where guessing is
    # worse than stopping.
    rules.append(
        OutcomeRule(
            when=DialogCondition(unmatched=True),
            return_=OutcomeReturn(status="escalated", reason="UNKNOWN_DIALOG"),
        )
    )
    return rules


def _dialog_key(text: str) -> str:
    """A short, matchable phrase from a dialog's text — its title, not its body.

    The body carries a per-request reference code (`ERR-APP_BD`), which would make the rule match
    exactly one occurrence and never fire again.
    """
    cleaned = re.sub(r"^[^A-Za-z]+", "", text).strip()
    words = cleaned.split()
    return " ".join(words[:2]) if words else cleaned[:40]


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def assert_no_leak(capability: Capability, declared: set[str]) -> None:
    """Refuse to emit an artifact that carries what canonicalization exists to remove."""
    blob = capability.to_yaml()
    problems: list[str] = []

    for step in capability.steps:
        target = getattr(step.action, "target", None)
        for candidate in (target.candidates if target else []):
            if candidate.kind == "coordinate":  # pragma: no cover - unexpressible by construction
                problems.append(f"{step.id}: a coordinate is a primary locator")

    found_ids = set(GENERATED_ID.findall(blob))
    # A generated id may appear as evidence of a REJECTION; that is the point. It must not appear in
    # any candidate selector.
    for step in capability.steps:
        target = getattr(step.action, "target", None)
        for candidate in (target.candidates if target else []):
            selector = getattr(candidate, "selector", "") or ""
            if GENERATED_ID.search(selector):
                problems.append(f"{step.id}: candidate selector uses a generated id: {selector}")

    if SECRET_SHAPED.search(blob):
        problems.append("a credential-shaped string reached the artifact")

    for name in declared:
        # The redactor replaced declared values with ${inputs.name} at discovery time. If a literal
        # survived, redaction has a hole and this artifact must not be written.
        if f"${{inputs.{name}}}" not in blob and name in blob:
            problems.append(f"declared input {name!r} may appear unredacted")

    if problems:
        raise CanonicalizationError("; ".join(problems))
    _ = found_ids  # rejections may legitimately name them


# --------------------------------------------------------------------------- #
# the whole thing
# --------------------------------------------------------------------------- #


def _verification_scope(spec: CapabilitySpec) -> str | None:
    """Where the outcome is displayed — taken from where the outputs are extracted.

    Not a separate authored field. `OutputSpec.extract.scope` already names the region the outcome values
    are read out of, so "verify the values where you extract them" is the same claim said once rather
    than twice, and a second copy of the selector is how the two drift apart.

    It matters because without it the checkpoint asserts against the whole frame, and on this app that
    cannot fail: the form the run just submitted is still on screen, and its `<select>` renders every
    account type as visible text. The checkpoint passed for `savings`, for `checking`, and for
    `money_market` — which is not even a legal value — while the review panel said `Savings`.

    Only when the spec declares exactly one output with a selector-bearing scope. Several would be a
    guess about which one verifies the outcome, and `None` leaves the checkpoint unscoped, which is
    simply the old behaviour rather than a new failure.
    """
    scopes = [
        selector
        for output in spec.outputs.values()
        if (selector := getattr(output.extract.scope, "selector", None))
    ]
    return scopes[0] if len(scopes) == 1 else None


def canonicalize(trace: RunTrace, spec: CapabilitySpec) -> Capability:
    """One trace, one capability. The procedure is derived; the contract comes from `spec`."""
    steps, derived_from, humans, dedup_message = _reduce(trace)
    if not steps:
        raise CanonicalizationError("no executable steps survived; nothing to replay")

    # The final step reads and verifies rather than acting: the run's whole point was reaching a
    # screen, and the checkpoint is what makes "we got there" checkable rather than claimed.
    final = trace.steps[-1].observation_after
    checkpoint_conditions: list[Condition] = []
    if final and final.headings:
        # Unscoped on purpose: the outcome heading sits *outside* the region the values are shown in,
        # and `observation.headings` is already a narrow enough haystack to assert against.
        checkpoint_conditions.append(HeadingCondition(contains=final.headings[-1]))
    scope = _verification_scope(spec)
    for name in sorted(spec.inputs):
        if name in _referenced_input_names(trace):
            checkpoint_conditions.append(TextCondition(contains=f"${{inputs.{name}}}", within=scope))
    if checkpoint_conditions:
        steps.append(
            Step(
                id="verify-outcome",
                action=ReadAction(),
                checkpoint=Checkpoint(all=checkpoint_conditions),
            )
        )

    first_observation = trace.steps[0].observation_before
    capability = Capability(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        capability=CapabilityMeta(
            id=spec.id, version=spec.version, title=spec.title, risk=spec.risk
        ),
        contract=Contract(inputs=dict(spec.inputs), outputs=dict(spec.outputs)),
        policy=Policy(
            allowed_origins=_origins(trace),
            forbidden_actions=["submit_irreversible", "navigate_external", "download", "upload"],
        ),
        entry=Entry(
            url=trace.target,
            frame_path=next(
                (f.path for f in (first_observation.frames if first_observation else []) if f.path),
                [],
            ),
            expect_url_contains=_path_of(_inner_url(first_observation)),
        ),
        steps=steps,
        outcome_rules=_outcome_rules(trace, humans, dedup_message, spec),
        provenance=Provenance(
            source_run_id=trace.run_id,
            discovered_at=trace.started_at,
            model=f"{trace.provider.name}/{trace.provider.model}",
            fault_profile=trace.fault_profile,
        ),
    )

    capability.gaps = detect_gaps(trace, capability, derived_from, humans)
    assert_no_leak(capability, set(spec.inputs))
    return capability
