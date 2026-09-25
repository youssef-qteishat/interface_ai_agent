"""
Canonicalizer tests — the reduction, and the two gaps.

Twenty-one recorded steps become seven. Most of what goes is noise, but two removals are judgments,
and both are asserted here against the run that forced them:

  * a rejected attempt and its retry are ONE step, and the refusal message survives as the
    postcondition that would catch the same failure again;
  * a human step is never a step. It is either a condition the artifact can describe, or a hole the
    artifact has to admit to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.canonicalizer import (
    CanonicalizationError,
    CapabilitySpec,
    canonicalize,
)
from src.discovery.specs import OPEN_SUBACCOUNT
from src.domain.artifact import InputSpec, load_artifact
from src.domain.trace import RunTrace

TRACE = Path("evidence/discovery-success/trace.yaml")
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def trace() -> RunTrace:
    return RunTrace.from_yaml(TRACE.read_text())


@pytest.fixture(scope="module")
def capability(trace: RunTrace):
    return canonicalize(trace, OPEN_SUBACCOUNT)


def step(capability, step_id):
    return next(s for s in capability.steps if s.id == step_id)


# --------------------------------------------------------------------------- #
# the whole reduction
# --------------------------------------------------------------------------- #


def test_the_output_is_the_committed_draft(trace, capability):
    """The fixture is this function's output, not a transcription of it.

    Hand-writing the expected artifact was how the first two attempts went wrong — a fixture I typed
    cannot check the code that should have produced it.
    """
    assert capability.to_yaml() == load_artifact(FIXTURES / "capability_draft.yaml").to_yaml()


def test_the_checkpoint_asserts_inside_the_region_the_outputs_come_from(capability):
    """The checkpoint's value assertions are scoped, and scoped to where the spec extracts outputs.

    Unscoped they read the whole frame, and the checkpoint could not fail: after Continue the submitted
    form is still on screen with every `<option>` of the account-type `<select>` rendered as visible
    text, so `text: ${inputs.account_type}` matched every value claimed — including `money_market`, which
    is not a legal value for that input.

    The selector is derived from `OutputSpec.extract.scope` rather than authored separately, so "verify
    the values where you read them" stays one statement instead of two that can drift.
    """
    from src.discovery.specs import OPEN_SUBACCOUNT

    expected = OPEN_SUBACCOUNT.outputs["review"].extract.scope.selector
    checkpoint = next(s for s in capability.steps if s.id == "verify-outcome").checkpoint.all

    value_assertions = [c for c in checkpoint if c.kind == "text"]
    assert value_assertions, "the checkpoint should assert on the inputs"
    assert all(c.within == expected for c in value_assertions)

    # The outcome heading sits outside that table, so scoping it would break a sound assertion.
    headings = [c for c in checkpoint if c.kind == "heading"]
    assert headings and all(getattr(c, "within", None) is None for c in headings)


def test_a_spec_with_no_single_output_scope_leaves_the_checkpoint_unscoped(trace):
    """Falling back to the old behaviour rather than guessing which of several outputs verifies the
    outcome. Unscoped is weak, but a scope pointing at the wrong region is wrong."""
    import dataclasses

    from src.discovery.canonicalizer import _verification_scope, canonicalize
    from src.discovery.specs import OPEN_SUBACCOUNT

    two_outputs = dataclasses.replace(
        OPEN_SUBACCOUNT,
        outputs={"review": OPEN_SUBACCOUNT.outputs["review"],
                 "copy": OPEN_SUBACCOUNT.outputs["review"]},
    )
    assert _verification_scope(two_outputs) is None

    checkpoint = next(
        s for s in canonicalize(trace, two_outputs).steps if s.id == "verify-outcome"
    ).checkpoint.all
    assert all(getattr(c, "within", None) is None for c in checkpoint)


def test_twenty_one_trace_steps_become_seven(trace, capability):
    assert len(trace.steps) == 21
    assert [s.id for s in capability.steps] == [
        "member-id",
        "search",
        "results-panel",
        "open-sub-account",
        "opening-amount",
        "continue",
        "verify-outcome",
    ]


def test_no_noise_survives(capability):
    """A `wait` step in particular: replay waits on conditions, never on a clock."""
    assert {s.action.kind for s in capability.steps} == {"fill", "click", "read"}


def test_click_then_type_collapsed_into_one_fill(trace, capability):
    """Trace steps 0 and 1. A `type` carries no probe of its own, so the click is the only source of
    a target for it."""
    fill = step(capability, "member-id")
    assert fill.action.kind == "fill"
    assert fill.action.value == "${inputs.member_id}"
    # The target came from the click, whose probe found the Member ID field.
    assert fill.action.target.evidence.discovery_coordinate == (549, 96)
    assert fill.action.target.evidence.expected_tag == "input"


def test_the_rejected_attempt_and_its_retry_are_one_step(capability):
    """Trace steps 13 and 17 are the same coordinate — the app refused the first one."""
    assert len([s for s in capability.steps if s.id.startswith("continue")]) == 1


def test_the_refusal_message_became_a_postcondition(capability):
    """The evidence is not merely recorded, it is turned into an assertion that would catch the same
    failure on a future run."""
    absent = [c for c in step(capability, "continue").postconditions if c.kind == "text_absent"]
    assert len(absent) == 1
    assert "accept the account disclosure" in absent[0].contains


# --------------------------------------------------------------------------- #
# postconditions — derived narrowly on purpose
# --------------------------------------------------------------------------- #


def test_url_postconditions_come_from_the_inner_frame(capability):
    """`main_frame_url` never changes — the shell holds an iframe and the workflow navigates inside
    it. A URL rule reading the main frame would be true on every screen and assert nothing."""
    url = next(c for c in step(capability, "results-panel").postconditions if c.kind == "url")
    assert url.contains == "/servicing/members/${inputs.member_id}"


def test_a_url_postcondition_is_parameterized(capability):
    """It asserts the right member, not merely that some member page loaded."""
    url = next(c for c in step(capability, "results-panel").postconditions if c.kind == "url")
    assert "${inputs.member_id}" in url.contains


def test_a_new_heading_becomes_a_postcondition(capability):
    heading = next(c for c in step(capability, "open-sub-account").postconditions if c.kind == "heading")
    assert heading.contains == "Open Sub-Account"


def test_a_fill_asserts_its_own_value(capability):
    value = next(c for c in step(capability, "opening-amount").postconditions if c.kind == "value")
    assert value.equals == "${inputs.opening_amount}"


def test_steps_that_changed_nothing_observable_assert_nothing(capability):
    """`search` swaps a results panel via htmx: no navigation, no new heading. Inventing a
    postcondition from the results text would bake this member's name into the artifact."""
    assert step(capability, "search").postconditions == []


# --------------------------------------------------------------------------- #
# the two gaps
# --------------------------------------------------------------------------- #


def test_both_gaps_are_found(capability):
    assert len(capability.gaps) == 2
    assert capability.is_replayable is False


def test_the_invisible_intervention_is_localised_by_the_message_that_caused_it(capability):
    """Trace step 16 records nothing — ticking a checkbox changes no visible text. What names the
    control is step 13's refusal, three steps earlier."""
    gap = next(g for g in capability.gaps if g.detected_from.human_step == 16)
    assert gap.detected_from.trace_step == 13
    assert gap.detected_from.evidence == "You must accept the account disclosure to continue."


def test_that_gap_anchors_before_the_step_that_was_rejected(capability):
    """The fix has to happen earlier than the click it enables. Anchoring to the rejected step would
    tell an author to insert it after the thing it was supposed to make work."""
    gap = next(g for g in capability.gaps if g.detected_from.human_step == 16)
    ids = [s.id for s in capability.steps]
    assert gap.step_after == "opening-amount"
    assert ids.index("opening-amount") < ids.index("continue")


def test_the_unbound_input_gap_has_no_anchor(capability):
    """Nothing in the run touched the dropdown, so nothing says which screen it is on. Guessing a
    position would be worse than admitting the author has to place it."""
    gap = next(g for g in capability.gaps if "account_type" in g.reason)
    assert gap.step_after is None
    assert capability.unbound_inputs == ["account_type"]


def test_the_visible_intervention_is_a_recovery_rule_not_a_gap(capability):
    """Trace step 20's diff shows exactly what left the screen, so it is describable."""
    assert all(g.detected_from.human_step != 20 for g in capability.gaps)
    dismiss = [r for r in capability.outcome_rules if r.recover and r.recover.strategy == "dismiss"]
    assert len(dismiss) == 1
    assert dismiss[0].when.contains == "System Notice"


def test_the_dialog_rule_matches_the_title_not_the_reference_code(capability):
    """The body carries a per-request `ERR-APP_xx`; a rule containing it would fire exactly once,
    ever."""
    dismiss = next(r for r in capability.outcome_rules if r.recover and r.recover.strategy == "dismiss")
    assert "ERR" not in dismiss.when.contains


def test_a_trace_with_no_human_steps_produces_no_intervention_gap(trace):
    clean = trace.model_copy(deep=True)
    clean.steps = [s for s in clean.steps if s.actor != "human"]
    result = canonicalize(clean, OPEN_SUBACCOUNT)
    assert all(g.detected_from.human_step is None for g in result.gaps)


def test_a_spec_whose_inputs_are_all_bound_produces_no_unbound_gap(trace):
    bound_only = CapabilitySpec(
        id=OPEN_SUBACCOUNT.id,
        title=OPEN_SUBACCOUNT.title,
        inputs={k: v for k, v in OPEN_SUBACCOUNT.inputs.items() if k != "account_type"},
        outputs=OPEN_SUBACCOUNT.outputs,
    )
    result = canonicalize(trace, bound_only)
    assert result.unbound_inputs == []
    assert all("account_type" not in g.reason for g in result.gaps)


# --------------------------------------------------------------------------- #
# derived vs authored, and the gate
# --------------------------------------------------------------------------- #


def test_the_procedure_is_derived_and_the_contract_is_not(capability):
    """Everything below came from the trace; the input types and output extractors could not have."""
    assert capability.entry.url == "http://bank-sim:8001/"
    assert capability.entry.frame_path == ["servicing-frame"]
    assert capability.entry.expect_url_contains == "/servicing/members/search"
    assert capability.policy.allowed_origins == ["http://bank-sim:8001"]
    assert capability.provenance.source_run_id.startswith("run_")
    assert capability.provenance.fault_profile == "dialog"
    # From the spec, and underivable: one run cannot say a member id is always five digits.
    assert capability.contract.inputs["member_id"].pattern == r"^[0-9]{5}$"
    assert capability.contract.inputs["account_type"].values == ["savings", "checking"]


def test_the_checkpoint_asserts_the_screen_and_the_inputs(capability):
    checkpoint = step(capability, "verify-outcome").checkpoint
    kinds = [(c.kind, c.contains) for c in checkpoint.all]
    assert ("heading", "Review New Account") in kinds
    assert ("text", "${inputs.account_type}") in kinds
    assert ("text", "${inputs.opening_amount}") in kinds


def test_an_unmatched_dialog_rule_is_always_last(capability):
    """Guessing at a dialog you cannot name is the one case where stopping is better."""
    last = capability.outcome_rules[-1]
    assert last.when.kind == "dialog" and last.when.unmatched is True
    assert last.return_.reason == "UNKNOWN_DIALOG"


def test_nothing_sensitive_or_unstable_reaches_the_artifact(capability):
    blob = capability.to_yaml()
    assert "12345" not in blob
    for step_ in capability.steps:
        target = getattr(step_.action, "target", None)
        for candidate in (target.candidates if target else []):
            selector = getattr(candidate, "selector", "") or ""
            assert "inp_" not in selector and "amt_" not in selector


def test_a_generated_id_in_a_candidate_is_refused(trace):
    """The gate, forced. Rejections may legitimately name a generated id; a candidate may not."""
    from src.discovery.canonicalizer import assert_no_leak
    from src.domain.trace import CssCandidate

    bad = canonicalize(trace, OPEN_SUBACCOUNT)
    # The id the ranker rejected, smuggled back in as a candidate.
    bad.steps[0].action.target.candidates[0] = CssCandidate(selector="#inp_7676796a", match_count=1)
    with pytest.raises(CanonicalizationError, match="generated id"):
        assert_no_leak(bad, set(OPEN_SUBACCOUNT.inputs))

    # And it is still legitimate for a REJECTION to name that same id — that is the record of why it
    # was not used.
    good = canonicalize(trace, OPEN_SUBACCOUNT)
    assert any(
        r.kind == "dom_id" and "inp_" in (r.value or "")
        for r in good.steps[0].action.target.evidence.rejected
    )
    assert_no_leak(good, set(OPEN_SUBACCOUNT.inputs))


def test_a_trace_with_no_executable_steps_is_refused(trace):
    empty = trace.model_copy(deep=True)
    empty.steps = [s for s in empty.steps if s.action.get("kind") in {"wait", "screenshot"}]
    with pytest.raises(CanonicalizationError, match="nothing to replay"):
        canonicalize(empty, OPEN_SUBACCOUNT)
