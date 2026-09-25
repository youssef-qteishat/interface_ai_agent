"""
Recorder tests — what the trace must carry, and what it refuses to be written without.

The controller tests cover *endings*. These cover the artifact: a trace is the only
thing that outlives this layer, and the canonicalizer reads it with no access to the run
that produced it. So every fact it needs has to be in there, and a trace missing one
should fail loudly at the step that dropped it rather than quietly at the end.

Two things carry the most weight:

  * `match_count > 1` survives the whole pipeline. §7 calls it the reason the trace is
    worth having — a locator that matched two elements must be demoted rather than
    silently becoming a flaky one. It is asserted here from a payload captured off the
    running simulator, not a hand-written one.
  * a `human` step carries NO policy decision. Nobody ran the allowlist against what a
    person did with their own hands, and an invented `allow` would make the assertion
    "every step has a policy decision" satisfiable by lying.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domain.actions import HumanIntervention
from src.domain.results import StopReason, Success
from src.domain.trace import (
    Display,
    Observation,
    PolicyDecision,
    ProbeResult,
    ProviderInfo,
    RecordedStep,
    RunTrace,
)
from src.evidence.writer import (
    EvidenceWriter,
    RecorderAssertionError,
    assert_recordable,
)
from src.policy.redaction import Redactor

FIXTURES = Path(__file__).parent / "fixtures"
DECLARED = {"member_id": "12345", "account_type": "savings", "opening_amount": "25.00"}
URL = "http://bank-sim:8001/servicing/members/12345"
PNG = b"\x89PNG\r\n\x1a\nfake"

ALLOW = PolicyDecision(decision="allow", risk="reversible", rule="action_allowlist")


def observation(text: str = "Member Detail", **kw) -> Observation:
    return Observation.model_validate(
        {"main_frame_url": URL, "visible_text": text, "dom_hash": f"dom:{text}", **kw}
    )


def trace() -> RunTrace:
    return RunTrace(
        run_id="run_20260924_120000_abcd",
        goal="Find member 12345 and open their detail page",
        target="http://bank-sim:8001/",
        display=Display(width=1280, height=800),
        provider=ProviderInfo(name="fake", model="none"),
    )


def click(index: int = 0, *, probe: ProbeResult | None = None, **kw) -> RecordedStep:
    defaults = {
        "index": index,
        "action": {"kind": "left_click", "coordinate": [233, 239]},
        "policy": ALLOW,
        "observation_before": observation(),
        "observation_after": observation("after"),
    }
    if probe is not None:
        defaults["probe"] = probe
    else:
        defaults["probe_unavailable"] = "test fixture"
    return RecordedStep.model_validate({**defaults, **kw})


@pytest.fixture
def writer(tmp_path: Path) -> EvidenceWriter:
    return EvidenceWriter(root=tmp_path, redactor=Redactor(DECLARED))


# --------------------------------------------------------------------------- #
# the ambiguity signal, from a real payload
# --------------------------------------------------------------------------- #


def ambiguous_probe() -> ProbeResult:
    """Member Detail's two identical `Back` buttons, captured off the running sim.

    Worth noting that the plan originally named `Continue` as the duplicated control.
    It is not — `Continue` probes as match_count=1. Asserting against a captured payload
    rather than a sketch is how that was found.
    """
    return ProbeResult.model_validate(
        json.loads((FIXTURES / "live_probe_ambiguous.json").read_text())
    )


def test_the_captured_payload_really_is_ambiguous():
    """Guards the fixture itself. If the simulator's markup changes so that `Back` is no
    longer duplicated, every test below would pass while proving nothing."""
    probe = ambiguous_probe()
    assert probe.accessible_name == "Back"
    assert probe.is_ambiguous
    assert [c.match_count for c in probe.candidates] == [2, 2, 2]
    assert probe.best_candidate is None, "nothing here identifies one element"


def test_match_count_survives_redaction_and_the_yaml_round_trip(writer: EvidenceWriter):
    """The full path to disk, not just the model.

    A locator that matched twice must still say so after the redactor has walked it and
    YAML has round-tripped it — otherwise the canonicalizer promotes a flaky locator.
    """
    t = trace()
    t.steps.append(click(probe=ambiguous_probe()))
    writer.write_trace(t)

    reloaded = RunTrace.from_yaml(writer.trace_path.read_text())
    counts = [c.match_count for c in reloaded.steps[0].probe.candidates]
    assert counts == [2, 2, 2]
    assert reloaded.steps[0].probe.is_ambiguous


def test_null_match_count_is_not_rewritten_as_zero(writer: EvidenceWriter):
    """§7's fourth rule. Null means nobody counted; zero means nothing matched, and only
    the second is a reason to reject a locator."""
    probe = ProbeResult.model_validate(
        {
            "coordinate": [1, 1],
            "tag": "button",
            "candidates": [{"kind": "role", "role": "button", "name": "Back"}],
        }
    )
    t = trace()
    t.steps.append(click(probe=probe))
    writer.write_trace(t)

    reloaded = RunTrace.from_yaml(writer.trace_path.read_text())
    assert reloaded.steps[0].probe.candidates[0].match_count is None


# --------------------------------------------------------------------------- #
# the assertion gate
# --------------------------------------------------------------------------- #


def test_an_automation_step_cannot_be_built_without_a_policy_decision():
    """Caught at construction, which is the cheapest place to catch it."""
    with pytest.raises(ValueError, match="policy decision"):
        RecordedStep.model_validate(
            {"index": 0, "action": {"kind": "wait", "duration": 1.0}, "policy": None}
        )


def test_a_step_that_lost_its_policy_decision_later_is_refused_at_write_time():
    """The second line of defence: a step mutated after construction is the only way one
    can reach the writer, and the writer is the last place to stop it."""
    t = trace()
    step = click()
    t.steps.append(step)
    object.__setattr__(step, "policy", None)  # what a bug would do

    with pytest.raises(RecorderAssertionError, match="no policy decision"):
        assert_recordable(t)


def test_a_click_with_neither_probe_nor_a_reason_is_refused():
    """A coordinate step with no locator evidence and no explanation cannot be
    canonicalized, and silence about why is the worst of the three options."""
    t = trace()
    t.steps.append(
        RecordedStep.model_validate(
            {
                "index": 0,
                "action": {"kind": "left_click", "coordinate": [1, 1]},
                "policy": ALLOW,
            }
        )
    )
    with pytest.raises(RecorderAssertionError, match="probe_unavailable"):
        assert_recordable(t)


def test_a_stated_reason_is_enough():
    """The cross-surface argument, as a test: a surface with no accessibility backend
    still produces a valid trace, it just cannot become a web capability."""
    t = trace()
    t.steps.append(
        RecordedStep.model_validate(
            {
                "index": 0,
                "action": {"kind": "left_click", "coordinate": [1, 1]},
                "policy": ALLOW,
                "probe_unavailable": "no_accessibility_backend",
            }
        )
    )
    assert_recordable(t)  # no raise


def test_duplicate_step_indices_are_refused():
    """Two steps at index 3 overwrite each other's screenshots, so the trace would point
    at a picture of the wrong action."""
    t = trace()
    t.steps.extend([click(3), click(3)])
    with pytest.raises(RecorderAssertionError, match="unique and ascending"):
        assert_recordable(t)


def test_out_of_order_step_indices_are_refused():
    t = trace()
    t.steps.extend([click(1), click(0)])
    with pytest.raises(RecorderAssertionError, match="unique and ascending"):
        assert_recordable(t)


def test_a_refused_write_leaves_the_previous_trace_intact(writer: EvidenceWriter):
    """Write-then-rename earns its place here: the assertion fires after a good trace is
    already on disk, and that copy must survive."""
    t = trace()
    t.steps.append(click(0))
    writer.write_trace(t)
    good = writer.trace_path.read_text()

    t.steps.append(click(0))  # duplicate index
    with pytest.raises(RecorderAssertionError):
        writer.write_trace(t)

    assert writer.trace_path.read_text() == good


def test_the_gate_reports_every_problem_at_once():
    """A reader fixing one thing should not have to run it again to find the next."""
    t = trace()
    t.steps.append(
        RecordedStep.model_validate(
            {"index": 5, "action": {"kind": "left_click", "coordinate": [1, 1]},
             "policy": ALLOW}
        )
    )
    t.steps.append(click(2))
    with pytest.raises(RecorderAssertionError) as exc:
        assert_recordable(t)
    assert len(exc.value.problems) == 2


# --------------------------------------------------------------------------- #
# the human as a recorded actor
# --------------------------------------------------------------------------- #


def human_step(index: int = 1, **kw) -> RecordedStep:
    return RecordedStep.model_validate(
        {
            "index": index,
            "actor": "human",
            "action": HumanIntervention(
                intervention_id="int_abc123",
                reason="UNKNOWN_DIALOG",
                operator="teller1",
                context="System Notice: ERR-APP_97",
            ).model_dump(mode="json"),
            "policy": None,
            "observation_before": observation("System Notice"),
            "observation_after": observation(
                "Review New Account", dom_changed=True, new_text=["Review New Account"]
            ),
            "probe_unavailable": "a human acted; no coordinate was proposed",
            **kw,
        }
    )


def test_a_human_step_needs_no_policy_decision():
    step = human_step()
    assert step.policy is None
    assert step.actor == "human"

    # And the gate does not ask for one — with the step actually in the trace, or this
    # would pass by validating nothing.
    t = trace()
    t.steps.extend([click(0), step])
    assert_recordable(t)


def test_a_human_step_must_not_carry_a_policy_decision():
    """The inverse matters more. A fabricated `allow` here would be a claim that the
    policy engine authorized something it never saw."""
    with pytest.raises(ValueError, match="must not carry a policy decision"):
        human_step(policy=ALLOW)


def test_the_model_cannot_propose_a_human_intervention():
    """Unproposable by construction, not by convention: there is no tool schema for it
    and the action union rejects it."""
    import pydantic

    from src.domain.actions import parse_action, terminal_tool_schemas

    with pytest.raises(pydantic.ValidationError):
        parse_action({"kind": "human_intervention", "intervention_id": "x", "reason": "y"})
    assert "human_intervention" not in {s["name"] for s in terminal_tool_schemas()}


def test_a_human_step_round_trips_and_is_findable(writer: EvidenceWriter):
    t = trace()
    t.steps.extend([click(0), human_step(1)])
    writer.write_trace(t)

    reloaded = RunTrace.from_yaml(writer.trace_path.read_text())
    assert reloaded.human_steps() == [1]
    step = reloaded.steps[1]
    assert step.policy is None
    assert step.action["kind"] == "human_intervention"
    assert step.action["operator"] == "teller1"
    assert step.observation_after.new_text == ["Review New Account"]


def test_the_summary_counts_the_humans(writer: EvidenceWriter):
    t = trace()
    t.steps.extend([click(0), human_step(1), click(2)])
    writer.finish(
        t,
        Success(run_id=t.run_id, steps_used=3, stop_reason=StopReason.CHECKPOINT_VERIFIED),
        stop_reason=StopReason.CHECKPOINT_VERIFIED,
    )
    summary = json.loads(writer.summary_path.read_text())
    assert summary["steps_recorded"] == 3
    assert summary["human_steps"] == 1


def test_a_human_steps_event_line_carries_no_policy(writer: EvidenceWriter):
    """The JSONL is what a reviewer greps. It must not imply a decision either."""
    t = trace()
    step = human_step(0)
    t.steps.append(step)
    writer.record_step(step, before_png=PNG, after_png=PNG, trace=t)

    line = json.loads(writer.events_path.read_text().splitlines()[0])
    assert line["actor"] == "human"
    assert line["policy"] is None


# --------------------------------------------------------------------------- #
# the §7 contract as a whole
# --------------------------------------------------------------------------- #


def test_a_recorded_step_carries_every_field_section_7_promises(writer: EvidenceWriter):
    """Read as a checklist against the plan's §7 sketch. Each of these is something the
    canonicalizer or the replay engine reads, with no access to the run that made it."""
    t = trace()
    t.steps.append(
        click(
            7,
            probe=ambiguous_probe(),
            model_reason="probe an intentionally duplicated control",
            timing={"dispatched_ms": 31, "settled_ms": 412},
        )
    )
    writer.write_trace(t)
    step = RunTrace.from_yaml(writer.trace_path.read_text()).steps[0]

    assert step.index == 7
    assert step.actor == "automation"
    assert step.action["kind"] == "left_click"
    assert step.action["coordinate"] == [233, 239]
    assert step.policy.decision == "allow"
    assert step.policy.rule
    assert step.observation_before.main_frame_url
    assert step.observation_before.dom_hash
    assert step.observation_after is not None
    assert step.probe.tag and step.probe.candidates
    assert step.timing.dispatched_ms == 31 and step.timing.settled_ms == 412
    assert step.model_reason


def test_the_example_trace_fixture_still_validates():
    """The §7 sketch, as a file. If the contract drifts, this is what notices."""
    t = RunTrace.from_yaml((FIXTURES / "example_trace.yaml").read_text())
    assert t.steps
    assert_recordable(t)
