"""
What replay does about a screen it did not expect.

The artifact's `outcome_rules` say what each recognised exception state means and what to do about it. Two
strategies, and the boundary between them is the safety argument of this whole layer:

  * `wait_and_retry` — a loading overlay is a timing artifact, not a decision. Bounded, condition-based.
  * `dismiss` — clicking a modal away. **Only for a dialog the artifact names.**

A dialog nobody named is always an escalation. Dismissing a dialog you cannot identify is precisely the
failure the human handoff exists to prevent: the modal might be a confirmation, and "OK" might commit
something. The artifact's last outcome rule — `dialog: {unmatched: true} -> escalated: UNKNOWN_DIALOG` —
is that rule stated declaratively, and this module is it stated in code. Both, deliberately: the rule can
be read by a reviewer and cannot be edited away by a page.

Every strategy is bounded by the rule's own `max_attempts`. An unbounded dismiss loop against a modal that
keeps coming back is worse than a handoff — it looks like progress and makes none.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.domain.artifact import Capability, OutcomeRule
from src.domain.trace import Observation
from src.replay.conditions import evaluate

# The dialog's own dismiss control. The simulator renders `<button class="modal-button">OK</button>` inside
# `.modal-overlay`; scoped to the overlay so this can never reach a button on the page behind it.
DISMISS_SELECTOR = ".modal-overlay button.modal-button"


@dataclass(frozen=True)
class Recovery:
    """What was attempted, and whether the condition cleared. Recorded either way."""

    strategy: str
    attempts: int
    cleared: bool
    detail: str

    def __str__(self) -> str:
        verdict = "cleared" if self.cleared else "did not clear"
        return f"{self.strategy} after {self.attempts} attempt(s): {verdict} — {self.detail}"


class UnrecoverableDialog(Exception):
    """A `dismiss` rule matched a dialog the artifact does not name.

    Its own exception rather than a `Recovery(cleared=False)`, because the two mean different things: one
    is "I tried and it did not work", this is "I will not try". The engine turns it into an escalation.
    """


async def recover(
    surface: Any,
    rule: OutcomeRule,
    capability: Capability,
    *,
    frame_path: list[str] | None = None,
    inputs: dict[str, Any] | None = None,
) -> Recovery:
    """Run the rule's `recover` strategy until its condition clears or its attempts run out.

    Returns a `Recovery` whichever way it goes. The caller decides what a failure means, because that is
    written in the rule's `else:` — this module does not get to invent an outcome.
    """
    strategy = rule.recover.strategy
    if strategy == "wait_and_retry":
        return await _wait_and_retry(surface, rule, capability, frame_path, inputs)
    if strategy == "dismiss":
        return await _dismiss(surface, rule, capability, frame_path, inputs)
    raise UnrecoverableDialog(f"no recovery implemented for strategy {strategy!r}")


async def _still_matching(
    surface: Any, rule: OutcomeRule, capability: Capability, frame_path, inputs
) -> tuple[bool, Observation]:
    """Is the exception state still on screen? Re-observed, never assumed."""
    observation = await surface.observe(frame_path)
    result = await evaluate(
        rule.when,
        observation,
        surface=surface,
        frame_path=frame_path,
        inputs=inputs,
        named_dialogs=capability.named_dialogs,
    )
    return result.passed, observation


async def _wait_and_retry(
    surface: Any, rule: OutcomeRule, capability: Capability, frame_path, inputs
) -> Recovery:
    """Wait for the condition to stop holding, bounded by the rule.

    The wait's exit criterion is the condition, not the delay: `backoff_ms` is how often it looks, and
    `max_attempts` is what stops it. A loading overlay that never clears is a failure the caller should
    hear about, not something to sit behind forever.
    """
    backoff = rule.recover.backoff_ms / 1000
    for attempt in range(1, rule.recover.max_attempts + 1):
        await asyncio.sleep(backoff)
        still, _ = await _still_matching(surface, rule, capability, frame_path, inputs)
        if not still:
            return Recovery("wait_and_retry", attempt, True, "the condition cleared on its own")
    return Recovery(
        "wait_and_retry",
        rule.recover.max_attempts,
        False,
        f"still present after {rule.recover.max_attempts} wait(s) of {rule.recover.backoff_ms}ms",
    )


async def _dismiss(
    surface: Any, rule: OutcomeRule, capability: Capability, frame_path, inputs
) -> Recovery:
    """Click a named dialog away, and refuse to touch one that is not named.

    The check is on the rule, not on the screen: a `dismiss` strategy is only ever authorised by a rule
    whose `when` names a specific dialog. `unmatched: true` can never be dismissed, because there is no
    phrase to have recognised — which is the whole point of the catch-all existing.
    """
    named = getattr(rule.when, "contains", None)
    if rule.when.kind != "dialog" or not named:
        raise UnrecoverableDialog(
            "a dismiss rule must name the dialog it dismisses; refusing to close an unidentified one"
        )
    if named not in capability.named_dialogs:
        # Defence in depth: the rule names it, and the artifact agrees it is named. A rule that drifted
        # out of `named_dialogs` would otherwise authorise dismissing something the catch-all still
        # treats as unknown.
        raise UnrecoverableDialog(f"{named!r} is not among the artifact's named dialogs")

    for attempt in range(1, rule.recover.max_attempts + 1):
        button = surface.frame(frame_path).locator(DISMISS_SELECTOR)
        if await button.count():
            await surface.click(button.first)
        still, _ = await _still_matching(surface, rule, capability, frame_path, inputs)
        if not still:
            return Recovery("dismiss", attempt, True, f"{named!r} dismissed")
        await asyncio.sleep(rule.recover.backoff_ms / 1000)

    return Recovery(
        "dismiss",
        rule.recover.max_attempts,
        False,
        f"{named!r} returned after {rule.recover.max_attempts} dismissal(s)",
    )
