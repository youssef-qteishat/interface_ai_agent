"""
Candidate bundle in, exactly one element out — or a refusal that says why.

This is where the artifact's central claim gets tested against reality. Discovery recorded a ladder of
ways to find each control; replay tries them in order and must end up on *one* element, the right one,
or stop. Three rules carry the weight:

  * **Absence falls through, ambiguity does not.** A candidate matching nothing is a fallback signal —
    that is what a bundle is for. A candidate matching several is a statement that the page contains
    something the artifact cannot tell apart, and picking one of them is how a replay clicks the wrong
    button and then reports success.
  * **A unique match can still be the wrong element**, so the resolved node's tag is checked against
    what discovery saw before it is handed back.
  * **Every attempt is recorded.** When an app changes under a committed artifact, the diagnostics are
    the whole product: a human needs to know which locators were tried and how each one failed.

Locators are never cached across steps. The results panel is swapped in by htmx, and a locator held
from before the swap points at a detached node.

One thing Playwright does *not* auto-wait for is `count()` — it is a point-in-time query, unlike an
action or an `expect()`. The first live run failed on exactly that: the results row was queried before
htmx had finished swapping the panel in. So the ladder is walked on a bounded poll rather than once.
That is still a condition-based wait, which is the rule; a fixed sleep would not be.

Polling alone was not enough, and the way it failed is worth keeping written down. Walking the ladder
takes a round-trip per candidate, so on a page that is still arriving the *later* candidates are
queried later in wall-clock time and the earlier ones are the only ones that see the old screen. The
live probe duly reported that `role "Open Sub-Account"` matched nothing and `css a > button.action-button`
saved the step — on a page where all three candidates match. A fall-through caused by latency is worse
than a slow resolve: it is noise in the one signal that is supposed to mean *your primary locator
stopped working*, and it would make a real degradation unremarkable. So resolution is two phases —
wait for the screen, then decide on it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from src.domain.artifact import Target
from src.domain.results import FailureCode
from src.surfaces.base import SurfaceError


@dataclass(frozen=True)
class Attempt:
    """One candidate, tried. The record a human reads when an artifact stops working."""

    kind: str
    how: str
    matched: int | None = None
    rejected: str | None = None

    def __str__(self) -> str:
        outcome = self.rejected or f"matched {self.matched}"
        return f"{self.kind}({self.how}): {outcome}"


class TargetResolutionError(SurfaceError):
    """Base for the two ways resolution ends badly. They are different facts."""

    code: FailureCode

    def __init__(self, message: str, attempts: list[Attempt]) -> None:
        super().__init__(message)
        self.attempts = attempts

    def diagnostics(self) -> str:
        return "\n".join(f"    {a}" for a in self.attempts)


class TargetNotResolved(TargetResolutionError):
    """Every candidate was tried and none identified an element."""

    code = FailureCode.TARGET_NOT_RESOLVED


class TargetAmbiguous(TargetResolutionError):
    """A candidate matched more than one element.

    Raised **immediately**, without trying the remaining candidates. Falling through would be worse
    than useless: the page is telling us the artifact cannot distinguish these elements, and a later
    candidate that happens to match one of them would paper over exactly that.
    """

    code = FailureCode.TARGET_AMBIGUOUS


def _contextual_xpath(anchor: str, relative: str) -> str:
    """The element whose nearest **non-empty preceding sibling cell** holds the anchor text.

    This is `probe.js`'s `tableRelativeLabel` walk, and the precision matters twice on this app:

      * the search row is `[label "Member ID:"] [input] [button Search]` — the walk skips the input's
        empty cell, which is why the Search button's anchor is "Member ID" and not "";
      * the form's last row is `[button Back] [button Continue]`, so Continue's anchor is "Back". The
        obvious translation — filter rows containing "Back", then take the buttons — matches **both**
        buttons, and since ambiguity is a hard stop that would kill a run the artifact can survive.

    `preceding-sibling::td[normalize-space(.)!=''][1]` is the nearest non-empty preceding cell: the
    predicate filters first, then `[1]` takes the closest. Both colon spellings are accepted because
    the probe strips a trailing colon from the anchor while the cell renders `Member ID:`.
    """
    quoted = _xpath_literal(anchor)
    quoted_colon = _xpath_literal(f"{anchor}:")
    nearest = "preceding-sibling::td[normalize-space(.)!=''][1]"
    return (
        f".//td[normalize-space({nearest})={quoted} or normalize-space({nearest})={quoted_colon}]"
        f"//{relative}"
    )


def _xpath_literal(value: str) -> str:
    """XPath 1.0 has no escape character, so a value with both quote kinds needs concat()."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    joined = ", \"'\", ".join(f"'{p}'" for p in parts)
    return f"concat({joined})"


def build_locator(scope: Any, candidate: Any) -> tuple[Any, str]:
    """One candidate → a Playwright locator, plus a short description for the attempt log."""
    kind = candidate.kind

    if kind == "role":
        return scope.get_by_role(candidate.role, name=candidate.name), f"{candidate.role}/{candidate.name}"

    if kind == "label":
        # The disclosure checkbox is an implicit label — the input is nested inside the <label>.
        return scope.get_by_label(candidate.text), candidate.text[:40]

    if kind == "contextual_text":
        xpath = _contextual_xpath(candidate.anchor, candidate.relative)
        return scope.locator(f"xpath={xpath}"), f"{candidate.anchor} -> {candidate.relative}"

    if kind == "text":
        return scope.locator(candidate.tag, has_text=candidate.text), f"{candidate.tag}:{candidate.text}"

    if kind in {"attribute", "css"}:
        return scope.locator(candidate.selector), candidate.selector

    raise SurfaceError(f"no Playwright mapping for candidate kind {kind!r}")


# How often the ladder is re-walked while waiting for a page to settle. Short enough that a fast swap
# costs nothing, long enough not to spin.
POLL_INTERVAL_S = 0.1


async def resolve(
    surface: Any, target: Target, *, timeout_ms: int = 5000
) -> tuple[Any, list[Attempt]]:
    """Resolve a target to exactly one element, or refuse.

    Two phases, because they answer two different questions.

    **Phase 1 — has the screen arrived?** The whole ladder is walked on a bounded poll until *some*
    candidate matches. The ladder rather than one candidate, because a bundle whose element is
    genuinely gone should cost **one** timeout and not one per candidate: waiting 5s on each of four
    turns a clean fall-through into a 20-second stall.

    **Phase 2 — which candidate wins?** The ladder is walked again, from the top, on the settled page,
    and *that* walk is what gets returned and reported. Phase 1's attempts are thrown away on purpose:
    they were taken against a screen that was still in flight, and reporting them is how the probe came
    to claim that `role "Open Sub-Account"` had stopped working on a page where it matches. A candidate
    is only judged once there is a page to judge it on.

    The narrow race left is a candidate that appears between the two walks. On a server-rendered page
    the elements of a screen arrive together, so in practice phase 2 sees all of them or none; if it
    somehow sees none, phase 1's result stands rather than the step failing.

    Returns the locator and the attempt log. The log comes back on success too, because which candidate
    *won* is the interesting fact: a run whose primary locator quietly stopped working and fell through
    to CSS is still passing, and is still something someone should look at.
    """
    scope = surface.frame(target.frame_path)
    expected_tag = (target.evidence.expected_tag if target.evidence else None) or None
    deadline = time.monotonic() + timeout_ms / 1000

    while True:
        settled, settle_attempts = await _walk(scope, target, expected_tag)
        if settled is not None:
            break
        if time.monotonic() >= deadline:
            raise TargetNotResolved(
                f"none of {len(target.candidates)} candidate(s) identified an element "
                f"within {timeout_ms}ms",
                settle_attempts,
            )
        await asyncio.sleep(POLL_INTERVAL_S)

    decided, attempts = await _walk(scope, target, expected_tag)
    if decided is None:
        return settled, settle_attempts
    return decided, attempts


async def _walk(
    scope: Any, target: Target, expected_tag: str | None
) -> tuple[Any | None, list[Attempt]]:
    """One pass down the ladder. Returns the winner, or None with the attempts that failed."""
    attempts: list[Attempt] = []

    for candidate in target.candidates:
        locator, how = build_locator(scope, candidate)
        count = await locator.count()

        if count > 1:
            attempts.append(
                Attempt(candidate.kind, how, matched=count, rejected=f"ambiguous: matched {count}")
            )
            raise TargetAmbiguous(
                f"{how!r} matched {count} elements; the artifact cannot tell them apart",
                attempts,
            )

        if count == 0:
            attempts.append(Attempt(candidate.kind, how, matched=0, rejected="matched nothing"))
            continue

        if expected_tag:
            # A unique match on the wrong element is precisely the failure a bundle exists to
            # prevent, so the one thing discovery recorded about the element's identity is checked.
            #
            # Only the tag. `evidence.expected_role` is NOT asserted: it was read from the
            # accessibility tree at the point the coordinate landed, while the tag describes the
            # control the probe retargeted to, and on `results-panel` they legitimately disagree
            # (role `cell`, tag `tr`). A `role` candidate is self-verifying anyway — `get_by_role`
            # already filtered on it.
            actual = (await locator.evaluate("el => el.tagName.toLowerCase()")) or ""
            if actual != expected_tag:
                attempts.append(
                    Attempt(
                        candidate.kind,
                        how,
                        matched=1,
                        rejected=f"resolved a <{actual}>, expected <{expected_tag}>",
                    )
                )
                continue

        attempts.append(Attempt(candidate.kind, how, matched=1))
        return locator, attempts

    return None, attempts
