"""
Resolver tests: does a candidate bundle from the committed artifact land on exactly one element,
the right one, and does it say so honestly?

Two halves.

The pure half uses a fake scope, because the two rules that are easiest to get wrong are about
*bookkeeping* rather than about a browser: a unique match on the wrong tag must be refused, and a
fall-through must be caused by the page rather than by how long the walk took to get to that
candidate. The second one is a regression test for a real bug — see
`test_a_candidate_that_arrives_late_still_wins`.

The live half is marked `integration` and needs `docker compose up -d bank-sim`. It is the first time
the artifact is *executed* against the app rather than reasoned about, so it is where the
`contextual_text` translation and the `tenant_b` fall-through are settled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.domain.artifact import Target, load_artifact
from src.domain.results import FailureCode
from src.replay.locator_resolver import (
    Attempt,
    TargetAmbiguous,
    TargetNotResolved,
    _contextual_xpath,
    _xpath_literal,
    resolve,
)
from src.surfaces.playwright_web import BaseUrlRebind, ReplaySurface

ARTIFACT = Path("artifacts/open_subaccount_review.yaml")
BANK_SIM_URL = "http://127.0.0.1:8001"


@pytest.fixture(scope="module")
def capability():
    return load_artifact(ARTIFACT)


@pytest.fixture(scope="module")
def rebind(capability) -> BaseUrlRebind:
    return BaseUrlRebind.for_capability(capability)


def step_of(capability, step_id: str):
    return next(s for s in capability.steps if s.id == step_id)


def target_of(capability, step_id: str) -> Target:
    return step_of(capability, step_id).action.target


# --------------------------------------------------------------------------- #
# a fake scope: counts and tags, scripted per selector
# --------------------------------------------------------------------------- #


class FakeLocator:
    def __init__(self, script: list[Any]) -> None:
        # Each entry is consumed by one `count()`, so a candidate can behave differently on the
        # resolver's two walks. An int is a count; a tuple is (count, tag).
        self._script = script

    def _next(self) -> tuple[int, str]:
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        return entry if isinstance(entry, tuple) else (entry, "input")

    async def count(self) -> int:
        self._pending = self._next()
        return self._pending[0]

    async def evaluate(self, _expression: str) -> str:
        return self._pending[1]


class FakeScope:
    """Answers every candidate kind with a scripted locator, keyed by the description
    `build_locator` produces. Nothing here needs a browser, which is the point: the resolver's
    decisions are its own."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self._locators: dict[str, FakeLocator] = {}

    def _for(self, key: str) -> FakeLocator:
        if key not in self._locators:
            self._locators[key] = FakeLocator(self.script.get(key, [0]))
        return self._locators[key]

    def get_by_role(self, role: str, name: str | None = None) -> FakeLocator:
        return self._for(f"role:{role}/{name}")

    def get_by_label(self, text: str) -> FakeLocator:
        return self._for(f"label:{text}")

    def locator(self, selector: str, has_text: str | None = None) -> FakeLocator:
        return self._for(f"sel:{selector}" if has_text is None else f"sel:{selector}:{has_text}")


class FakeSurface:
    def __init__(self, scope: FakeScope) -> None:
        self.scope = scope

    def frame(self, _frame_path: Any = None) -> FakeScope:
        return self.scope


def fake(script: dict[str, list[Any]]) -> FakeSurface:
    return FakeSurface(FakeScope(script))


# --------------------------------------------------------------------------- #
# the xpath translation, as a string
# --------------------------------------------------------------------------- #


def test_the_contextual_xpath_walks_to_the_nearest_non_empty_preceding_cell():
    """The predicate has to filter *before* `[1]`, or `[1]` picks the immediately preceding cell —
    which on the search row is the input's empty one, and the anchor would never match."""
    xpath = _contextual_xpath("Member ID", "input")
    assert "preceding-sibling::td[normalize-space(.)!=''][1]" in xpath
    assert xpath.endswith("//input")


def test_both_colon_spellings_are_accepted():
    """The probe strips a trailing colon from the anchor; the cell renders `Member ID:`."""
    xpath = _contextual_xpath("Member ID", "input")
    assert "'Member ID'" in xpath
    assert "'Member ID:'" in xpath


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Member ID", "'Member ID'"),
        ("it's", '"it\'s"'),
    ],
)
def test_xpath_literals_are_quoted_for_the_quote_the_value_lacks(value, expected):
    assert _xpath_literal(value) == expected


def test_a_value_with_both_quote_kinds_needs_concat():
    """XPath 1.0 has no escape character, so there is no third quoting option."""
    literal = _xpath_literal('it\'s "x"')
    assert literal.startswith("concat(")
    assert "\"'\"" in literal


# --------------------------------------------------------------------------- #
# the resolver's own rules
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_first_candidate_that_matches_wins(capability):
    target = target_of(capability, "open-sub-account")
    surface = fake({"role:button/Open Sub-Account": [(1, "button")]})

    _, attempts = await resolve(surface, target)

    assert [a.kind for a in attempts] == ["role"]
    assert attempts[-1].matched == 1


@pytest.mark.asyncio
async def test_absence_falls_through_to_the_next_candidate(capability):
    target = target_of(capability, "open-sub-account")
    surface = fake(
        {
            "role:button/Open Sub-Account": [0],
            "sel:button:Open Sub-Account": [(1, "button")],
        }
    )

    _, attempts = await resolve(surface, target)

    assert [(a.kind, a.matched) for a in attempts] == [("role", 0), ("text", 1)]
    assert attempts[0].rejected == "matched nothing"


@pytest.mark.asyncio
async def test_ambiguity_stops_dead_and_does_not_try_the_rest(capability):
    """The page is saying the artifact cannot tell two elements apart. A later candidate that happens
    to match one of them would paper over exactly that, and the run would click something and report
    success."""
    target = target_of(capability, "open-sub-account")
    surface = fake(
        {
            "role:button/Open Sub-Account": [2],
            "sel:button:Open Sub-Account": [(1, "button")],
        }
    )

    with pytest.raises(TargetAmbiguous) as exc:
        await resolve(surface, target)

    assert exc.value.code is FailureCode.TARGET_AMBIGUOUS
    # The log stops at the ambiguous candidate: the later ones were never asked.
    assert [a.kind for a in exc.value.attempts] == ["role"]
    assert "matched 2" in exc.value.attempts[0].rejected


@pytest.mark.asyncio
async def test_a_unique_match_on_the_wrong_tag_is_refused(capability):
    """The failure a bundle exists to prevent. `member-id`'s css candidate is a positional path, so
    a redesign can leave it matching exactly one element that is not an input."""
    target = target_of(capability, "member-id")
    surface = fake(
        {
            "sel:xpath=" + _contextual_xpath("Member ID", "input"): [(1, "td")],
            'sel:input[name="member_id"]': [(1, "input")],
        }
    )

    _, attempts = await resolve(surface, target)

    assert attempts[0].rejected == "resolved a <td>, expected <input>"
    assert attempts[0].matched == 1  # it *did* match — uniquely, and wrongly
    assert attempts[-1].kind == "attribute"


@pytest.mark.asyncio
async def test_exhaustion_names_every_candidate_and_why_it_failed(capability):
    """The message is what a human reads when an app has changed under a committed artifact, so the
    diagnostics are the deliverable, not a detail."""
    target = target_of(capability, "continue")
    surface = fake({})  # nothing matches anything

    with pytest.raises(TargetNotResolved) as exc:
        await resolve(surface, target, timeout_ms=0)

    assert exc.value.code is FailureCode.TARGET_NOT_RESOLVED
    kinds = [a.kind for a in exc.value.attempts]
    assert kinds == [c.kind for c in target.candidates]
    assert all(a.rejected for a in exc.value.attempts)
    assert "4 candidate(s)" in str(exc.value)


@pytest.mark.asyncio
async def test_a_candidate_that_arrives_late_still_wins(capability):
    """The bug this file exists for.

    Walking the ladder costs a round-trip per candidate, so on a page that is still arriving the
    *later* candidates are queried later in wall-clock time. The live probe duly reported that
    `role "Open Sub-Account"` had stopped working and css had saved the step — on a page where all
    three candidates match. A fall-through caused by latency is noise in the one signal that means
    *your primary locator broke*.

    So the first walk only establishes that the screen has arrived; a second walk decides. Here the
    role candidate misses once and matches after, and it must still be the winner with no
    fall-through reported.
    """
    target = target_of(capability, "open-sub-account")
    surface = fake(
        {
            "role:button/Open Sub-Account": [0, (1, "button")],
            "sel:a > button.action-button": [(1, "button")],
        }
    )

    _, attempts = await resolve(surface, target)

    assert [a.kind for a in attempts] == ["role"]
    assert not any(a.rejected for a in attempts)


@pytest.mark.asyncio
async def test_a_genuine_fall_through_survives_the_second_walk(capability):
    """The other side of it: a candidate that misses on *both* walks is really gone, and the
    fall-through is reported. Otherwise the fix above would have hidden every fall-through."""
    target = target_of(capability, "continue")
    surface = fake(
        {
            "role:button/Continue": [0],
            "sel:xpath=" + _contextual_xpath("Back", "button"): [(1, "button")],
        }
    )

    _, attempts = await resolve(surface, target)

    assert [(a.kind, a.matched) for a in attempts] == [("role", 0), ("contextual_text", 1)]


@pytest.mark.asyncio
async def test_a_target_with_no_recorded_tag_skips_the_tag_check(capability):
    """`select-account-type` was authored by a human and carries no `evidence`, so there is nothing
    to check against. A missing expectation is not a failed one."""
    target = target_of(capability, "select-account-type")
    assert target.evidence is None

    _, attempts = await resolve(
        surface := fake({'sel:select[name="account_type"]': [(1, "select")]}), target
    )
    assert surface is not None
    assert attempts[-1].kind == "attribute"


def test_an_attempt_reads_as_one_line():
    assert str(Attempt("role", "button/Continue", matched=0, rejected="matched nothing")) == (
        "role(button/Continue): matched nothing"
    )
    assert str(Attempt("role", "button/Continue", matched=1)) == "role(button/Continue): matched 1"


# --------------------------------------------------------------------------- #
# against the live simulator
# --------------------------------------------------------------------------- #


async def advance(surface: ReplaySurface, capability, until: str) -> Any:
    """Drive the artifact forward and return the locator its `until` step resolved to.

    Each target only exists on the screen its step belongs to — there is no Continue button before
    the form that carries it — so reaching one means executing the ones before it. This is the same
    walk `replay --probe` does; Step 9 owns the real loop, with policy and evidence.
    """
    import re

    example = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}
    await surface.goto(capability.entry.url)

    for step in capability.steps:
        target = getattr(step.action, "target", None)
        if target is None:
            continue
        locator, attempts = await resolve(surface, target)
        if step.id == until:
            return locator, attempts

        value = getattr(step.action, "value", None)
        if value:
            value = re.sub(r"\$\{inputs\.([a-z_]+)\}", lambda m: example[m.group(1)], value)
        action = {
            "click": lambda: surface.click(locator),
            "fill": lambda: surface.fill(locator, value),
            "select": lambda: surface.select(locator, value),
            "check": lambda: surface.check(locator),
        }[step.action.kind]
        await action()

    raise AssertionError(f"never reached step {until!r}")


@pytest.fixture
def fault_profile():
    """Arm a profile for one test and put it back. Host-side, like the CLI: test conditions are set
    by the operator, never by the thing under test."""
    armed: list[str] = []

    def arm(profile: str) -> None:
        httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/{profile}", timeout=10).raise_for_status()
        armed.append(profile)

    yield arm
    if armed:
        httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/default", timeout=10)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_target_in_the_artifact_resolves_on_its_first_candidate(capability, rebind):
    """The artifact executed against the app rather than reasoned about.

    First candidate specifically, not merely *a* candidate: on an unchanged app every fall-through is
    a false one, and a resolver that quietly leans on css would pass a weaker assertion.
    """
    targets = [s.id for s in capability.steps if getattr(s.action, "target", None)]
    assert len(targets) == 8

    async with ReplaySurface(rebind) as surface:
        for step_id in targets:
            _, attempts = await advance(surface, capability, until=step_id)
            won = attempts[-1]
            assert won.kind == target_of(capability, step_id).candidates[0].kind, (
                f"{step_id} fell through to {won.kind}: {[str(a) for a in attempts]}"
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_contextual_anchor_resolves_continue_and_not_back(capability, rebind):
    """The trap. The form's last row is `[button Back] [button Continue]`, so Continue's anchor is
    "Back" — and the obvious translation, filter the row on "Back" then take its buttons, matches
    **both**. Since ambiguity is a hard stop, the obvious version would kill a run the artifact can
    otherwise survive.
    """
    async with ReplaySurface(rebind) as surface:
        locator, _ = await advance(surface, capability, until="continue")
        by_anchor = surface.frame(["servicing-frame"]).locator(
            "xpath=" + _contextual_xpath("Back", "button")
        )
        assert await by_anchor.count() == 1
        assert (await by_anchor.inner_text()).strip() == "Continue"
        assert (await locator.inner_text()).strip() == "Continue"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_identical_back_buttons_are_a_hard_stop(capability, rebind):
    """Member Detail carries two identical `Back` buttons, which is why ranking kept no candidate for
    them at all. Asked to resolve one anyway, the resolver refuses rather than taking the first."""
    detail_back = Target.model_validate(
        {
            "frame_path": ["servicing-frame"],
            "candidates": [{"kind": "role", "role": "button", "name": "Back", "match_count": 2}],
        }
    )

    async with ReplaySurface(rebind) as surface:
        await advance(surface, capability, until="open-sub-account")
        with pytest.raises(TargetAmbiguous) as exc:
            await resolve(surface, detail_back, timeout_ms=1000)

    assert exc.value.code is FailureCode.TARGET_AMBIGUOUS
    assert exc.value.attempts[0].matched == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_ladder_falls_through_when_a_tenant_renames_the_button(
    capability, rebind, fault_profile
):
    """Fall-through demonstrated rather than simulated: a fallback nobody observes is not a fallback.

    `tenant_b` renders the submit button as `Proceed`, leaving `Back` alone. So the recorded
    `role "Continue"` genuinely matches nothing and the contextual anchor carries the step — the
    cross-tenant claim, live.
    """
    fault_profile("tenant_b")

    async with ReplaySurface(rebind) as surface:
        locator, attempts = await advance(surface, capability, until="continue")
        # Inside the context: a locator is bound to the page, not a value read off it.
        assert (await locator.inner_text()).strip() == "Proceed"

    assert [(a.kind, a.matched) for a in attempts] == [("role", 0), ("contextual_text", 1)]
