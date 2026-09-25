"""
The model boundary.

Everything Anthropic-specific lives here: tool definitions, `tool_use` ids,
`toolset_name`, cache breakpoints, context editing, token accounting. The controller
above sees typed actions in and outcomes out, which is what lets the same loop run
against a real model in Step 14 and against a scripted `FakeProvider` in every test —
with no branching in the loop itself.

The provider owns the transcript deliberately. Tool results must reference the ids of
the `tool_use` blocks that produced them, and those ids are generated here; handing
that bookkeeping to the controller would leak the wire format into the one layer that
is supposed to be provider-agnostic.

What this module does NOT do: decide when to stop, execute anything, or record. It
proposes. Step 11 owns the loop.

Current API shape (see plan §4 — several of these differ from older references and each
one is a 400 if wrong):

  * `computer_toolset_20260801` takes NO display dimensions; the model infers the
    coordinate space from the screenshots it is sent.
  * every result for a computer member carries `toolset_name: "computer"`; results for
    the four terminal declarations must not.
  * all results for one assistant turn go back in a SINGLE user message.
  * instruction text goes before the image in a user turn.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from src.domain.actions import (
    DISABLED_MEMBERS,
    TERMINAL_KINDS,
    parse_action,
)
from src.discovery.prompts import system_prompt, turn_instruction

COMPUTER_TOOLSET = "computer_toolset_20260801"
TOOLSET_NAME = "computer"
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
DEFAULT_MODEL = "claude-opus-5"

# Sent when a batch is abandoned partway: the model needs to know the later actions did
# not run, or it will reason about a screen state that never happened.
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

# Opus 5 list pricing, per million tokens. Used only for the budget estimate the loop
# enforces; it is not billing.
#
# Cached tokens are priced separately, and the API reports them in their OWN buckets:
# `input_tokens` already excludes anything served from cache. So an estimate built
# from input+output alone silently bills cache reads at zero — an underestimate, which
# is the dangerous direction for a budget rule that is supposed to stop a runaway run.
# Measured on a live 2-turn smoke run: 6,586 cache-read tokens, 43% of billed input,
# which the old formula counted as free.
USD_PER_MTOK_IN = 5.0
USD_PER_MTOK_OUT = 25.0
USD_PER_MTOK_CACHE_READ = 0.5  # ~0.1x input
USD_PER_MTOK_CACHE_WRITE = 6.25  # ~1.25x input


@dataclass
class Usage:
    """Token and cost accounting. Step 11's budget rule reads this."""

    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    # Cache accounting. Worth tracking rather than assuming: if cache_read stays zero
    # across turns, the cache_control breakpoint is not earning its place and the
    # per-run cost estimate is wrong.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def usd_estimate(self) -> float:
        """`input_tokens` from the API excludes cached tokens, so the three input
        buckets are disjoint and priced at their own rates."""
        return (
            self.input_tokens / 1_000_000 * USD_PER_MTOK_IN
            + self.cache_read_tokens / 1_000_000 * USD_PER_MTOK_CACHE_READ
            + self.cache_creation_tokens / 1_000_000 * USD_PER_MTOK_CACHE_WRITE
            + self.output_tokens / 1_000_000 * USD_PER_MTOK_OUT
        )

    @property
    def cache_hit_rate(self) -> float:
        billed = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / billed if billed else 0.0

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cache_creation_tokens += cache_creation
        self.turns += 1


@dataclass
class InvalidAction:
    """Something the model asked for that the action union refused.

    Kept rather than raised: the model is told what was wrong and given a chance to
    correct itself, and the controller counts these toward its invalid-action limit.
    """

    tool_use_id: str
    name: str
    payload: dict[str, Any]
    reason: str


@dataclass
class ActionOutcome:
    """What actually happened to one proposed action, handed back by the controller."""

    tool_use_id: str
    ok: bool
    detail: str = ""
    # Images for screenshot/zoom results; text for everything else.
    image_png: bytes | None = None
    skipped: bool = False


@dataclass
class ProposedBatch:
    """One assistant turn's worth of proposals."""

    actions: list[Any] = field(default_factory=list)
    tool_use_ids: list[str] = field(default_factory=list)
    invalid: list[InvalidAction] = field(default_factory=list)
    # A short operational note, never hidden reasoning: the trace must not depend on
    # provider-specific transcripts.
    reason: str | None = None
    stop_reason: str | None = None

    @property
    def terminal(self) -> Any | None:
        return next((a for a in self.actions if a.kind in TERMINAL_KINDS), None)

    def __len__(self) -> int:
        return len(self.actions)


class ModelProvider(Protocol):
    async def propose(
        self, observation: Any, screenshot_png: bytes | None, *, note: str | None = None
    ) -> ProposedBatch: ...

    def record_results(self, outcomes: list[ActionOutcome]) -> None: ...

    @property
    def usage(self) -> Usage: ...


# --------------------------------------------------------------------------- #
# the real thing
# --------------------------------------------------------------------------- #


class AnthropicComputerUseProvider:
    """Claude, driving the computer toolset."""

    def __init__(
        self,
        goal: str,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int = 4096,
        effort: str = "high",
        client: Any | None = None,
    ) -> None:
        self.goal = goal
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.system = system_prompt(goal)
        self.messages: list[dict[str, Any]] = []
        self._usage = Usage()
        self._step = 0
        self._pending: ProposedBatch | None = None
        self._pending_system_note: dict[str, str] | None = None
        self._client = client
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    @property
    def usage(self) -> Usage:
        return self._usage

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    # ---- request shape ----

    def tool_definitions(self) -> list[dict[str, Any]]:
        """The toolset plus the four terminal declarations.

        `configs` disables the members this workflow does not need. Note what is
        absent: no `display_width_px`/`display_height_px`/`display_number`/`name` —
        those are rejected by the current API, and the model reads the coordinate
        space off the screenshots instead.
        """
        from src.domain.actions import terminal_tool_schemas

        return [
            {
                "type": COMPUTER_TOOLSET,
                "configs": {member: {"enabled": False} for member in DISABLED_MEMBERS},
                # Cache breakpoint on the stable prefix: the tool block and system
                # prompt never change within a run, so they should be paid for once.
                "cache_control": {"type": "ephemeral"},
            },
            *terminal_tool_schemas(),
        ]

    def request_kwargs(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tool_definitions(),
            "output_config": {"effort": self.effort},
            # Screenshots dominate context; drop stale tool results rather than
            # letting a 30-step run carry 30 images.
            "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
            "betas": [CONTEXT_MANAGEMENT_BETA],
        }

    # ---- turns ----

    def _user_turn(
        self, observation: Any, screenshot_png: bytes | None, note: str | None
    ) -> dict[str, Any]:
        """Text first, image second. Not cosmetic: describing the target before the
        image is processed measurably improves click accuracy."""
        content: list[dict[str, Any]] = [
            {"type": "text", "text": turn_instruction(self._step, note=note)}
        ]
        if screenshot_png is not None:
            content.append(_image_block(screenshot_png))
        return {"role": "user", "content": content}

    async def propose(
        self, observation: Any, screenshot_png: bytes | None, *, note: str | None = None
    ) -> ProposedBatch:
        self._step += 1
        self.messages.append(self._user_turn(observation, screenshot_png, note))

        if self._pending_system_note is not None:
            # Appended AFTER the user turn, so it is the last message in the array.
            # Discovered by a 400 on a live handoff: a content-carrying system message
            # is accepted only where it "precedes an assistant message or ends the
            # array". Queued at handoff time and placed here, it satisfies both — it
            # ends the array for this request, and once the response is appended it
            # precedes an assistant turn for every request after.
            self.messages.append(self._pending_system_note)
            self._pending_system_note = None

        client = self._get_client()
        response = await client.beta.messages.create(
            messages=self.messages, **self.request_kwargs()
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self._usage.add(
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
                cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_creation=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            )

        # Echo the assistant turn back verbatim — tool results must reference its ids.
        self.messages.append({"role": "assistant", "content": response.content})
        batch = self._parse(response)
        self._pending = batch
        return batch

    def _parse(self, response: Any) -> ProposedBatch:
        batch = ProposedBatch(stop_reason=getattr(response, "stop_reason", None))
        notes: list[str] = []

        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                notes.append(block.text.strip())
                continue
            if kind != "tool_use":
                continue

            payload = dict(block.input or {})
            payload["kind"] = block.name
            try:
                batch.actions.append(parse_action(payload))
                batch.tool_use_ids.append(block.id)
            except ValidationError as exc:
                # Not raised: the model is told what was wrong and can correct itself,
                # and the controller counts this toward its invalid-action limit.
                batch.invalid.append(
                    InvalidAction(
                        tool_use_id=block.id,
                        name=block.name,
                        payload=payload,
                        reason=_first_error(exc),
                    )
                )

        batch.reason = " ".join(notes)[:500] or None
        return batch

    # ---- results ----

    def record_results(self, outcomes: list[ActionOutcome]) -> None:
        """Answer every `tool_use` in one user message.

        All of them, in one message: splitting results across messages trains the
        model out of batching, which costs a round trip per action for the rest of
        the run.
        """
        if self._pending is None:
            return

        content: list[dict[str, Any]] = []
        by_id = {o.tool_use_id: o for o in outcomes}

        for action, tool_use_id in zip(self._pending.actions, self._pending.tool_use_ids):
            outcome = by_id.get(tool_use_id)
            content.append(_result_block(action.kind, tool_use_id, outcome))

        for invalid in self._pending.invalid:
            # Invalid actions get a result too — an unanswered tool_use is a protocol
            # error, and the explanation is how the model recovers.
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": invalid.tool_use_id,
                    "is_error": True,
                    "content": f"Rejected: {invalid.reason}",
                    **(
                        {"toolset_name": TOOLSET_NAME}
                        if invalid.name not in TERMINAL_KINDS
                        else {}
                    ),
                }
            )

        if content:
            _attach_trailing_screenshot(content, self._pending, outcomes)
            self.messages.append({"role": "user", "content": content})
        self._pending = None

    def add_system_note(self, note: dict[str, str]) -> None:
        """Queue a mid-conversation operator message — used after a human handoff.

        A system message rather than a user turn: it carries operator authority and does
        not invalidate the cached prefix on Opus 5.

        Queued rather than appended, because position is constrained. Appending it here
        put it between two user messages and earned a 400 on the first live handoff:
        `role 'system' must precede an 'assistant' message or end the array`. `propose`
        places it last instead.
        """
        self._pending_system_note = note


def _image_block(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode(),
        },
    }


def _result_block(kind: str, tool_use_id: str, outcome: ActionOutcome | None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id}

    # Terminal declarations are ordinary custom tools; tagging them with a toolset
    # the API does not associate them with is rejected.
    if kind not in TERMINAL_KINDS:
        block["toolset_name"] = TOOLSET_NAME

    if outcome is None:
        block["content"] = "No result recorded."
        block["is_error"] = True
        return block

    if outcome.skipped:
        block["content"] = NOT_EXECUTED
        block["is_error"] = True
        return block

    if outcome.image_png is not None:
        block["content"] = [_image_block(outcome.image_png)]
        return block

    block["content"] = outcome.detail or ("OK" if outcome.ok else "Failed")
    if not outcome.ok:
        block["is_error"] = True
    return block


def _attach_trailing_screenshot(
    content: list[dict[str, Any]], batch: ProposedBatch, outcomes: list[ActionOutcome]
) -> None:
    """If the batch did not end with a screenshot, append one to the last result.

    Saves a round trip per step: otherwise the model's next move is almost always to
    ask for the screenshot it could have been given.
    """
    if batch.actions and batch.actions[-1].kind in {"screenshot", "zoom"}:
        return
    trailing = next((o for o in reversed(outcomes) if o.image_png is not None), None)
    if trailing is None or not content:
        return
    last = content[-1]
    if isinstance(last.get("content"), list):
        last["content"].append(_image_block(trailing.image_png))
    else:
        last["content"] = [
            {"type": "text", "text": str(last.get("content", "OK"))},
            _image_block(trailing.image_png),
        ]


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid action"
    first = errors[0]
    location = ".".join(str(p) for p in first.get("loc", ())) or "action"
    return f"{location}: {first.get('msg', 'invalid')}"


# --------------------------------------------------------------------------- #
# the offline stand-in
# --------------------------------------------------------------------------- #


class FakeProvider:
    """Replays a scripted list of actions. No network, no key, deterministic.

    Every test uses this. Because the provider owns the transcript, swapping it in
    changes nothing above — the controller cannot tell the difference, which is the
    property that makes the tests worth anything.

    Script format (YAML list), one entry per turn:

        - actions: [{kind: left_click, coordinate: [550, 96]}, {kind: screenshot}]
          reason: "click the member field"
        - actions: [{kind: type, text: "12345"}]
        - invalid: [{kind: left_click_drag, start_coordinate: [1, 1], coordinate: [2, 2]}]
        - actions: [{kind: goal_complete, summary: "reached review"}]
    """

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self.script = script or []
        self.turn = 0
        self.recorded: list[list[ActionOutcome]] = []
        self.notes: list[dict[str, str]] = []
        self._usage = Usage()

    @classmethod
    def from_yaml(cls, path: str | Path) -> FakeProvider:
        return cls(yaml.safe_load(Path(path).read_text()) or [])

    @property
    def usage(self) -> Usage:
        return self._usage

    async def propose(
        self, observation: Any, screenshot_png: bytes | None, *, note: str | None = None
    ) -> ProposedBatch:
        if self.turn >= len(self.script):
            # A script that runs out is a script that never terminated. Say so rather
            # than looping forever.
            return ProposedBatch(
                actions=[parse_action({"kind": "cannot_proceed", "reason": "script exhausted"})],
                tool_use_ids=["fake_exhausted"],
            )

        entry = self.script[self.turn]
        self.turn += 1
        batch = ProposedBatch(reason=entry.get("reason"))

        for index, payload in enumerate(entry.get("actions", []) or []):
            batch.actions.append(parse_action(dict(payload)))
            batch.tool_use_ids.append(f"fake_{self.turn}_{index}")

        for index, payload in enumerate(entry.get("invalid", []) or []):
            try:
                parse_action(dict(payload))
                reason = "expected invalid, but it parsed"
            except ValidationError as exc:
                reason = _first_error(exc)
            batch.invalid.append(
                InvalidAction(
                    tool_use_id=f"fake_{self.turn}_bad{index}",
                    name=str(payload.get("kind", "?")),
                    payload=dict(payload),
                    reason=reason,
                )
            )

        # Rough but non-zero, so budget-limit tests have something to count.
        self._usage.add(1500, 80)
        return batch

    def record_results(self, outcomes: list[ActionOutcome]) -> None:
        self.recorded.append(list(outcomes))

    def add_system_note(self, note: dict[str, str]) -> None:
        self.notes.append(note)
