"""
Replay surface tests — mostly about the base-URL rebind.

The rebind is the one thing in this layer that can disable a safety control without anything
appearing to go wrong. The artifact declares `http://bank-sim:8001`, the hostname the *agent* saw
inside `sandbox_net`; replay runs on the host, where the same app is `http://127.0.0.1:8001`. Get it
wrong in either tempting direction — an empty allowlist, or allowing both origins — and the origin
check still *runs*, still *passes*, and no longer *checks* anything.

So the tests that matter are the ones that prove the check still bites: an unmapped origin is refused,
and `/dev/` stays denied after the swap.

Browser tests are marked `integration` and need `docker compose up -d bank-sim`. Everything else is
pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain.artifact import load_artifact
from src.domain.trace import Observation
from src.policy.engine import PolicyEngine
from src.surfaces.playwright_web import (
    DEFAULT_BASE_URL,
    BaseUrlRebind,
    RebindError,
    ReplaySurface,
)

ARTIFACT = Path("artifacts/open_subaccount_review.yaml")
ARTIFACT_ORIGIN = "http://bank-sim:8001"
RUNTIME_ORIGIN = "http://127.0.0.1:8001"


@pytest.fixture(scope="module")
def capability():
    return load_artifact(ARTIFACT)


@pytest.fixture(scope="module")
def rebind(capability) -> BaseUrlRebind:
    return BaseUrlRebind.for_capability(capability)


def observation(*urls: str) -> Observation:
    """An observation carrying the given frame URLs, which is all `_check_urls` reads."""
    return Observation(
        main_frame_url=urls[0],
        frames=[{"path": [], "url": u} for u in urls],
    )


# --------------------------------------------------------------------------- #
# the rebind itself
# --------------------------------------------------------------------------- #


def test_it_reads_the_origin_off_the_committed_artifact(rebind):
    assert rebind.artifact_origin == ARTIFACT_ORIGIN
    assert rebind.runtime_origin == RUNTIME_ORIGIN


def test_it_rewrites_the_origin_and_leaves_the_rest_alone(rebind):
    url = f"{ARTIFACT_ORIGIN}/servicing/accounts/open?member_id=12345"
    assert rebind.apply(url) == f"{RUNTIME_ORIGIN}/servicing/accounts/open?member_id=12345"


def test_it_maps_one_way_only(rebind):
    """There is no reverse mapping, on purpose. An earlier version rewrote observed URLs back to the
    artifact's origin so the trace would read nicely — which meant the policy engine was checking a
    rewritten view of the world instead of where the browser actually was."""
    assert not hasattr(rebind, "restore")


def test_a_foreign_url_passes_through_untouched(rebind):
    """The rebind is a mapping, not a rewrite-everything. A URL it does not know is left alone so the
    policy engine sees it exactly as the page reported it."""
    assert rebind.apply("http://evil.example/x") == "http://evil.example/x"


def test_more_than_one_origin_refuses_rather_than_guessing(capability):
    """Picking one of several would be a coin flip with the origin allowlist riding on it."""
    two = capability.model_copy(deep=True)
    two.policy.allowed_origins = [ARTIFACT_ORIGIN, "http://other:9000"]
    with pytest.raises(RebindError, match="exactly one"):
        BaseUrlRebind.for_capability(two)


def test_a_base_url_that_is_not_an_origin_is_refused(capability):
    with pytest.raises(RebindError, match="absolute origin"):
        BaseUrlRebind.for_capability(capability, "127.0.0.1:8001")


def test_a_path_on_the_base_url_is_reduced_to_its_origin(capability):
    r = BaseUrlRebind.for_capability(capability, "http://127.0.0.1:8001/servicing/")
    assert r.runtime_origin == RUNTIME_ORIGIN


# --------------------------------------------------------------------------- #
# what the policy engine is actually given — the part that can go silently wrong
# --------------------------------------------------------------------------- #


def test_policy_gets_the_runtime_origin_and_only_that(rebind):
    """Not the artifact's, and not both. Allowing both would permanently allow an origin that does
    not exist at replay time, which is the check the allowlist is for."""
    assert rebind.policy_origins == (RUNTIME_ORIGIN,)
    assert ARTIFACT_ORIGIN not in rebind.policy_origins


def test_the_rebind_is_why_replay_is_allowed_at_all(rebind):
    """Both halves. Without the rebind the real URL is refused; with it, allowed."""
    live = observation(f"{RUNTIME_ORIGIN}/servicing/members/search")

    without = PolicyEngine(allowed_origins=(ARTIFACT_ORIGIN,)).check_action(
        _read_only(), live
    )
    assert without.decision == "deny"
    assert without.code == "ORIGIN_NOT_ALLOWED"

    with_rebind = PolicyEngine(allowed_origins=rebind.policy_origins).check_action(
        _read_only(), live
    )
    assert with_rebind.decision == "allow"


def test_an_unmapped_origin_is_still_refused_after_rebinding(rebind):
    """The failure mode the whole design note is about: the check must still bite."""
    policy = PolicyEngine(allowed_origins=rebind.policy_origins)
    decision = policy.check_action(
        _read_only(), observation(f"{RUNTIME_ORIGIN}/servicing/", "http://evil.example/steal")
    )
    assert decision.decision == "deny"
    assert decision.code == "ORIGIN_NOT_ALLOWED"


def test_the_dev_route_stays_denied_after_rebinding(rebind):
    """Fault profiles are an operator capability. Swapping the origin must not smuggle in the routes
    that origin serves."""
    policy = PolicyEngine(allowed_origins=rebind.policy_origins)
    decision = policy.check_action(_read_only(), observation(f"{RUNTIME_ORIGIN}/dev/fault-profile"))
    assert decision.decision == "deny"
    assert decision.code == "ROUTE_NOT_ALLOWED"


def _read_only():
    from src.domain.actions import Screenshot

    return Screenshot()


# --------------------------------------------------------------------------- #
# against the live simulator
# --------------------------------------------------------------------------- #

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_workflow_is_only_reachable_inside_the_frame(capability, rebind):
    """`frame_path` is not decoration: the shell holds an iframe and the search page lives in it."""
    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)

        outer = await surface.observe()
        inner = await surface.observe(capability.entry.frame_path)

        assert "Member Search" in inner.headings
        assert "Member Search" not in outer.headings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_observe_returns_something_the_existing_policy_engine_accepts(capability, rebind):
    """The reuse claim, asserted rather than stated: the *same* policy engine runs at replay time,
    unchanged, because replay produces the same `Observation` discovery does."""
    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)
        live = await surface.observe(capability.entry.frame_path)

    assert isinstance(live, Observation)
    decision = PolicyEngine(allowed_origins=rebind.policy_origins).check_action(_read_only(), live)
    assert decision.decision == "allow", decision.detail


@pytest.mark.integration
@pytest.mark.asyncio
async def test_observed_urls_are_where_the_browser_actually_is(capability, rebind):
    """Evidence records reality. The correspondence to the artifact's origin is stated once, as the
    mapping, rather than by editing every URL."""
    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)
        live = await surface.observe(capability.entry.frame_path)

    assert live.main_frame_url.startswith(RUNTIME_ORIGIN)
    inner = next(f.url for f in live.frames if f.path)
    assert inner.startswith(RUNTIME_ORIGIN)
    # The artifact's path assertions are origin-free, so they still match.
    assert capability.entry.expect_url_contains in inner


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_paused_surface_refuses_to_act(capability, rebind):
    """The same second guard the discovery adapter has: ownership is enforced above, but a surface
    that would happily act while parked is a race waiting for a handoff."""
    from src.surfaces.base import SurfaceError

    async with ReplaySurface(rebind) as surface:
        await surface.goto(capability.entry.url)
        await surface.pause()
        with pytest.raises(SurfaceError, match="paused"):
            await surface.observe(capability.entry.frame_path)
        await surface.resume()
        assert await surface.observe(capability.entry.frame_path)
