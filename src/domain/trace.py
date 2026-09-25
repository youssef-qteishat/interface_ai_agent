"""
The run trace — the one contract that outlives this layer.

The driver works in pixels; the artifact must be semantic. The bridge is built at act
time, because the DOM under a coordinate is only knowable while the page is in that
state. Everything the canonicalizer will ever know about *what was clicked* is written
here, by the recorder, during the run.

These models mirror what `sandbox/surface_agent.py` actually returns — not an idealised
sketch. Where the two ever disagree, the agent is right and this file is wrong, because
the agent is what touches the page.

Two shapes carry more weight than they look like they do:

  * `match_count: int | None` — null means "not counted", never zero. A candidate that
    matched nothing and a candidate nobody counted are different facts, and only one of
    them means "do not use this locator".
  * `RecordedStep.probe` may be null, but then `probe_unavailable` must say why. A
    surface with no accessibility backend (the Tkinter mock in Step 16) still produces
    a valid trace; it just cannot be canonicalized into a web capability. That is the
    cross-surface argument, expressed as a type.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.domain.results import RunResult

SCHEMA_VERSION = "1.0.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# locator candidates
# --------------------------------------------------------------------------- #
#
# A strict union: each kind declares the fields it actually needs. Adding a seventh
# kind to probe.js requires a matching model here, which is the intended friction —
# the canonicalizer matches these exhaustively.

MatchCount = Annotated[
    int | None,
    Field(
        default=None,
        ge=0,
        description="How many elements this locator matches. null = not counted (NOT zero).",
    ),
]


class _Candidate(_Model):
    match_count: MatchCount = None


class RoleCandidate(_Candidate):
    """Accessibility role + name. The strongest locator when a real name exists —
    which, on a legacy app with unlabelled inputs, is often not the case."""

    kind: Literal["role"] = "role"
    role: str
    name: str
    # Where the name came from. "placeholder" is the trap: Chromium will compute an
    # accessible name from placeholder text ("$0.00"), which looks authoritative and
    # identifies nothing.
    name_source: Literal["computed", "visible_text", "label", "placeholder"] | None = None


class TextCandidate(_Candidate):
    kind: Literal["text"] = "text"
    tag: str
    text: str


class ContextualTextCandidate(_Candidate):
    """Anchored on nearby text — the table-row label in this app's markup. The
    workhorse where labels carry no `for=` attribute."""

    kind: Literal["contextual_text"] = "contextual_text"
    anchor: str
    relative: str


class AttributeCandidate(_Candidate):
    """A stable attribute selector, `input[name="member_id"]`. Ranked above structural
    CSS because form field names survive restarts while generated ids do not."""

    kind: Literal["attribute"] = "attribute"
    selector: str
    attribute: str
    value: str


class LabelCandidate(_Candidate):
    kind: Literal["label"] = "label"
    text: str
    control: str


class CssCandidate(_Candidate):
    """Structural path. Lowest confidence — it encodes markup order, which is exactly
    what a redesign changes. Always marked unverified until replay proves it."""

    kind: Literal["css"] = "css"
    selector: str
    stability: Literal["unverified", "verified", "generated"] = "unverified"


LocatorCandidate = Annotated[
    RoleCandidate
    | TextCandidate
    | ContextualTextCandidate
    | AttributeCandidate
    | LabelCandidate
    | CssCandidate,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


class ElementRect(_Model):
    x: int
    y: int
    width: int
    height: int


class ProbeResult(_Model):
    """What was under the coordinate, and how to find it again.

    Mirrors `POST /probe`. Validating a live payload against this model is the real
    test of this module.
    """

    coordinate: tuple[int, int]
    frame_path: list[str] = Field(
        default_factory=list,
        description="Frame names from the main document down, e.g. ['servicing-frame'].",
    )

    role: str | None = None
    role_source: Literal["ax_tree", "retargeted_element"] | None = None
    accessible_name: str | None = None
    accessible_name_source: (
        Literal["computed", "visible_text", "label", "placeholder"] | None
    ) = None

    tag: str
    # The coordinate may land on a decorative child (the <img> inside an icon button);
    # `tag` describes the control, these two record what was literally hit.
    hit_tag: str | None = None
    retargeted_from: str | None = None
    role_hint: str | None = None
    name_hint: str | None = None

    type: str | None = None
    name_attr: str | None = None
    dom_id: str | None = None
    # "generated" ids (inp_0a8b8b80) change every request. Recorded to show WHY no id
    # locator was offered, rather than leaving its absence unexplained.
    dom_id_stability: Literal["generated", "stable"] | None = None

    visible_text: str | None = None
    nearby_label: str | None = None
    placeholder: str | None = None
    title: str | None = None
    value: str | None = None
    disabled: bool = False
    visible: bool = True

    enclosing_region: str | None = None
    enclosing_region_label: str | None = None
    classes: list[str] = Field(default_factory=list)
    rect: ElementRect | None = None

    candidates: list[LocatorCandidate] = Field(default_factory=list)
    element_fingerprint: str | None = None

    @property
    def best_candidate(self) -> Any | None:
        """First candidate that matches exactly one element. Convenience only — the
        canonicalizer makes the real decision, with the full list in front of it."""
        return next((c for c in self.candidates if c.match_count == 1), None)

    @property
    def is_ambiguous(self) -> bool:
        return all((c.match_count or 0) != 1 for c in self.candidates) if self.candidates else True


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #


class FrameInfo(_Model):
    path: list[str] = Field(default_factory=list)
    url: str
    headings: list[str] = Field(default_factory=list)
    controls: list[dict[str, Any]] = Field(default_factory=list)


class DialogInfo(_Model):
    selector: str | None = None
    text: str | None = None


class Observation(_Model):
    """One look at the screen: where we are, what is showing, and two hashes.

    The hashes are what make progress decidable. `observation_hash` is the screenshot
    digest (pixels changed); `dom_hash` is the visible-text digest (content changed).
    A page can repaint without changing, and can change without repainting much, so
    Step 11's no-progress rule wants both.
    """

    main_frame_url: str | None = None
    title: str | None = None
    frames: list[FrameInfo] = Field(default_factory=list)
    dialogs: list[DialogInfo] = Field(default_factory=list)
    overlays: list[str] = Field(default_factory=list)
    banners: list[str] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)

    # The flattened visible text of every frame, capped by the agent. This is what
    # Step 11's checkpoint reads ("does 'Review New Account' appear, with the right
    # account type and amount?") and what `new_text` below is diffed out of, so it is
    # part of the contract rather than debug output.
    visible_text: str | None = None

    screenshot: str | None = Field(None, description="Path, relative to the evidence dir.")
    observation_hash: str | None = Field(None, description="sha256 of the screenshot bytes.")
    dom_hash: str | None = Field(None, description="sha256 of the visible text.")
    observed_at_ms: int | None = None

    # Only meaningful on an "after" observation.
    dom_changed: bool | None = None
    new_text: list[str] = Field(default_factory=list)
    # The other half of the diff. Added in Step 12 after a live handoff recorded
    # `dom_changed: true` with an empty `new_text`: the human had DISMISSED a dialog, so
    # the whole event was a disappearance and an additions-only diff described it as
    # nothing at all.
    removed_text: list[str] = Field(default_factory=list)

    @property
    def frame_urls(self) -> list[str]:
        """Every URL currently rendered — what the policy engine's origin check reads."""
        return [f.url for f in self.frames]


# --------------------------------------------------------------------------- #
# step + run
# --------------------------------------------------------------------------- #


class PolicyDecision(_Model):
    """Recorded for every action the automation took, allowed or denied. An automation
    step without one is a bug the recorder refuses to write (Step 12).

    A `human` step carries `policy: null` instead. That is not an omission: nobody ran
    the allowlist against what an operator did with their own hands, and synthesising an
    `allow` here would make the recorder's assertion satisfiable by a lie.
    """

    decision: Literal["allow", "deny", "escalate"]
    risk: Literal["read_only", "reversible", "consequential", "irreversible"]
    rule: str
    code: str | None = None
    detail: str | None = None


class Timing(_Model):
    dispatched_ms: int | None = None
    settled_ms: int | None = None


class RecordedStep(_Model):
    index: int = Field(..., ge=0)
    actor: Literal["automation", "human"] = "automation"
    action: dict[str, Any] = Field(
        ...,
        description=(
            "The action as executed, dumped from the src.domain.actions union — or a "
            "HumanIntervention, which is deliberately outside that union."
        ),
    )
    policy: PolicyDecision | None = None

    observation_before: Observation | None = None
    probe: ProbeResult | None = None
    probe_unavailable: str | None = Field(
        None,
        description="Why no probe exists, e.g. 'no_accessibility_backend' on a native surface.",
    )
    observation_after: Observation | None = None

    timing: Timing = Field(default_factory=Timing)
    # A short operational reason. Deliberately not the model's hidden reasoning: the
    # artifact must not depend on provider-specific transcripts.
    model_reason: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _probe_xor_reason(self) -> RecordedStep:
        if self.probe is not None and self.probe_unavailable is not None:
            raise ValueError("a step has either a probe or a probe_unavailable reason, not both")
        return self

    @model_validator(mode="after")
    def _policy_required_for_automation(self) -> RecordedStep:
        """Required where it means something, absent where it would be fiction."""
        if self.actor == "automation" and self.policy is None:
            raise ValueError("an automation step must carry a policy decision")
        if self.actor == "human" and self.policy is not None:
            raise ValueError(
                "a human step must not carry a policy decision — nothing evaluated it"
            )
        return self

    def requires_probe(self) -> bool:
        """Clicks and scrolls carry a coordinate, so they must be explained."""
        return self.action.get("kind") in {"left_click", "double_click", "scroll"}


class Display(_Model):
    width: int
    height: int
    # Identity at 1280x800, but recorded because the trace must say what coordinate
    # space its numbers are in.
    scale_sent_to_model: float = 1.0


class ProviderInfo(_Model):
    name: str
    model: str
    tool: str | None = None


class Budget(_Model):
    max_steps: int | None = None
    wall_clock_s: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    usd_estimate: float | None = None


class RunTrace(_Model):
    """The whole run. Written incrementally, so a crashed run still leaves something
    readable (Step 12)."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    goal: str
    surface_kind: Literal["web", "desktop"] = "web"
    target: str
    display: Display
    provider: ProviderInfo
    fault_profile: str | None = None

    started_at: str | None = None
    ended_at: str | None = None

    steps: list[RecordedStep] = Field(default_factory=list)
    outcome: RunResult | None = None
    budget: Budget = Field(default_factory=Budget)

    # ---- YAML round-trip ----

    def to_yaml(self) -> str:
        """Dump in declaration order — a trace is meant to be read by a human
        reviewer, and alphabetised keys would scatter the story of each step."""
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=False),
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )

    @classmethod
    def from_yaml(cls, text: str) -> RunTrace:
        return cls.model_validate(yaml.safe_load(text))

    def steps_missing_probe(self) -> list[int]:
        """Indices of coordinate actions with neither a probe nor a stated reason —
        the assertion Step 12 runs before writing."""
        return [
            s.index
            for s in self.steps
            if s.requires_probe() and s.probe is None and s.probe_unavailable is None
        ]

    def steps_missing_policy(self) -> list[int]:
        """Indices of automation steps with no policy decision.

        The model validator already refuses to construct one, so this is the second
        line: it catches a step mutated after construction, which is the only way such a
        step can reach the writer.
        """
        return [s.index for s in self.steps if s.actor == "automation" and s.policy is None]

    def human_steps(self) -> list[int]:
        """Indices of steps a person is responsible for. `run-summary.json` reports the
        count, because "was a human involved?" is the first thing a reviewer asks."""
        return [s.index for s in self.steps if s.actor == "human"]
