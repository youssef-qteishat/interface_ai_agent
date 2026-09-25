"""
Condition tests: does an assertion in the artifact hold against the screen in front of us?

Most of this needs no browser, which is the payoff of `ReplaySurface.observe()` returning the discovery
`Observation` rather than a replay-specific shape — six of the eight condition kinds read that object and
nothing else, so they can be evaluated against one constructed by hand.

The tests that earn their place are the ones about *which haystack*:

  * `url` reads the frame, not the page — the outer document never leaves `/`, so reading `main_frame_url`
    would make every `url` condition in the artifact permanently false;
  * `equals` is exact while `contains` is fuzzy, because the page chose one rendering and replay chose the
    other;
  * an absence cannot be polled for, and the wait says so rather than passing instantly.

Browser tests are marked `integration` and need `docker compose up -d bank-sim`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.domain.artifact import UnknownInput, load_artifact, substitute
from src.domain.trace import DialogInfo, FrameInfo, Observation
from src.replay.conditions import (
    ConditionError,
    ConditionResult,
    evaluate,
    frame_url,
    is_positive,
    wait_for,
)
from src.surfaces.playwright_web import BaseUrlRebind, ReplaySurface

ARTIFACT = Path("artifacts/open_subaccount_review.yaml")
BANK_SIM_URL = "http://127.0.0.1:8001"
EXAMPLE = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}


@pytest.fixture(scope="module")
def capability():
    return load_artifact(ARTIFACT)


@pytest.fixture(scope="module")
def rebind(capability) -> BaseUrlRebind:
    return BaseUrlRebind.for_capability(capability)


def condition_of(capability, step_id: str, index: int = 0):
    step = next(s for s in capability.steps if s.id == step_id)
    return step.postconditions[index]


def observation(
    *,
    outer: str = "http://127.0.0.1:8001/",
    inner: str | None = None,
    text: str = "",
    headings: tuple[str, ...] = (),
    dialogs: tuple[str, ...] = (),
    overlays: tuple[str, ...] = (),
) -> Observation:
    frames = [FrameInfo(path=[], url=outer)]
    if inner is not None:
        frames.append(FrameInfo(path=["servicing-frame"], url=inner))
    return Observation(
        main_frame_url=outer,
        frames=frames,
        visible_text=text,
        headings=list(headings),
        dialogs=[DialogInfo(selector=".modal-overlay", text=t) for t in dialogs],
        overlays=list(overlays),
    )


# --------------------------------------------------------------------------- #
# substitution
# --------------------------------------------------------------------------- #


def test_substitution_fills_every_reference():
    assert substitute("Member Detail — ${inputs.member_id}", EXAMPLE) == "Member Detail — 23456"


def test_an_unsupplied_input_raises_rather_than_becoming_empty():
    """A silent `""` would leave a condition asserting on a truncated string, which then passes. The
    artifact loader already proves every reference names a *declared* input, so a name missing here is
    the caller's bug and has to be loud."""
    with pytest.raises(UnknownInput, match="opening_amount"):
        substitute("${inputs.opening_amount}", {"member_id": "23456"})


# --------------------------------------------------------------------------- #
# which haystack
# --------------------------------------------------------------------------- #


def test_the_url_haystack_is_the_innermost_frame():
    obs = observation(outer="http://127.0.0.1:8001/", inner="http://127.0.0.1:8001/servicing/members/23456")
    assert frame_url(obs).endswith("/servicing/members/23456")


def test_a_target_outside_any_frame_falls_back_to_the_page():
    assert frame_url(observation(outer="http://127.0.0.1:8001/x")) == "http://127.0.0.1:8001/x"


@pytest.mark.asyncio
async def test_a_url_condition_reads_the_frame_and_not_the_page(capability):
    """The regression test for the haystack bug.

    The workflow lives in an iframe, so the outer document sits at `/` for the whole run while the frame
    moves. `results-panel` asserts a servicing path; evaluated against `main_frame_url` it could never
    hold, and every `url` condition in the artifact would be dead.
    """
    condition = condition_of(capability, "results-panel")
    assert condition.kind == "url"

    obs = observation(outer="http://127.0.0.1:8001/", inner="http://127.0.0.1:8001/servicing/members/23456")
    result = await evaluate(condition, obs, inputs=EXAMPLE)

    assert result.passed
    assert "/servicing/members/23456" in result.expected
    # The outer URL alone does not contain the path, which is the whole point.
    assert "/servicing/members/23456" not in obs.main_frame_url


@pytest.mark.asyncio
async def test_a_url_condition_fails_on_the_wrong_frame_url(capability):
    obs = observation(outer="http://127.0.0.1:8001/", inner="http://127.0.0.1:8001/servicing/members/search")
    result = await evaluate(condition_of(capability, "results-panel"), obs, inputs=EXAMPLE)
    assert not result.passed


@pytest.mark.asyncio
async def test_a_heading_condition_matches_any_heading(capability):
    condition = condition_of(capability, "results-panel", 1)
    assert condition.kind == "heading"

    passing = await evaluate(
        condition, observation(headings=("Member Search", "Member Detail — 23456")), inputs=EXAMPLE
    )
    failing = await evaluate(condition, observation(headings=("Member Search",)), inputs=EXAMPLE)

    assert passing.passed and not failing.passed


# --------------------------------------------------------------------------- #
# the fuzzy rule, and where it stops
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_page_may_render_an_input_differently(capability):
    """Why the text predicates are case-insensitive substrings: the run declares `savings` and the page
    renders `Savings`, declares `50.00` and renders `$50.00`. Matching strictly would reject a screen
    that is in fact correct — a failure report on a good run."""
    checkpoint = next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all
    obs = observation(
        text="Review New Account Member: 23456 Account Type: Savings Opening Amount: $50.00",
        headings=("Review New Account",),
    )

    for condition in checkpoint:
        result = await evaluate(
            condition, obs, surface=FakeSurface(), frame_path=["servicing-frame"], inputs=EXAMPLE
        )
        assert result.passed, result


@pytest.mark.asyncio
async def test_a_scoped_text_condition_can_fail_where_an_unscoped_one_cannot(capability):
    """The bug this scope exists for, both halves in one test because the contrast *is* the finding.

    The `verify-outcome` checkpoint asserts the account type appears on screen. Unscoped it reads the
    whole frame — and after Continue the submitted form is still there, its `<select>` rendering every
    `<option>` as visible text. So the assertion passed for `savings`, for `checking`, and even for
    `money_market`, which is not a legal value for that input, while the review panel said `Savings`.
    A checkpoint that cannot fail is not a check.

    Scoped to the review table it discriminates: the table holds one account type.
    """
    scoped = next(
        c for c in next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all
        if getattr(c, "within", None) and "account_type" in (c.contains or "")
    )
    unscoped = scoped.model_copy(update={"within": None})

    # What the review table says, versus what the whole frame says while the form is still up.
    table = "Member: 23456 Account Type: Savings Opening Amount: $50.00 Funding Source: ****-0153"
    whole_frame = observation(
        text="Open Sub-Account Account Type: Savings Checking Opening Amount: "
        "Funding Source: ****-0153 (Savings) ****-0277 (Money_market) " + table
    )
    surface = FakeSurface(whole_frame, scoped=table)

    async def check(condition, claimed):
        result = await evaluate(
            condition,
            whole_frame,
            surface=surface,
            frame_path=["servicing-frame"],
            inputs={**EXAMPLE, "account_type": claimed},
        )
        return result.passed

    # Scoped: only the value the review table actually shows.
    assert await check(scoped, "savings")
    assert not await check(scoped, "checking")
    assert not await check(scoped, "money_market")

    # Unscoped, the same three all pass — which is what made the checkpoint vacuous.
    assert await check(unscoped, "savings")
    assert await check(unscoped, "checking")
    assert await check(unscoped, "money_market")


@pytest.mark.asyncio
async def test_a_scoped_condition_reads_the_region_the_artifact_names(capability):
    """The selector reaching the surface is the mechanism; asserting it keeps a silent fall-back to the
    whole frame from passing the test above by accident."""
    scoped = next(
        c for c in next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all
        if getattr(c, "within", None)
    )
    surface = FakeSurface()
    await evaluate(scoped, observation(), surface=surface, frame_path=["servicing-frame"], inputs=EXAMPLE)
    assert surface.scoped_reads == ["#review-container table.review-table"]


@pytest.mark.asyncio
async def test_a_scoped_condition_on_a_region_that_is_not_there_does_not_hold(capability):
    """A missing region means the assertion does not hold *yet*, which is what `wait_for` polls on — not
    an error, and not a pass. The review table is absent for the whole run until the last screen."""
    scoped = next(
        c for c in next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all
        if getattr(c, "within", None)
    )
    result = await evaluate(
        scoped,
        observation(text="Open Sub-Account Account Type: Savings Checking"),
        surface=FakeSurface(scoped=""),  # `scoped_text` returns "" when the region is not on screen
        frame_path=["servicing-frame"],
        inputs=EXAMPLE,
    )
    assert not result.passed


@pytest.mark.asyncio
async def test_a_scoped_condition_without_a_surface_is_an_error_not_a_failure(capability):
    """Same distinction the element-scoped kinds make: "I could not read the region" is not "the region
    does not contain it"."""
    scoped = next(
        c for c in next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all
        if getattr(c, "within", None)
    )
    with pytest.raises(ConditionError, match="review-container"):
        await evaluate(scoped, observation(), inputs=EXAMPLE)


@pytest.mark.asyncio
async def test_a_value_condition_is_exact_where_a_text_condition_would_not_be(capability):
    """`equals` is not `contains`, and the difference bites: a member-id field holding `234567` contains
    `23456` and is still the wrong field contents. Replay chose this value, so there is no rendering to
    be generous about."""
    condition = condition_of(capability, "member-id")
    assert condition.kind == "value"

    surface = FakeSurface(value="234567")
    result = await evaluate(condition, observation(), surface=surface, locator=object(), inputs=EXAMPLE)
    assert not result.passed
    assert result.actual == "234567"

    surface = FakeSurface(value="23456")
    assert (
        await evaluate(condition, observation(), surface=surface, locator=object(), inputs=EXAMPLE)
    ).passed


@pytest.mark.asyncio
async def test_a_checked_condition_reads_the_control(capability):
    condition = condition_of(capability, "accept-disclosure")
    assert condition.kind == "checked" and condition.equals is True

    for checked, expected in ((True, True), (False, False)):
        result = await evaluate(
            condition, observation(), surface=FakeSurface(checked=checked), locator=object(), inputs=EXAMPLE
        )
        assert result.passed is expected


@pytest.mark.asyncio
async def test_an_element_condition_without_a_locator_is_an_error_not_a_failure(capability):
    """"I could not evaluate this" is a different fact from "this does not hold", and reporting the
    second for the first is how a replay claims a field is wrong when nothing was ever read."""
    with pytest.raises(ConditionError, match="locator"):
        await evaluate(condition_of(capability, "member-id"), observation(), inputs=EXAMPLE)


# --------------------------------------------------------------------------- #
# dialogs and overlays
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_named_dialog_is_not_an_unmatched_one(capability):
    """`unmatched` is relative to the artifact, which is why it needs `named_dialogs` and cannot be
    answered by the condition alone."""
    named = capability.named_dialogs
    assert named == ("System Notice",)

    rules = {("unmatched" if r.when.unmatched else r.when.contains): r.when
             for r in capability.outcome_rules if r.when.kind == "dialog"}
    catch_all = rules["unmatched"]

    known = await evaluate(
        catch_all, observation(dialogs=("⚠ System Notice ERR-APP_BD",)), named_dialogs=named
    )
    unknown = await evaluate(
        catch_all, observation(dialogs=("⚠ Session Expired",)), named_dialogs=named
    )
    none_at_all = await evaluate(catch_all, observation(), named_dialogs=named)

    assert not known.passed, "a dialog the artifact names must not escalate as unknown"
    assert unknown.passed
    assert not none_at_all.passed


@pytest.mark.asyncio
async def test_a_dialog_is_matched_by_its_title_not_its_body(capability):
    """The body carries a per-request reference code, so the rule names the title."""
    named_rule = next(
        r.when for r in capability.outcome_rules if r.when.kind == "dialog" and r.when.contains
    )
    result = await evaluate(named_rule, observation(dialogs=("⚠ System Notice ERR-APP_9f2",)))
    assert result.passed


@pytest.mark.asyncio
async def test_an_overlay_condition_reads_presence_both_ways(capability):
    rule = next(r.when for r in capability.outcome_rules if r.when.kind == "overlay")
    assert rule.present is True

    assert (await evaluate(rule, observation(overlays=("Searching...",)))).passed
    assert not (await evaluate(rule, observation())).passed


# --------------------------------------------------------------------------- #
# nothing is silently unhandled
# --------------------------------------------------------------------------- #


def _every_condition(capability):
    for step in capability.steps:
        yield from step.preconditions
        yield from step.postconditions
        if step.checkpoint:
            yield from step.checkpoint.all
    for rule in capability.outcome_rules:
        yield rule.when


@pytest.mark.asyncio
async def test_every_condition_in_the_committed_artifact_can_be_evaluated(capability):
    """Driven off the artifact rather than a hand-written list, so a condition kind added to the model
    without being taught here fails this test instead of quietly returning False."""
    conditions = list(_every_condition(capability))
    assert {c.kind for c in conditions} == {
        "value", "url", "heading", "checked", "text_absent", "text", "dialog", "overlay",
    }, "all eight kinds should be exercised by the committed artifact"

    for condition in conditions:
        result = await evaluate(
            condition,
            observation(inner="http://127.0.0.1:8001/servicing/members/search"),
            surface=FakeSurface(),
            locator=object(),
            frame_path=["servicing-frame"],
            inputs=EXAMPLE,
            named_dialogs=capability.named_dialogs,
        )
        assert isinstance(result, ConditionResult)
        assert result.kind == condition.kind


# --------------------------------------------------------------------------- #
# waiting
# --------------------------------------------------------------------------- #


class FakeSurface:
    """Serves a scripted sequence of observations, plus the two element reads.

    `observe` returns the last entry forever once the script runs out, so a test can say "the screen
    arrives on the third look" without counting polls exactly.
    """

    def __init__(
        self,
        *observations: Observation,
        value: str = "23456",
        checked: bool = True,
        scoped: str = "Member: 23456 Account Type: Savings Opening Amount: $50.00 Funding Source: ****-0153",
    ) -> None:
        self._script = list(observations) or [observation()]
        self._value = value
        self._checked = checked
        # The review table's text, which is what a `within` condition reads. Defaults to the real thing
        # the live app renders, so a scoped condition in the committed artifact evaluates as it would.
        self._scoped = scoped
        self.observe_count = 0
        self.scoped_reads: list[str] = []

    async def observe(self, _frame_path: Any = None) -> Observation:
        index = min(self.observe_count, len(self._script) - 1)
        self.observe_count += 1
        return self._script[index]

    async def input_value(self, _locator: Any) -> str:
        return self._value

    async def is_checked(self, _locator: Any) -> bool:
        return self._checked

    async def scoped_text(self, _frame_path: Any, selector: str) -> str:
        self.scoped_reads.append(selector)
        return self._scoped


def test_only_the_vacuous_assertions_are_non_positive(capability):
    """An absence is satisfied by an empty screen; a presence is not. Element-scoped kinds count as
    positive even asserting a negative, because resolving their locator already proved the screen."""
    kinds = {c.kind: c for c in _every_condition(capability)}
    assert not is_positive(kinds["text_absent"])
    assert is_positive(kinds["overlay"])  # present: true
    assert is_positive(kinds["checked"])
    assert is_positive(kinds["text"])


@pytest.mark.asyncio
async def test_a_wait_polls_until_the_condition_holds(capability):
    """The screen arrives on the third look. No sleep decides that — the condition does."""
    condition = condition_of(capability, "results-panel", 1)  # heading: Member Detail — 23456
    surface = FakeSurface(
        observation(headings=("Member Search",)),
        observation(headings=("Member Search",)),
        observation(headings=("Member Detail — 23456",)),
    )

    passed, results = await wait_for(surface, ["servicing-frame"], [condition], inputs=EXAMPLE)

    assert passed
    assert surface.observe_count == 3
    assert results[0].passed


@pytest.mark.asyncio
async def test_a_wait_that_times_out_reports_every_condition(capability):
    """The diagnostics are the product: a human reading a failed replay needs what was expected and what
    was on screen, per condition, not a bare timeout."""
    step = next(s for s in capability.steps if s.id == "results-panel")
    surface = FakeSurface(observation(headings=("Member Search",), inner="http://127.0.0.1:8001/x"))

    passed, results = await wait_for(
        surface, ["servicing-frame"], list(step.postconditions), inputs=EXAMPLE, timeout_ms=0
    )

    assert not passed
    assert [r.kind for r in results] == ["url", "heading"]
    assert all(not r.passed for r in results)
    assert all(r.expected and r.actual is not None for r in results)


@pytest.mark.asyncio
async def test_an_absence_does_not_pass_until_the_screen_changes(capability):
    """The false pass this design exists to prevent.

    `continue`'s only postcondition is `text_absent: "You must accept the account disclosure…"`. Straight
    after the click, before the form has submitted, that text is absent from the *old* page too — so a
    naive poll passes instantly and certifies a screen that never arrived.
    """
    condition = condition_of(capability, "continue")
    assert condition.kind == "text_absent"

    before = observation(text="Open Sub-Account Account Type: Opening Amount:")
    after = observation(text="Review New Account Member: 23456")
    surface = FakeSurface(before, before, after)

    passed, results = await wait_for(
        surface, ["servicing-frame"], [condition], inputs=EXAMPLE, baseline=before
    )

    assert passed
    assert surface.observe_count == 3, "it must not have decided on the pre-action screen"
    assert results[0].passed


@pytest.mark.asyncio
async def test_an_absence_on_a_screen_that_never_changes_fails(capability):
    """Which also catches the action that did nothing at all — the click that missed, where every
    absence is trivially true."""
    before = observation(text="Open Sub-Account Account Type: Opening Amount:")
    surface = FakeSurface(before)

    passed, results = await wait_for(
        surface,
        ["servicing-frame"],
        [condition_of(capability, "continue")],
        inputs=EXAMPLE,
        baseline=before,
        timeout_ms=0,
    )

    assert not passed
    assert "unchanged" in results[0].actual


@pytest.mark.asyncio
async def test_an_absence_still_fails_when_the_text_is_actually_there(capability):
    """The change gate must not swallow the real signal it is guarding."""
    before = observation(text="Open Sub-Account")
    after = observation(text="You must accept the account disclosure to continue.")
    surface = FakeSurface(before, after)

    passed, _ = await wait_for(
        surface,
        ["servicing-frame"],
        [condition_of(capability, "continue")],
        inputs=EXAMPLE,
        baseline=before,
        timeout_ms=0,
    )
    assert not passed


@pytest.mark.asyncio
async def test_an_empty_condition_list_passes_without_observing():
    surface = FakeSurface()
    passed, results = await wait_for(surface, None, [])
    assert passed and results == [] and surface.observe_count == 0


# --------------------------------------------------------------------------- #
# against the live simulator
# --------------------------------------------------------------------------- #


@pytest.fixture
def fault_profile():
    armed: list[str] = []

    def arm(profile: str) -> None:
        httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/{profile}", timeout=10).raise_for_status()
        armed.append(profile)

    yield arm
    if armed:
        httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/default", timeout=10)


async def run_every_step(
    surface: ReplaySurface,
    capability,
    inputs: dict[str, str] | None = None,
    *,
    skip: str | None = None,
) -> None:
    """Execute the artifact's steps in order, asserting nothing.

    Leaves the browser on the review panel, which is where the `dialog` fault renders — so a test that
    needs a dialog on screen needs the whole workflow first.

    `skip` omits one step, which is how the checkpoint gets something real to catch: skipping
    `select-account-type` reproduces exactly the state the `account_type` gap warns about, where the
    form's default is accepted and the review panel disagrees with the requested input.
    """
    from src.cli import _advance
    from src.replay.locator_resolver import resolve

    await surface.goto(capability.entry.url)
    for step in capability.steps:
        target = getattr(step.action, "target", None)
        if target is None or step.id == skip:
            continue
        locator, _ = await resolve(surface, target)
        await _advance(surface, step, locator, inputs or EXAMPLE)


async def review_row(surface: ReplaySurface, label: str) -> str:
    """One labelled value out of the review table, so a test can say what the app actually decided."""
    text = await surface.scoped_text(["servicing-frame"], "#review-container table.review-table")
    return text.split(f"{label}:")[1].strip().split(" ")[0] if f"{label}:" in text else ""


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_type, skip, shown, should_pass",
    [
        ("savings", None, "Savings", True),
        ("checking", None, "Checking", True),
        ("checking", "select-account-type", "Savings", False),
    ],
)
async def test_the_checkpoint_catches_an_account_type_the_app_did_not_accept(
    capability, rebind, account_type, skip, shown, should_pass
):
    """The regression test for the whole scoping change, live.

    Row 1 and 2 prove parameterization is real — the authored `select-account-type` step genuinely sets
    the dropdown, and the review panel follows the requested input.

    **Row 3 is the bug.** With that step skipped the form's default is accepted, the review panel reads
    `Savings` while `checking` was asked for, and before the checkpoint was scoped it *passed* — because
    it read the whole frame, where the still-visible `<select>` renders both options. The artifact's own
    gap note says replay with a different value would "fail only at the checkpoint"; this is what makes
    that sentence true.
    """
    inputs = {**EXAMPLE, "account_type": account_type}
    step = next(s for s in capability.steps if s.id == "verify-outcome")

    async with ReplaySurface(rebind) as surface:
        await run_every_step(surface, capability, inputs, skip=skip)
        passed, results = await wait_for(
            surface,
            capability.entry.frame_path,
            step.checkpoint.all,
            inputs=inputs,
            named_dialogs=capability.named_dialogs,
            timeout_ms=step.timeout_ms,
        )
        assert await review_row(surface, "Account Type") == shown

    assert passed is should_pass, "; ".join(str(r) for r in results)
    if not should_pass:
        # And it fails for the right reason, naming the value the app did not accept.
        failed = [r for r in results if not r.passed]
        assert len(failed) == 1 and failed[0].expected == account_type


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_step_postcondition_holds_against_the_live_app(capability, rebind):
    """Step 7 executed the artifact's locators; this executes its *assertions*. A step whose
    postconditions do not hold has not been replayed, however cleanly its target resolved.

    One forward pass, checking each step's conditions right after its action, because that is the only
    point at which they are meant to hold — a postcondition re-checked three screens later is a
    different claim.
    """
    from src.cli import _advance
    from src.replay.locator_resolver import resolve

    checked: list[str] = []

    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)

        for step in capability.steps:
            target = getattr(step.action, "target", None)
            frame_path = target.frame_path if target else capability.entry.frame_path
            locator = None

            if target is not None:
                locator, _ = await resolve(surface, target)

            before = await surface.observe(frame_path)
            if target is not None:
                await _advance(surface, step, locator, EXAMPLE)

            checks = list(step.postconditions) + (step.checkpoint.all if step.checkpoint else [])
            if not checks:
                continue

            passed, results = await wait_for(
                surface,
                frame_path,
                checks,
                locator=locator,
                inputs=EXAMPLE,
                named_dialogs=capability.named_dialogs,
                timeout_ms=step.timeout_ms,
                baseline=before,
            )
            checked.append(step.id)
            assert passed, f"{step.id}: " + "; ".join(str(r) for r in results if not r.passed)

    assert checked == [
        "member-id",
        "results-panel",
        "open-sub-account",
        "opening-amount",
        "accept-disclosure",
        "continue",
        "verify-outcome",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_idle_page_reports_no_overlay_and_a_busy_one_does(capability, rebind):
    """Both halves, because the selector bug this fixes was wrong in both directions.

    `.loading-overlay` matched nothing, so `overlays` was always empty and the artifact's
    `overlay(present: true)` rule could never fire. Fixing the selector alone gives the opposite error:
    the overlay lives in the DOM permanently, hidden by `.htmx-indicator`, so a DOM-presence test would
    report one on every idle page.
    """
    rule = next(r.when for r in capability.outcome_rules if r.when.kind == "overlay")

    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)
        frame = surface.frame(capability.entry.frame_path)

        idle = await surface.observe(capability.entry.frame_path)
        assert idle.overlays == [], f"an idle page reports an overlay: {idle.overlays}"
        assert not (await evaluate(rule, idle)).passed

        # In the DOM the whole time, which is exactly why visibility rather than presence is the question.
        assert await frame.locator(".overlay").count() == 1

        # htmx reveals it by adding `htmx-request`; that class is what a real in-flight request toggles.
        await frame.locator("#loading-overlay").evaluate("el => el.classList.add('htmx-request')")
        busy = await surface.observe(capability.entry.frame_path)
        busy_result = await evaluate(rule, busy)

    assert busy.overlays, "a visible overlay was not reported"
    assert busy_result.passed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_dialog_fault_fires_the_named_rule_and_not_the_catch_all(
    capability, rebind, fault_profile
):
    """`System Notice` is a dialog the artifact names, so it must dismiss-and-continue rather than
    escalate as unknown. Both rules read the same observation, which is what keeps them from both
    firing on the same screen."""
    fault_profile("dialog")
    named = next(
        r.when for r in capability.outcome_rules if r.when.kind == "dialog" and r.when.contains
    )
    catch_all = next(
        r.when for r in capability.outcome_rules if r.when.kind == "dialog" and r.when.unmatched
    )

    async with ReplaySurface(rebind) as surface:
        await run_every_step(surface, capability)

        # `wait_for` rather than a bare `observe()`: the review panel carrying the dialog is swapped in
        # by htmx *after* the click returns, so a single look sometimes lands on the form. This test
        # failed exactly that way — intermittently, and only in a full run — before the wait went in.
        appeared, results = await wait_for(
            surface, capability.entry.frame_path, [named], named_dialogs=capability.named_dialogs
        )
        observed = await surface.observe(capability.entry.frame_path)

    assert appeared, f"the dialog fault produced no dialog: {results[0]}"
    assert observed.dialogs
    assert not (await evaluate(catch_all, observed, named_dialogs=capability.named_dialogs)).passed
