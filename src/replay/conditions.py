"""
Does a `Condition` hold against the screen in front of us?

One place, because every assertion in the artifact is a `Condition` — preconditions, postconditions, the
`verify-outcome` checkpoint, and the `when` of every outcome rule. A wrong haystack here is a wrong
answer in all four.

Most kinds read a `trace.Observation` and nothing else, which is why `ReplaySurface.observe()` returns
the discovery shape rather than a replay-specific one: the same snapshot the policy engine checks is the
snapshot conditions are evaluated against, and most of this module is testable without a browser. The two
element-scoped kinds (`value`, `checked`) read the step's own locator, and a `text` assertion carrying a
`within` reads that region — both through the surface, so the ownership guard still applies.

Three things decided here rather than left implicit:

  * **The fuzzy rule belongs where the page chose the rendering.** `text`, `heading`, `url` and `dialog`
    match case-insensitive substrings — the run declares `savings` and the page renders `Savings`,
    declares `50.00` and renders `$50.00`. The first three get it from `_TextPredicate.matches` on the
    models themselves rather than a copy here; `dialog` is not a `_TextPredicate` and applies the same
    rule inline. `value` and `checked` use `equals`, exactly, because there replay chose the value: a
    field merely *containing* `23456` (say `234567`) is a different fact from one that equals it.
  * **`url` means the frame's URL.** The workflow lives in an iframe, so after the results row is clicked
    the outer page is still `/` while the frame is at `/servicing/members/23456`. Reading
    `main_frame_url` makes every `url` condition in the artifact permanently false.
  * **An absence cannot be waited for the way a presence can** — see `wait_for`.
  * **A text assertion is only as sharp as the region it reads.** Unscoped it reads the whole frame,
    which made the `verify-outcome` checkpoint vacuous: the submitted form is still on screen and its
    `<select>` renders every account type, so the checkpoint passed for every value claimed. `within`
    narrows it to the region that actually shows the outcome.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from src.domain.artifact import Condition, substitute
from src.domain.trace import Observation

# The granularity at which a wait re-checks, not a duration anyone waits out. The distinction matters
# because the rule is condition-based waits only: the loop exits on the condition, and shortening this
# would only make it spin.
POLL_INTERVAL_S = 0.1


@dataclass(frozen=True)
class ConditionResult:
    """One condition, evaluated. Mirrors the resolver's `Attempt`: when a replay fails against a changed
    app, what was expected and what was actually on screen *is* the deliverable."""

    kind: str
    expected: str
    actual: str
    passed: bool

    def __str__(self) -> str:
        verdict = "ok" if self.passed else "FAILED"
        return f"{self.kind}({self.expected}): {verdict} — saw {self.actual}"


class ConditionError(Exception):
    """A condition that cannot be evaluated at all, as opposed to one that does not hold."""


def frame_url(observation: Observation) -> str:
    """The URL of the innermost frame observed, falling back to the page.

    `observe()` records the outer page first and each frame in the path after it, so the last entry is
    the deepest. The fallback covers a target that is not frame-scoped at all.
    """
    for info in reversed(observation.frames):
        if info.url:
            return info.url
    return observation.main_frame_url or ""


def is_positive(condition: Condition) -> bool:
    """Can waiting for this condition distinguish "arrived and correct" from "has not arrived"?

    `text_absent` and `overlay(present=False)` are satisfied by an empty screen, so they cannot be
    polled for — see `wait_for`. Element-scoped kinds count as positive even when asserting a negative
    (`checked: false`): resolving their locator already proved the screen is there.
    """
    if condition.kind == "text_absent":
        return False
    if condition.kind == "overlay":
        return condition.present
    return True


async def evaluate(
    condition: Condition,
    observation: Observation,
    *,
    surface: Any = None,
    locator: Any = None,
    frame_path: list[str] | None = None,
    inputs: dict[str, Any] | None = None,
    named_dialogs: tuple[str, ...] = (),
) -> ConditionResult:
    """Evaluate one condition against one snapshot.

    `${inputs.*}` is substituted into the expected value first, and an unsupplied input raises rather
    than becoming an empty string — an assertion on a truncated string would quietly pass.

    `frame_path` is passed in rather than read off `observation.frames[-1].path`, which looks equivalent
    and is not: `observe()` records each frame's own name, not the cumulative path, so the two agree only
    while nesting stays one level deep. `wait_for` has the authoritative value and forwards it.
    """
    inputs = inputs or {}
    kind = condition.kind

    if kind in {"text", "text_absent"}:
        haystack: str | list[str] = observation.visible_text or ""
        if condition.within:
            # A scoped assertion reads one region rather than the frame. Without this the
            # `verify-outcome` checkpoint could not fail: the submitted form is still on screen and its
            # `<select>` renders every account type, so any claimed value matched.
            if surface is None:
                raise ConditionError(
                    f"{kind} condition is scoped to {condition.within!r}, which needs the surface to read"
                )
            haystack = await surface.scoped_text(frame_path, condition.within)
        return _predicate(condition, haystack, inputs, negate=kind == "text_absent")

    if kind == "heading":
        return _predicate(condition, observation.headings, inputs)

    if kind == "url":
        return _predicate(condition, frame_url(observation), inputs)

    if kind == "dialog":
        # `DialogCondition` is not a `_TextPredicate` — it carries `contains`/`unmatched` rather than
        # `contains`/`any_of`, and has no `matches()`. So the substring rule is applied here rather than
        # through `_predicate`, which would reach for `any_of` and find nothing.
        texts = [d.text or "" for d in observation.dialogs]
        if condition.unmatched:
            # "A dialog this artifact does not name." Every named phrase is checked against every open
            # dialog; anything left over is the one case where guessing is worse than stopping.
            unknown = [t for t in texts if not any(n.lower() in t.lower() for n in named_dialogs)]
            return ConditionResult(
                kind="dialog",
                expected=f"a dialog not named by the artifact (named: {list(named_dialogs)})",
                actual=_shorten(unknown) or "no unnamed dialog",
                passed=bool(unknown),
            )
        needle = substitute(condition.contains, inputs)
        return ConditionResult(
            kind="dialog",
            expected=needle,
            actual=_shorten(texts) or "no dialog",
            passed=any(needle.lower() in t.lower() for t in texts),
        )

    if kind == "overlay":
        return ConditionResult(
            kind="overlay",
            expected=f"present={condition.present}",
            actual=f"{observation.overlays}",
            passed=bool(observation.overlays) is condition.present,
        )

    # ---- element-scoped: the assertion is about the step's own control, not the screen ----

    if locator is None or surface is None:
        raise ConditionError(f"{kind} condition needs the step's locator, and none was resolved")

    if kind == "value":
        expected = substitute(condition.equals, inputs)
        actual = await surface.input_value(locator)
        # Exact, deliberately: see the module docstring. `.strip()` only because a browser reports the
        # field, not the markup's indentation.
        return ConditionResult("value", expected, actual, passed=actual.strip() == expected)

    if kind == "checked":
        actual_checked = await surface.is_checked(locator)
        return ConditionResult(
            "checked", str(condition.equals), str(actual_checked), passed=actual_checked is condition.equals
        )

    raise ConditionError(f"no evaluator for condition kind {kind!r}")


def _predicate(
    condition: Any,
    haystack: str | list[str],
    inputs: dict[str, Any],
    *,
    negate: bool = False,
) -> ConditionResult:
    """Apply a `_TextPredicate` to one string or to a list of them.

    The case-insensitive substring rule is `matches()` on the model itself, so it is shared with
    everything else that reads a condition rather than reimplemented here. Substitution happens on a
    copy, because the condition belongs to a loaded artifact that other steps still read.
    """
    resolved = condition.model_copy(
        update={
            "contains": substitute(condition.contains, inputs) if condition.contains is not None else None,
            "any_of": [substitute(n, inputs) for n in condition.any_of],
        }
    )
    hit = (
        any(resolved.matches(item) for item in haystack)
        if isinstance(haystack, list)
        else resolved.matches(haystack)
    )
    expected = resolved.contains if resolved.contains is not None else f"any of {resolved.any_of}"
    return ConditionResult(
        kind=condition.kind,
        expected=expected if not negate else f"absent: {expected}",
        actual=_shorten(haystack),
        passed=(not hit) if negate else hit,
    )


def _shorten(haystack: str | list[str], limit: int = 160) -> str:
    text = " | ".join(haystack) if isinstance(haystack, list) else haystack
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


async def wait_for(
    surface: Any,
    frame_path: list[str] | None,
    conditions: list[Condition],
    *,
    locator: Any = None,
    inputs: dict[str, Any] | None = None,
    named_dialogs: tuple[str, ...] = (),
    timeout_ms: int = 5000,
    baseline: Observation | None = None,
) -> tuple[bool, list[ConditionResult]]:
    """Wait until every condition holds, re-observing each pass. Returns `(passed, results)`.

    Returns a verdict rather than raising, unlike the resolver: a resolver must hand back a locator or
    nothing, while "these three held and that one did not" is itself the answer, and which layer turns it
    into a `FailureCode` is the engine's decision, not this one's.

    **An absence cannot be polled for.** `continue`'s only postcondition is
    `text_absent: "You must accept the account disclosure to continue."` — and immediately after the
    click, before the form has submitted, that text is absent from the *old* page too. Polling it would
    pass instantly and certify a screen that never arrived, which is the worst failure this layer has:
    a false pass.

    So a group with no positive condition waits on the screen changing instead. With a `baseline` it
    polls until `visible_text` differs, then evaluates the absence once; a timeout is a **failure**,
    which also catches an action that did nothing at all. Without a baseline there is nothing to compare
    against and the absence is evaluated immediately — honest, but weaker, and the reason Step 9 passes
    the observation it already takes before acting.
    """
    if not conditions:
        return True, []

    deadline = time.monotonic() + timeout_ms / 1000
    waiting_on_change = not any(is_positive(c) for c in conditions) and baseline is not None

    while True:
        observation = await surface.observe(frame_path)

        if waiting_on_change and observation.visible_text == (baseline.visible_text if baseline else None):
            settled = False
            results = [
                ConditionResult(
                    kind=c.kind,
                    expected="the screen to change before asserting an absence",
                    actual="unchanged since before the action",
                    passed=False,
                )
                for c in conditions
            ]
        else:
            results = [
                await evaluate(
                    c,
                    observation,
                    surface=surface,
                    locator=locator,
                    frame_path=frame_path,
                    inputs=inputs,
                    named_dialogs=named_dialogs,
                )
                for c in conditions
            ]
            settled = all(r.passed for r in results)

        if settled:
            return True, results
        if time.monotonic() >= deadline:
            return False, results
        await asyncio.sleep(POLL_INTERVAL_S)
