"""
Provider tests — offline, no key, no network.

Two things are being pinned. First, that `FakeProvider` is a genuine drop-in, so every
later test of the loop is testing the loop rather than a mock. Second, the **request
shape**: the current computer-use API rejects several fields older references still
show, and a 400 during a paid multi-step run is an expensive way to learn that.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.discovery.model_provider import (
    COMPUTER_TOOLSET,
    NOT_EXECUTED,
    TOOLSET_NAME,
    ActionOutcome,
    AnthropicComputerUseProvider,
    FakeProvider,
    ProposedBatch,
    Usage,
)
from src.discovery.prompts import INJECTION_NOTICE, STOP_AT_REVIEW, system_prompt
from src.domain.actions import DISABLED_MEMBERS, ENABLED_MEMBERS, TERMINAL_KINDS

SCRIPTS = Path(__file__).parent / "fixtures" / "scripts"
GOAL = "Find member 12345 and prepare a savings sub-account; stop at review"
PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture
def provider() -> AnthropicComputerUseProvider:
    """No client, so nothing can accidentally reach the network."""
    return AnthropicComputerUseProvider(GOAL, api_key="not-used", client=object())


# --------------------------------------------------------------------------- #
# FakeProvider — the drop-in
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fake_provider_replays_a_script_in_order():
    fake = FakeProvider.from_yaml(SCRIPTS / "happy_path.yaml")

    first = await fake.propose(None, PNG)
    assert [a.kind for a in first.actions] == ["left_click", "screenshot"]
    assert first.reason == "reset to the search page"

    second = await fake.propose(None, PNG)
    assert [a.kind for a in second.actions] == ["left_click", "type", "key", "screenshot"]
    assert second.actions[1].text == "12345"


@pytest.mark.asyncio
async def test_fake_provider_reaches_a_terminal_declaration():
    fake = FakeProvider.from_yaml(SCRIPTS / "happy_path.yaml")
    for _ in range(3):
        await fake.propose(None, PNG)
    final = await fake.propose(None, PNG)
    assert final.terminal is not None
    assert final.terminal.kind == "goal_complete"


@pytest.mark.asyncio
async def test_a_script_that_never_terminates_says_so():
    """Rather than looping forever, which would be a test that hangs CI."""
    fake = FakeProvider([{"actions": [{"kind": "screenshot"}]}])
    await fake.propose(None, PNG)
    exhausted = await fake.propose(None, PNG)
    assert exhausted.terminal.kind == "cannot_proceed"


@pytest.mark.asyncio
async def test_disabled_members_land_in_invalid_never_in_actions():
    """The schema gate from Step 4, seen from the provider's side: a disabled member
    is reported to the model as an error and never becomes an executable action."""
    fake = FakeProvider.from_yaml(SCRIPTS / "invalid_action.yaml")
    batch = await fake.propose(None, PNG)

    assert [a.kind for a in batch.actions] == ["screenshot"]
    assert {i.name for i in batch.invalid} == {"left_click_drag", "left_click"}
    assert all(i.reason for i in batch.invalid), "the model must be told what was wrong"


@pytest.mark.asyncio
async def test_fake_provider_records_what_the_controller_hands_back():
    fake = FakeProvider.from_yaml(SCRIPTS / "happy_path.yaml")
    batch = await fake.propose(None, PNG)
    outcomes = [ActionOutcome(tool_use_id=i, ok=True) for i in batch.tool_use_ids]
    fake.record_results(outcomes)
    assert fake.recorded == [outcomes]


@pytest.mark.asyncio
async def test_fake_provider_accumulates_usage():
    """Non-zero so Step 11's budget rule has something to count."""
    fake = FakeProvider.from_yaml(SCRIPTS / "happy_path.yaml")
    await fake.propose(None, PNG)
    await fake.propose(None, PNG)
    assert fake.usage.turns == 2
    assert fake.usage.input_tokens > 0
    assert fake.usage.usd_estimate > 0


# --------------------------------------------------------------------------- #
# the request shape — where a 400 would come from
# --------------------------------------------------------------------------- #


def test_toolset_uses_the_current_type(provider: AnthropicComputerUseProvider):
    tools = provider.tool_definitions()
    assert tools[0]["type"] == COMPUTER_TOOLSET == "computer_toolset_20260801"


@pytest.mark.parametrize(
    "rejected", ["display_width_px", "display_height_px", "display_number", "name", "enable_zoom"]
)
def test_rejected_legacy_fields_are_absent(provider, rejected: str):
    """Every one of these returns invalid_request_error on the current API. They
    appear in older references, which is exactly why this test exists."""
    assert rejected not in provider.tool_definitions()[0]


def test_configs_disables_exactly_the_unused_members(provider):
    configs = provider.tool_definitions()[0]["configs"]
    assert set(configs) == set(DISABLED_MEMBERS)
    assert all(c == {"enabled": False} for c in configs.values())
    # ...and never disables something the parser accepts.
    assert not set(configs) & set(ENABLED_MEMBERS)


def test_terminal_declarations_are_sent_as_custom_tools(provider):
    names = [t.get("name") for t in provider.tool_definitions()[1:]]
    assert set(names) == TERMINAL_KINDS


def test_request_carries_context_editing_and_effort(provider):
    kwargs = provider.request_kwargs()
    assert kwargs["betas"] == ["context-management-2025-06-27"]
    assert kwargs["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["model"] == "claude-opus-5"


def test_tool_block_is_cached(provider):
    """The tool block and system prompt never change within a run; paying for them
    once per turn would be the single largest avoidable cost."""
    assert provider.tool_definitions()[0]["cache_control"] == {"type": "ephemeral"}


# --------------------------------------------------------------------------- #
# turn construction
# --------------------------------------------------------------------------- #


def test_text_comes_before_the_image(provider):
    """Not cosmetic: the target description must be read before the image is
    processed, which measurably improves click accuracy."""
    turn = provider._user_turn(None, PNG, None)
    assert [b["type"] for b in turn["content"]] == ["text", "image"]


def test_a_turn_without_a_screenshot_is_still_valid(provider):
    turn = provider._user_turn(None, None, None)
    assert [b["type"] for b in turn["content"]] == ["text"]


def test_operator_note_rides_along_in_the_turn(provider):
    turn = provider._user_turn(None, PNG, "a human changed the screen")
    assert "a human changed the screen" in turn["content"][0]["text"]


# --------------------------------------------------------------------------- #
# result blocks — the other place a 400 comes from
# --------------------------------------------------------------------------- #


async def _batch_of(provider, kinds: list[str]) -> ProposedBatch:
    from src.domain.actions import parse_action

    payloads = {
        "left_click": {"kind": "left_click", "coordinate": [1, 2]},
        "screenshot": {"kind": "screenshot"},
        "type": {"kind": "type", "text": "12345"},
        "goal_complete": {"kind": "goal_complete", "summary": "done"},
    }
    batch = ProposedBatch(
        actions=[parse_action(payloads[k]) for k in kinds],
        tool_use_ids=[f"tu_{i}" for i in range(len(kinds))],
    )
    provider._pending = batch
    return batch


@pytest.mark.asyncio
async def test_computer_results_carry_the_toolset_name(provider):
    await _batch_of(provider, ["left_click", "screenshot"])
    provider.record_results(
        [
            ActionOutcome(tool_use_id="tu_0", ok=True, detail="clicked"),
            ActionOutcome(tool_use_id="tu_1", ok=True, image_png=PNG),
        ]
    )
    blocks = provider.messages[-1]["content"]
    assert all(b["toolset_name"] == TOOLSET_NAME for b in blocks)


@pytest.mark.asyncio
async def test_terminal_results_do_not_carry_the_toolset_name(provider):
    """They are ordinary custom tools; tagging them with a toolset the API does not
    associate them with is rejected."""
    await _batch_of(provider, ["goal_complete"])
    provider.record_results([ActionOutcome(tool_use_id="tu_0", ok=True)])
    assert "toolset_name" not in provider.messages[-1]["content"][0]


@pytest.mark.asyncio
async def test_all_results_go_back_in_one_user_message(provider):
    """Splitting them trains the model out of batching, costing a round trip per
    action for the rest of the run."""
    await _batch_of(provider, ["left_click", "type", "screenshot"])
    provider.record_results(
        [ActionOutcome(tool_use_id=f"tu_{i}", ok=True) for i in range(3)]
    )
    messages = [m for m in provider.messages if m["role"] == "user"]
    assert len(messages) == 1
    assert len(messages[0]["content"]) == 3


@pytest.mark.asyncio
async def test_actions_after_a_failure_are_marked_not_executed(provider):
    """The model must know the later actions did not run, or it reasons about a
    screen state that never existed."""
    await _batch_of(provider, ["left_click", "type", "screenshot"])
    provider.record_results(
        [
            ActionOutcome(tool_use_id="tu_0", ok=True),
            ActionOutcome(tool_use_id="tu_1", ok=False, detail="click missed"),
            ActionOutcome(tool_use_id="tu_2", ok=False, skipped=True),
        ]
    )
    blocks = provider.messages[-1]["content"]
    assert blocks[1]["is_error"] is True
    assert blocks[2]["content"] == NOT_EXECUTED
    assert blocks[2]["is_error"] is True


@pytest.mark.asyncio
async def test_screenshot_results_are_sent_as_images(provider):
    await _batch_of(provider, ["screenshot"])
    provider.record_results([ActionOutcome(tool_use_id="tu_0", ok=True, image_png=PNG)])
    content = provider.messages[-1]["content"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_a_batch_not_ending_in_a_screenshot_gets_one_appended(provider):
    """Saves a round trip: otherwise the model's next move is almost always to ask
    for the screenshot it could have been handed."""
    await _batch_of(provider, ["left_click"])
    provider.record_results([ActionOutcome(tool_use_id="tu_0", ok=True, image_png=PNG)])
    last = provider.messages[-1]["content"][-1]
    assert isinstance(last["content"], list)
    assert any(b["type"] == "image" for b in last["content"])


@pytest.mark.asyncio
async def test_every_invalid_action_still_gets_answered(provider):
    """An unanswered tool_use is a protocol error, and the explanation is how the
    model corrects itself."""
    from src.discovery.model_provider import InvalidAction

    provider._pending = ProposedBatch(
        invalid=[
            InvalidAction(
                tool_use_id="tu_bad", name="left_click_drag", payload={}, reason="unknown kind"
            )
        ]
    )
    provider.record_results([])
    block = provider.messages[-1]["content"][0]
    assert block["is_error"] is True
    assert "unknown kind" in block["content"]


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #


def test_prompt_lists_exactly_the_enabled_vocabulary():
    """Built from the same constant the parser enforces, so the contract shown and
    the contract applied cannot drift."""
    prompt = system_prompt(GOAL)
    for member in ENABLED_MEMBERS:
        assert member in prompt
    for member in DISABLED_MEMBERS:
        assert member not in prompt


def test_prompt_carries_the_goal_and_the_stop_rule():
    prompt = system_prompt(GOAL)
    assert GOAL in prompt
    assert STOP_AT_REVIEW in prompt
    assert "Open Account" in prompt


def test_prompt_carries_the_injection_notice():
    assert INJECTION_NOTICE in system_prompt(GOAL)


def test_prompt_requires_a_tool_call_to_end_the_run():
    """Prose completion is not completion; only the declaration ends a run, and even
    that is verified against the screen."""
    prompt = system_prompt(GOAL)
    assert "Do not announce completion in prose" in prompt
    for kind in TERMINAL_KINDS:
        assert kind in prompt


def test_usage_costs_what_the_price_list_says():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.usd_estimate == pytest.approx(30.0)


def test_cache_reads_are_billed_rather_than_ignored():
    """The API reports cache reads in their own bucket — `input_tokens` already
    excludes them. So an estimate of input+output alone bills them at ZERO, which
    understates the run: the dangerous direction for a rule meant to stop a runaway.

    A live 2-turn smoke run reported 6,586 cache-read tokens (43% of billed input),
    all of which the old formula counted as free.
    """
    with_cache = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
    ignoring_cache = Usage(input_tokens=1_000_000)
    assert with_cache.usd_estimate > ignoring_cache.usd_estimate
    assert with_cache.usd_estimate == pytest.approx(5.5)  # 5.00 input + 0.50 cache read
    assert with_cache.cache_hit_rate == pytest.approx(0.5)


def test_cache_writes_cost_more_than_plain_input():
    """Writing the cache is ~1.25x; it pays back only if the prefix is reused."""
    assert Usage(cache_creation_tokens=1_000_000).usd_estimate == pytest.approx(6.25)


def test_cache_hit_rate_is_zero_without_cache_reads():
    assert Usage(input_tokens=1000).cache_hit_rate == 0.0
    assert Usage().cache_hit_rate == 0.0


# --------------------------------------------------------------------------- #
# the mid-conversation operator message (Step 12)
# --------------------------------------------------------------------------- #


class _StubClient:
    """Answers `propose` with an empty assistant turn, recording what it was sent.

    Enough to pin message ORDER, which is the thing the API is fussy about and the thing
    a 400 in the middle of a paid run is an expensive way to discover.
    """

    def __init__(self) -> None:
        self.sent: list[list[dict]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, *, messages, **kwargs):
        self.sent.append([dict(m) for m in messages])
        return SimpleNamespace(
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


@pytest.mark.asyncio
async def test_a_system_note_is_placed_last_in_the_array():
    """Position is constrained, and the constraint is not obvious.

    The API accepts a content-carrying system message only where it "precedes an
    assistant message or ends the array". Appending it at handoff time put it between two
    user messages and returned a 400 on the first live handoff, so it is queued and
    placed after the user turn instead.
    """
    client = _StubClient()
    provider = AnthropicComputerUseProvider(GOAL, api_key="not-used", client=client)

    provider.add_system_note({"role": "system", "content": "a human took control"})
    assert provider.messages == [], "queued, not appended"

    await provider.propose(None, PNG)

    sent = client.sent[0]
    assert sent[-2]["role"] == "user"
    assert sent[-1]["role"] == "system", "the note must END the array it is sent in"
    assert sent[-1]["content"] == "a human took control"


@pytest.mark.asyncio
async def test_the_note_precedes_the_assistant_turn_on_every_later_request():
    """The other half of the same constraint: once the response is appended, the note
    sits immediately before an assistant message, which is the legal position there."""
    provider = AnthropicComputerUseProvider(GOAL, api_key="not-used", client=_StubClient())
    provider.add_system_note({"role": "system", "content": "a human took control"})
    await provider.propose(None, PNG)

    roles = [m["role"] for m in provider.messages]
    assert roles[roles.index("system") + 1] == "assistant"


@pytest.mark.asyncio
async def test_a_queued_note_is_sent_once_not_on_every_turn():
    provider = AnthropicComputerUseProvider(GOAL, api_key="not-used", client=_StubClient())
    provider.add_system_note({"role": "system", "content": "note"})
    await provider.propose(None, PNG)
    provider.record_results([])
    await provider.propose(None, PNG)

    assert [m["role"] for m in provider.messages].count("system") == 1
