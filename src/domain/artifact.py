"""
The capability artifact — the contract between discovery and replay.

A trace says what happened once. This says what should happen every time, and it is the only thing
replay is allowed to read. That asymmetry is the whole point: if the artifact is not sufficient on its
own, replay is quietly depending on the run that produced it.

Three rules shape the models:

  * **Coordinates are never a locator.** They survive only under `Target.evidence`, to explain where a
    candidate came from. Replay resolves roles, labels and text.
  * **A draft artifact stays loadable.** An unbound input or an unresolved gap does not fail
    validation — you must be able to open a draft to author it. `is_replayable` is what refuses.
  * **Text matching is always case-insensitive.** The run declares `savings` and `25.00`; the page
    renders `Savings` and `$25.00`. Making that a per-condition flag invites forgetting it exactly
    where it matters, so there is no flag.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# The six-variant locator union already exists and is what the probe emits. A second one here would
# be the real duplication — the canonicalizer copies these straight across from the trace.
from src.domain.trace import LocatorCandidate

# The artifact FORMAT. Deliberately separate from `trace.SCHEMA_VERSION`: the two version different
# things and move for different reasons (see canonicalizer-plan.md §4).
ARTIFACT_SCHEMA_VERSION = "1.1.0"

INPUT_REF = re.compile(r"\$\{inputs\.([A-Za-z_][A-Za-z0-9_]*)\}")


class UnknownInput(KeyError):
    """A `${inputs.x}` whose value the caller did not supply."""


def substitute(text: str, inputs: Mapping[str, Any]) -> str:
    """Replace every `${inputs.x}` with the value supplied for `x`.

    An unknown name **raises**. `Capability._references_resolve` already guarantees at load time that
    every reference in an artifact names a *declared* input, so a name missing here means the caller
    passed incomplete inputs — and the tempting fallback of an empty string would turn that into a
    condition asserting on a truncated string, which then quietly passes. A missing input is a caller
    bug, not a match.
    """

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise UnknownInput(
                f"no value supplied for ${{inputs.{name}}} "
                f"(supplied: {', '.join(sorted(inputs)) or 'none'})"
            )
        return str(inputs[name])

    return INPUT_REF.sub(one, text)


class _Model(BaseModel):
    # `populate_by_name` so `return:`/`else:` can be written in YAML under their real names while the
    # Python attributes stay legal identifiers.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class IncompatibleSchemaVersion(Exception):
    """The artifact's format is one this interpreter does not speak."""

    def __init__(self, found: str, supported: str) -> None:
        super().__init__(
            f"artifact schema_version {found!r} is not compatible with this interpreter "
            f"(supports {supported!r}) — the major version must match"
        )
        self.found = found
        self.supported = supported


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #


class _TextPredicate(_Model):
    """Substring matching, always case-insensitive. Exactly one of the two fields."""

    contains: str | None = None
    any_of: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one(self) -> _TextPredicate:
        if (self.contains is None) == (not self.any_of):
            kind = getattr(self, "kind", "condition")
            raise ValueError(f"{kind}: give exactly one of `contains` or `any_of`")
        return self

    def matches(self, haystack: str | None) -> bool:
        needles = [self.contains] if self.contains is not None else self.any_of
        text = (haystack or "").lower()
        return any(n.lower() in text for n in needles)


class UrlCondition(_TextPredicate):
    kind: Literal["url"] = "url"


class HeadingCondition(_TextPredicate):
    kind: Literal["heading"] = "heading"


class _ScopedTextPredicate(_TextPredicate):
    """A text assertion that may name the region of the screen it applies to.

    Its own class rather than a field on `_TextPredicate`, because `UrlCondition` and `HeadingCondition`
    inherit from that too and a scoped *url* is nonsense — `extra="forbid"` then rejects one, which is
    the point of putting the field here.

    Unscoped, a text assertion reads the whole frame, and on this app that made the `verify-outcome`
    checkpoint **vacuous**: after Continue the sub-account form is still on screen, its `<select>`
    renders every `<option>`, and the funding dropdown lists `(Money_market)` — so
    `text: ${inputs.account_type}` matched whatever value was claimed, including values the review panel
    contradicted and one that is not even a legal input. A checkpoint that cannot fail is not a check.

    A bare CSS selector rather than a `LocatorCandidate`: a verification scope wants no ladder and no
    fallback. If the region named is not on screen, the assertion does not hold — that is the answer,
    not a reason to go looking somewhere else.
    """

    within: str | None = None


class TextCondition(_ScopedTextPredicate):
    kind: Literal["text"] = "text"


class TextAbsentCondition(_ScopedTextPredicate):
    """The inverse. Its own kind rather than a `negate` flag, because a reviewer reading the artifact
    should see the assertion, not have to resolve a boolean."""

    kind: Literal["text_absent"] = "text_absent"


class ValueCondition(_Model):
    kind: Literal["value"] = "value"
    equals: str


class CheckedCondition(_Model):
    kind: Literal["checked"] = "checked"
    equals: bool = True


class DialogCondition(_Model):
    kind: Literal["dialog"] = "dialog"
    contains: str | None = None
    # "any dialog this artifact does not name" — the escalation catch-all.
    unmatched: bool = False

    @model_validator(mode="after")
    def _one_or_the_other(self) -> DialogCondition:
        if (self.contains is None) == (not self.unmatched):
            raise ValueError("dialog: give either `contains` or `unmatched: true`, not both")
        return self


class OverlayCondition(_Model):
    kind: Literal["overlay"] = "overlay"
    present: bool = True


Condition = Annotated[
    UrlCondition
    | HeadingCondition
    | TextCondition
    | TextAbsentCondition
    | ValueCondition
    | CheckedCondition
    | DialogCondition
    | OverlayCondition,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #


class Rejection(_Model):
    """A candidate the ranker refused, and why.

    Kept in the artifact on purpose: an artifact that says why a locator was *not* used is far easier
    to review than one that silently omits it.
    """

    kind: str
    value: str | None = None
    reason: str


class TargetEvidence(_Model):
    # Provenance only. Replay must never resolve from this.
    discovery_coordinate: tuple[int, int] | None = None
    expected_tag: str | None = None
    expected_role: str | None = None
    rejected: list[Rejection] = Field(default_factory=list)


class Target(_Model):
    frame_path: list[str] = Field(default_factory=list)
    candidates: list[LocatorCandidate] = Field(default_factory=list, min_length=1)
    evidence: TargetEvidence | None = None


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
#
# No `wait` member. A wait *step* is the arbitrary sleep replay is supposed to avoid; waiting belongs
# to condition evaluation and to `recover: wait_and_retry`.


class ClickAction(_Model):
    kind: Literal["click"] = "click"
    target: Target


class FillAction(_Model):
    kind: Literal["fill"] = "fill"
    value: str
    target: Target


class SelectAction(_Model):
    kind: Literal["select"] = "select"
    value: str
    target: Target


class CheckAction(_Model):
    kind: Literal["check"] = "check"
    target: Target


class ReadAction(_Model):
    kind: Literal["read"] = "read"
    target: Target | None = None


StepAction = Annotated[
    ClickAction | FillAction | SelectAction | CheckAction | ReadAction,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #


class Retry(_Model):
    max_attempts: int = Field(1, ge=0)
    backoff_ms: int = Field(250, ge=0)


class Checkpoint(_Model):
    all: list[Condition] = Field(default_factory=list, min_length=1)


class Step(_Model):
    id: str
    action: StepAction
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None
    timeout_ms: int = 5000
    retry: Retry | None = None
    # Set when a human wrote this step because the trace could not supply it. Its matching `Gap`
    # carries the reason.
    authored_by: Literal["human"] | None = None


# --------------------------------------------------------------------------- #
# outcomes
# --------------------------------------------------------------------------- #


class OutcomeReturn(_Model):
    """What a matched outcome rule returns.

    `code` is validated against the enum its `status` implies, because the two are not independent: a
    `business_outcome` carries a `BusinessOutcomeCode`, a `failure` carries a `FailureCode`, and an
    `escalated` carries an `EscalationReason`. Without this the mismatch is invisible in the artifact and
    only surfaces when the rule *fires* — at which point building the `RunResult` raises and a run that had
    a perfectly good answer dies of a typo instead. The committed artifact shipped exactly that pairing
    (`status: failure` with `code: VALIDATION_REJECTED`, which is a business-outcome code) and nothing
    caught it until the engine tried to execute it.
    """

    status: Literal["success", "business_outcome", "failure", "escalated"]
    code: str | None = None
    reason: str | None = None
    field: str | None = None

    @model_validator(mode="after")
    def _code_matches_status(self) -> OutcomeReturn:
        from src.domain.results import BusinessOutcomeCode, EscalationReason, FailureCode

        vocabularies: dict[str, Any] = {
            "business_outcome": BusinessOutcomeCode,
            "failure": FailureCode,
            "escalated": EscalationReason,
        }
        enum = vocabularies.get(self.status)
        # `escalated` names its value in `reason`; the others use `code`.
        value = self.reason if self.status == "escalated" else self.code
        if enum is None or value is None:
            return self
        if value not in enum.__members__:
            field = "reason" if self.status == "escalated" else "code"
            raise ValueError(
                f"status {self.status!r} with {field} {value!r}: not a member of "
                f"{enum.__name__} ({', '.join(sorted(enum.__members__))})"
            )
        return self


class Recover(_Model):
    strategy: Literal["wait_and_retry", "dismiss"]
    target: Target | None = None
    max_attempts: int = Field(1, ge=1)
    backoff_ms: int = Field(500, ge=0)


class OutcomeRule(_Model):
    when: Condition
    # `return` and `else` are keywords; the YAML keeps the readable spelling.
    return_: OutcomeReturn | None = Field(None, alias="return")
    recover: Recover | None = None
    else_: OutcomeReturn | None = Field(None, alias="else")

    @model_validator(mode="after")
    def _must_do_something(self) -> OutcomeRule:
        if self.return_ is None and self.recover is None:
            raise ValueError("outcome rule must specify `return` or `recover`")
        if self.else_ is not None and self.recover is None:
            raise ValueError("`else` only means something beside `recover`")
        return self


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #


class InputSpec(_Model):
    type: Literal["string", "enum", "decimal", "integer", "boolean"]
    pattern: str | None = None
    values: list[str] | None = None
    minimum: float | None = None
    sensitive: bool = False


class ExtractField(_Model):
    row_label: str
    transform: str | None = None


class Extract(_Model):
    # A locator, reusing the same union as everything else.
    scope: LocatorCandidate
    fields: dict[str, ExtractField]


class OutputSpec(_Model):
    type: str
    properties: dict[str, Any] | None = None
    # Required: a declared output with no way to extract it is a promise the artifact cannot keep.
    extract: Extract


class Contract(_Model):
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)


class Policy(_Model):
    allowed_origins: list[str] = Field(default_factory=list)
    # No `allowed_actions`: the StepAction union makes an unlisted action unexpressible, so such a
    # check could never fire. This one can.
    forbidden_actions: list[str] = Field(default_factory=list)


class Entry(_Model):
    url: str
    frame_path: list[str] = Field(default_factory=list)
    expect_url_contains: str | None = None


class CapabilityMeta(_Model):
    id: str
    version: str
    title: str
    risk: Literal["read_only", "reversible", "consequential", "irreversible"] = "reversible"


# --------------------------------------------------------------------------- #
# gaps
# --------------------------------------------------------------------------- #


class GapEvidence(_Model):
    trace_step: int | None = None
    human_step: int | None = None
    evidence: str | None = None


class Gap(_Model):
    """A step the trace could not supply.

    Two ways this happens, both seen in the real run: a human acted and nothing observable changed, or
    an input was declared and the run never set it because the default happened to match.
    """

    step_after: str | None = None
    detected_from: GapEvidence = Field(default_factory=GapEvidence)
    reason: str
    resolved_by: Literal["human"] | None = None


class Provenance(_Model):
    source_run_id: str | None = None
    source_evidence: str | None = None
    discovered_at: str | None = None
    model: str | None = None
    fault_profile: str | None = None


# --------------------------------------------------------------------------- #
# the artifact
# --------------------------------------------------------------------------- #


class Capability(_Model):
    schema_version: str = ARTIFACT_SCHEMA_VERSION
    capability: CapabilityMeta
    contract: Contract = Field(default_factory=Contract)
    policy: Policy = Field(default_factory=Policy)
    entry: Entry
    steps: list[Step] = Field(default_factory=list)
    outcome_rules: list[OutcomeRule] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    # ---- validation ----

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Capability:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id: {step.id!r}")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _references_resolve(self) -> Capability:
        """Every `${inputs.x}` anywhere must name a declared input.

        A typo is not a gap — it is a reference to something that does not exist, and no amount of
        authoring fixes it. Rejected outright, unlike an *unbound* input.
        """
        declared = set(self.contract.inputs)
        unknown = sorted(self.referenced_inputs - declared)
        if unknown:
            raise ValueError(
                f"reference to undeclared input(s): {', '.join(unknown)} "
                f"(declared: {', '.join(sorted(declared)) or 'none'})"
            )
        return self

    # ---- what makes it replayable ----

    @property
    def referenced_inputs(self) -> set[str]:
        """Every input mentioned anywhere — steps, conditions, checkpoints."""
        blob = json.dumps(self.model_dump(mode="json", by_alias=True), default=str)
        return set(INPUT_REF.findall(blob))

    @property
    def named_dialogs(self) -> tuple[str, ...]:
        """The dialog phrases this artifact names, which is what `unmatched` is relative to.

        `DialogCondition(unmatched=True)` means "a dialog this artifact does not name", and that is not
        a question the condition can answer by itself — it is a fact about the whole artifact. Derived
        rather than stored so it cannot drift from the rules it summarises.
        """
        return tuple(
            rule.when.contains
            for rule in self.outcome_rules
            if rule.when.kind == "dialog" and rule.when.contains
        )

    @property
    def unbound_inputs(self) -> list[str]:
        """Declared inputs that no step actually *enters*.

        Deliberately narrower than `referenced_inputs`: an input a checkpoint merely asserts on is not
        bound by that. The real run declared `account_type` and never touched the dropdown, because
        `savings` is the first option — so the checkpoint passed by coincidence and replay with
        `checking` would open the wrong account. This is what catches that, before a browser opens.
        """
        entered: set[str] = set()
        for step in self.steps:
            value = getattr(step.action, "value", None)
            if value:
                entered |= set(INPUT_REF.findall(value))
        return [name for name in self.contract.inputs if name not in entered]

    @property
    def open_gaps(self) -> list[Gap]:
        return [g for g in self.gaps if g.resolved_by is None]

    @property
    def is_replayable(self) -> bool:
        return not self.open_gaps and not self.unbound_inputs

    def why_not_replayable(self) -> list[str]:
        """One line per reason, for the message `replay` prints when it refuses.

        Each gap carries its evidence, because that is the part that localises it — "a human acted"
        tells you nothing, while "You must accept the account disclosure to continue" names the
        control you have to author.
        """
        reasons = []
        for gap in self.open_gaps:
            # A gap with no anchor is not a bug: when nothing in the run touched the control, there
            # is no step to sit after, and saying "after step None" would be worse than saying so.
            where = (
                f"after step {gap.step_after!r}"
                if gap.step_after
                else "at a position the author must choose"
            )
            line = f"unresolved gap {where}: {gap.reason.strip()}"
            if gap.detected_from.evidence:
                line += f"\n    evidence: {gap.detected_from.evidence!r}"
                if gap.detected_from.trace_step is not None:
                    line += f" (trace step {gap.detected_from.trace_step})"
            reasons.append(line)
        if self.unbound_inputs:
            reasons.append(
                f"declared input(s) no step sets: {', '.join(self.unbound_inputs)}"
            )
        return reasons

    # ---- YAML ----

    def to_yaml(self) -> str:
        """Declaration order, aliases, and no nulls — this file is meant to be read by a human."""
        return yaml.safe_dump(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )

    @classmethod
    def from_yaml(cls, text: str) -> Capability:
        return cls.model_validate(yaml.safe_load(text))


def load_artifact(path: str | Path) -> Capability:
    """Read an artifact, refusing a format this interpreter does not speak.

    The version is checked *before* validation on purpose: a future-major artifact should say so,
    rather than producing a pile of `extra fields not permitted` noise about fields that will make
    perfect sense to the interpreter that understands them.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    found = str(data.get("schema_version", ""))
    if found.split(".")[0] != ARTIFACT_SCHEMA_VERSION.split(".")[0]:
        raise IncompatibleSchemaVersion(found, ARTIFACT_SCHEMA_VERSION)
    return Capability.model_validate(data)
