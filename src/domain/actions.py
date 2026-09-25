"""
The action vocabulary — what the model is allowed to ask for.

This union is the FIRST safety gate. Raw `tool_use.input` from the API is parsed
into it before anything touches the surface, so an action that does not validate
here is never executed, never reaches the policy engine, and never reaches xdotool.

Two rules shape the design:

1. **Only the nine enabled members exist.** The computer toolset ships seventeen;
   `configs` disables eight of them (drag, middle/right/triple click, mouse up/down,
   mouse_move, hold_key) because this workflow does not need them. There is
   deliberately no model for those: if one arrives it fails validation and counts
   toward the invalid-action limit. Step 6's allowlist re-checks the parsed kind, so
   there are two independent gates rather than one.

2. **Field names mirror Anthropic's member schemas, not our agent's wire format.**
   This union parses what the API sends. Translating to the surface agent's request
   shape is the adapter's job (Step 5). That is why modifier keys are called `text`
   on clicks and scrolls, even though `Type.text` means something entirely different.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# The Step 0 vocabulary, in one place. Step 10 builds the toolset `configs` from this,
# so the enabled set and the parseable set cannot drift apart.
ENABLED_MEMBERS: tuple[str, ...] = (
    "screenshot",
    "zoom",
    "left_click",
    "double_click",
    "type",
    "key",
    "scroll",
    "wait",
    "cursor_position",
)

DISABLED_MEMBERS: tuple[str, ...] = (
    "right_click",
    "middle_click",
    "triple_click",
    "left_click_drag",
    "mouse_move",
    "left_mouse_down",
    "left_mouse_up",
    "hold_key",
)

# The API's ceiling. Our surface agent caps waits far lower; the Step 5 adapter clamps
# and records that it clamped, rather than rejecting a legitimate request here and
# spending the invalid-action budget on it.
MAX_WAIT_SECONDS = 300.0
MAX_HOLD_SECONDS = 300.0

# Lower bound only. The upper bound is display-dependent, so it is enforced where the
# display size is known: the surface agent answers COORDINATE_OUT_OF_BOUNDS. Negative
# pixels are nonsense on any display, so they are rejected here, before execution.
Pixel = Annotated[int, Field(ge=0)]
Coordinate = Annotated[tuple[Pixel, Pixel], Field(description="[x, y] in screenshot pixel space")]


class _Action(BaseModel):
    """Strict by default: an unexpected key is a malformed action, not a hint."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# computer toolset members (the nine enabled ones)
# --------------------------------------------------------------------------- #


class Screenshot(_Action):
    kind: Literal["screenshot"] = "screenshot"


class Zoom(_Action):
    """Crop of the current screen. Coordinates stay in full-screenshot space —
    a zoom never introduces a second coordinate system (plan §4)."""

    kind: Literal["zoom"] = "zoom"
    region: Annotated[
        tuple[Pixel, Pixel, Pixel, Pixel], Field(description="[x0, y0, x1, y1]")
    ]


class LeftClick(_Action):
    kind: Literal["left_click"] = "left_click"
    coordinate: Coordinate
    # Anthropic sends held modifier keys in `text` (e.g. "ctrl", "ctrl+shift").
    text: str | None = None


class DoubleClick(_Action):
    kind: Literal["double_click"] = "double_click"
    coordinate: Coordinate
    text: str | None = None


class Type(_Action):
    kind: Literal["type"] = "type"
    text: str


class Key(_Action):
    kind: Literal["key"] = "key"
    text: str
    repeat: int = Field(1, ge=1, le=100)


class Scroll(_Action):
    kind: Literal["scroll"] = "scroll"
    scroll_direction: Literal["up", "down", "left", "right"]
    scroll_amount: int = Field(..., ge=1)
    coordinate: Coordinate | None = None
    text: str | None = None


class Wait(_Action):
    kind: Literal["wait"] = "wait"
    duration: float = Field(..., gt=0, le=MAX_WAIT_SECONDS)


class CursorPosition(_Action):
    kind: Literal["cursor_position"] = "cursor_position"


ComputerAction = Annotated[
    Screenshot | Zoom | LeftClick | DoubleClick | Type | Key | Scroll | Wait | CursorPosition,
    Field(discriminator="kind"),
]

# Actions that carry a target coordinate, and therefore must be probed before
# execution so the trace records what was actually under the cursor.
COORDINATE_KINDS: frozenset[str] = frozenset({"left_click", "double_click", "scroll"})


# --------------------------------------------------------------------------- #
# terminal declarations
# --------------------------------------------------------------------------- #
#
# A run ends because the model said so in a schema-valid way, not because prose was
# parsed for the word "done". These four are exposed as custom tools alongside the
# computer toolset (plan §3).


class GoalComplete(_Action):
    """The model believes the goal is reached. Never sufficient on its own — Step 11
    verifies the checkpoint against observed state before accepting it."""

    kind: Literal["goal_complete"] = "goal_complete"
    summary: str = Field(..., description="One line: what was accomplished.")
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Values read off the final screen, e.g. the review panel's fields.",
    )


class BusinessOutcome(_Action):
    """A legitimate domain answer, not a malfunction: the member does not exist, the
    amount was rejected. These are first-class results, never exceptions."""

    kind: Literal["business_outcome"] = "business_outcome"
    code: str = Field(..., description="Screaming snake case, e.g. MEMBER_NOT_FOUND.")
    detail: str | None = None


class RequestHuman(_Action):
    kind: Literal["request_human"] = "request_human"
    reason: str
    context: str | None = Field(
        None, description="What the operator needs to know to take over."
    )


class CannotProceed(_Action):
    kind: Literal["cannot_proceed"] = "cannot_proceed"
    reason: str


TerminalDeclaration = Annotated[
    GoalComplete | BusinessOutcome | RequestHuman | CannotProceed,
    Field(discriminator="kind"),
]

AgentAction = Annotated[
    Screenshot
    | Zoom
    | LeftClick
    | DoubleClick
    | Type
    | Key
    | Scroll
    | Wait
    | CursorPosition
    | GoalComplete
    | BusinessOutcome
    | RequestHuman
    | CannotProceed,
    Field(discriminator="kind"),
]

_agent_action_adapter: TypeAdapter[Any] = TypeAdapter(AgentAction)
_computer_action_adapter: TypeAdapter[Any] = TypeAdapter(ComputerAction)

TERMINAL_KINDS: frozenset[str] = frozenset(
    {"goal_complete", "business_outcome", "request_human", "cannot_proceed"}
)


# --------------------------------------------------------------------------- #
# what a human did
# --------------------------------------------------------------------------- #


class HumanIntervention(_Action):
    """A person held the screen and changed something.

    Deliberately NOT a member of `AgentAction`, so the model cannot propose it: there is
    no tool schema for it, and `parse_action` would reject it. Only the controller
    constructs one, and only after a handoff completes.

    What it does not claim: any knowledge of what the human clicked. At this stage the
    step's own DOM diff is the record of what changed — the injected observer described
    in `IMPLEMENTATION.md` belongs to the escalation layer, not here. Recording a
    coordinate we never saw would be worse than recording none.
    """

    kind: Literal["human_intervention"] = "human_intervention"
    intervention_id: str
    reason: str
    operator: str | None = None
    context: str | None = Field(None, description="Why the human was called in.")


def parse_action(payload: dict[str, Any]) -> Any:
    """Parse one `tool_use.input` (plus its member name as `kind`).

    Raises `pydantic.ValidationError` for anything outside the vocabulary — including
    the eight disabled members and `human_intervention`, none of which have a place in
    the union by design.
    """
    return _agent_action_adapter.validate_python(payload)


def parse_computer_action(payload: dict[str, Any]) -> Any:
    """As `parse_action`, but refuses terminal declarations — for call sites that must
    be handed something executable."""
    return _computer_action_adapter.validate_python(payload)


def is_terminal(action: Any) -> bool:
    return getattr(action, "kind", None) in TERMINAL_KINDS


def _tool_schema(model: type[BaseModel], name: str, description: str) -> dict[str, Any]:
    schema = model.model_json_schema()
    # `kind` is how we discriminate locally; the API already knows which tool was
    # called by name, so leave it out of the wire schema.
    schema.get("properties", {}).pop("kind", None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "kind"]
    schema.pop("title", None)
    return {"name": name, "description": description, "input_schema": schema}


def terminal_tool_schemas() -> list[dict[str, Any]]:
    """The four custom-tool definitions Step 10 sends alongside the computer toolset.

    Generated from the same models the parser uses, so the contract the model is shown
    and the contract we enforce cannot drift apart.
    """
    return [
        _tool_schema(
            GoalComplete,
            "goal_complete",
            "Declare the goal reached. Call this only when the screen shows the "
            "expected end state; the result is verified independently before the run "
            "is accepted as successful.",
        ),
        _tool_schema(
            BusinessOutcome,
            "business_outcome",
            "Declare a legitimate domain outcome that is not a malfunction, such as a "
            "member that does not exist or input the application rejected.",
        ),
        _tool_schema(
            RequestHuman,
            "request_human",
            "Hand control to a human operator. Use when the screen shows something "
            "outside the task, or when proceeding would need a judgement you cannot make.",
        ),
        _tool_schema(
            CannotProceed,
            "cannot_proceed",
            "Stop: the goal cannot be achieved on this screen and no human handoff "
            "would help. Explain what blocked you.",
        ),
    ]
