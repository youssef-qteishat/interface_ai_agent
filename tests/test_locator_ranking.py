"""
Locator ranking — asserted against the probes that actually exist.

Every case here is a real element from a real run. That matters more than usual for this module: the
rules exist because specific controls on this app defeat the obvious locator, and an invented fixture
would let a rule look correct while missing the thing it was written for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.discovery.locator_ranking import LADDER, rank_candidates
from src.domain.trace import ProbeResult, RunTrace

FIXTURES = Path(__file__).parent / "fixtures"
TRACE = Path("evidence/discovery-success/trace.yaml")


@pytest.fixture(scope="module")
def probes() -> dict[int, ProbeResult]:
    trace = RunTrace.from_yaml(TRACE.read_text())
    return {s.index: s.probe for s in trace.steps if s.probe}


def kinds(probe: ProbeResult) -> list[str]:
    return [c.kind for c in rank_candidates(probe)[0]]


def rejections(probe: ProbeResult) -> dict[str, str]:
    return {r.kind: r.reason for r in rank_candidates(probe)[1]}


# --------------------------------------------------------------------------- #
# the four exclusions, each against the element that exhibits it
# --------------------------------------------------------------------------- #


def test_a_placeholder_derived_name_is_excluded(probes):
    """Step 11, the Opening Amount field. Chromium computed `$0.00` from the placeholder — it looks
    authoritative and would match every empty currency field on the page."""
    assert "role" not in kinds(probes[11])
    assert "placeholder" in rejections(probes[11])["role"]


def test_placeholder_beats_null_count_in_the_reason(probes):
    """That same candidate is *also* uncounted. A reviewer wants the specific reason."""
    reason = rejections(probes[11])["role"]
    assert "placeholder" in reason
    assert "match_count: null" not in reason


def test_an_uncounted_candidate_cannot_be_primary(probes):
    """Step 5, the search result row. `match_count: null` means nobody counted — which is a different
    fact from "matched once", and only one of them justifies relying on it."""
    assert "role" not in kinds(probes[5])
    assert "not counted" in rejections(probes[5])["role"]


def test_an_ambiguous_candidate_is_never_kept():
    """Member Detail's two identical `Back` buttons: role, text and css all match 2."""
    probe = ProbeResult.model_validate(json.loads((FIXTURES / "live_probe_ambiguous.json").read_text()))
    kept, rejected = rank_candidates(probe)

    assert kept == [], "nothing on that page identifies one of the two buttons"
    assert {r.kind for r in rejected} == {"role", "text", "css"}
    assert all("match_count: 2" in r.reason for r in rejected)


def test_a_generated_dom_id_is_recorded_though_it_was_never_a_candidate(probes):
    """`LocatorCandidate` has no dom_id variant, so there is nothing to filter — but leaving it out
    silently would make the artifact mute about the most obvious-looking locator on the page."""
    for index, dom_id in ((0, "inp_7676796a"), (11, "amt_d955786b")):
        rejected = rank_candidates(probes[index])[1]
        entry = next(r for r in rejected if r.kind == "dom_id")
        assert entry.value == dom_id
        assert "generated" in entry.reason


def test_a_candidate_that_matched_nothing_is_excluded(probes):
    """No live example in this run, so it is constructed — but from a real probe, mutated."""
    probe = probes[0].model_copy(deep=True)
    probe.candidates[0].match_count = 0
    assert "contextual_text" not in kinds(probe)
    assert "matched nothing" in rejections(probe)["contextual_text"]


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #


def test_the_full_bundle_for_every_probed_step(probes):
    """The whole ladder, in one assertion, against all seven probed steps."""
    assert {i: kinds(p) for i, p in probes.items()} == {
        0: ["contextual_text", "attribute", "css"],
        2: ["role", "contextual_text", "text", "css"],
        5: ["css"],
        8: ["role", "text", "css"],
        11: ["contextual_text", "attribute", "css"],
        13: ["role", "contextual_text", "text", "css"],
        17: ["role", "contextual_text", "text", "css"],
    }


def test_text_sorts_below_attribute_and_contextual_text(probes):
    """probe.js emits `text` first; §3's ladder puts it fifth. A form `name=` is what the server
    reads — a contract — while button text is a label a retheme can change."""
    assert LADDER.index("text") > LADDER.index("attribute")
    assert LADDER.index("text") > LADDER.index("contextual_text")

    emitted = [c.kind for c in probes[2].candidates]
    assert emitted.index("text") < emitted.index("contextual_text"), "probe order"
    assert kinds(probes[2]).index("text") > kinds(probes[2]).index("contextual_text"), "ranked order"


def test_css_is_always_last(probes):
    for probe in probes.values():
        ranked = kinds(probe)
        if "css" in ranked:
            assert ranked[-1] == "css"


def test_equal_ranks_keep_probe_order(probes):
    """The sort is stable, so two candidates of the same kind do not get shuffled."""
    probe = probes[13].model_copy(deep=True)
    extra = probe.candidates[1].model_copy(deep=True)  # a second `text` candidate
    extra.text = "Continue "
    probe.candidates.append(extra)
    ranked = [c for c in rank_candidates(probe)[0] if c.kind == "text"]
    assert [c.text for c in ranked] == ["Continue", "Continue "]


# --------------------------------------------------------------------------- #
# the artifact has to be reviewable
# --------------------------------------------------------------------------- #


def test_every_rejection_explains_itself(probes):
    """A locator that vanishes without explanation is indistinguishable from one that was never
    offered — which is the thing this module exists to prevent."""
    for probe in probes.values():
        for rejection in rank_candidates(probe)[1]:
            assert rejection.reason.strip(), rejection.kind
            assert rejection.value, rejection.kind


def test_nothing_is_both_kept_and_rejected(probes):
    for probe in probes.values():
        kept, rejected = rank_candidates(probe)
        assert len(kept) + len([r for r in rejected if r.kind != "dom_id"]) == len(probe.candidates)


def test_a_coordinate_never_becomes_a_candidate(probes):
    """It is not in the union, so this is structural — asserted anyway, because it is the single
    claim canonicalization rests on."""
    for probe in probes.values():
        assert "coordinate" not in kinds(probe)
