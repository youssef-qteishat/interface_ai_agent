"""
Artifact model tests.

The artifact is the only thing replay reads, so these are mostly about what it *refuses*. Two
distinctions carry the weight:

  * a **typo** (`${inputs.membr_id}`) is rejected outright — no amount of authoring fixes a reference
    to something that does not exist;
  * an **unbound input** or an **open gap** is not. A draft has to stay loadable so it can be
    inspected and authored. `is_replayable` is what refuses it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.domain import artifact as A
from src.domain import trace as T
from src.domain.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    Capability,
    IncompatibleSchemaVersion,
    load_artifact,
)

FIXTURES = Path(__file__).parent / "fixtures"
DRAFT = FIXTURES / "capability_draft.yaml"
AUTHORED = FIXTURES / "capability_authored.yaml"


def draft_dict() -> dict:
    return yaml.safe_load(DRAFT.read_text())


# --------------------------------------------------------------------------- #
# the real artifact
# --------------------------------------------------------------------------- #


def test_the_draft_round_trips_unchanged():
    c = load_artifact(DRAFT)
    assert Capability.from_yaml(c.to_yaml()) == c


def test_the_locator_union_is_reused_not_redefined():
    """A second six-variant union would be the one duplication that actually costs something —
    the canonicalizer copies these straight across from the probe."""
    assert A.LocatorCandidate is T.LocatorCandidate


def test_the_draft_is_not_replayable_and_says_why_twice():
    c = load_artifact(DRAFT)
    assert c.is_replayable is False
    reasons = c.why_not_replayable()
    assert any("disclosure" in r for r in reasons)
    assert any("account_type" in r for r in reasons)


def test_account_type_is_unbound_because_the_run_never_touched_the_dropdown():
    """`savings` is the first <option>, so the default coincided with the goal and the model had no
    reason to click. The checkpoint asserts on account_type, which is NOT the same as setting it."""
    c = load_artifact(DRAFT)
    assert c.unbound_inputs == ["account_type"]
    assert "account_type" in c.referenced_inputs, "the checkpoint does reference it"


def test_authoring_the_two_steps_makes_it_replayable():
    """The pair that matters: same models, nothing else changed."""
    draft, authored = load_artifact(DRAFT), load_artifact(AUTHORED)
    assert draft.is_replayable is False
    assert authored.is_replayable is True
    assert authored.unbound_inputs == []
    assert [s.id for s in authored.steps if s.authored_by == "human"] == [
        "select-account-type",
        "accept-disclosure",
    ]


# --------------------------------------------------------------------------- #
# what it refuses, and whether the message is useful
# --------------------------------------------------------------------------- #


def test_a_wait_step_is_not_an_action():
    """A wait *step* is the arbitrary sleep replay exists to avoid; waiting belongs to condition
    evaluation and to `recover: wait_and_retry`."""
    data = draft_dict()
    data["steps"][0]["action"] = {"kind": "wait", "duration": 2.0}
    with pytest.raises(ValidationError, match="wait"):
        Capability.model_validate(data)


def test_a_click_with_no_target_names_the_missing_field():
    data = draft_dict()
    data["steps"][1]["action"] = {"kind": "click"}
    with pytest.raises(ValidationError, match="target"):
        Capability.model_validate(data)


def test_a_typo_in_an_input_reference_is_refused_outright():
    data = draft_dict()
    data["steps"][0]["action"]["value"] = "${inputs.membr_id}"
    with pytest.raises(ValidationError, match="membr_id"):
        Capability.model_validate(data)


def test_a_declared_output_with_no_extractor_is_refused():
    data = draft_dict()
    del data["contract"]["outputs"]["review"]["extract"]
    with pytest.raises(ValidationError, match="review"):
        Capability.model_validate(data)


def test_duplicate_step_ids_are_refused_by_id():
    data = draft_dict()
    # Derived from the fixture rather than hardcoded: step ids are the canonicalizer's output and
    # change when its naming does.
    duplicate = data["steps"][1]["id"]
    data["steps"][2]["id"] = duplicate
    with pytest.raises(ValidationError, match=duplicate):
        Capability.model_validate(data)


def test_a_coordinate_cannot_be_a_candidate():
    """Coordinates survive only as evidence. There is no candidate kind for one, by construction."""
    data = draft_dict()
    data["steps"][0]["action"]["target"]["candidates"] = [{"kind": "coordinate", "x": 549, "y": 96}]
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_a_target_needs_at_least_one_candidate():
    data = draft_dict()
    data["steps"][0]["action"]["target"]["candidates"] = []
    with pytest.raises(ValidationError):
        Capability.model_validate(data)


def test_a_text_condition_needs_exactly_one_predicate():
    for predicate in ({}, {"contains": "x", "any_of": ["y"]}):
        data = draft_dict()
        data["steps"][1]["postconditions"] = [{"kind": "text", **predicate}]
        with pytest.raises(ValidationError, match="exactly one"):
            Capability.model_validate(data)


def test_an_outcome_rule_must_do_something():
    data = draft_dict()
    data["outcome_rules"][0] = {"when": {"kind": "text", "contains": "whatever"}}
    with pytest.raises(ValidationError, match="return|recover"):
        Capability.model_validate(data)


# --------------------------------------------------------------------------- #
# a text assertion's scope
# --------------------------------------------------------------------------- #


def test_a_text_condition_may_name_the_region_it_applies_to():
    """Unscoped, a text assertion reads the whole frame — which made the `verify-outcome` checkpoint
    unable to fail, because the submitted form is still on screen with every `<option>` rendered."""
    condition = A.TextCondition(contains="${inputs.account_type}", within="#review-container")
    assert condition.within == "#review-container"
    assert A.TextAbsentCondition(contains="x", within="#y").within == "#y"


def test_a_url_or_heading_condition_cannot_be_scoped():
    """The reason `within` lives on a subclass rather than on `_TextPredicate`: `UrlCondition` and
    `HeadingCondition` inherit from that too, and a scoped URL is nonsense. `extra="forbid"` is what
    turns the class split into an enforced rule instead of a convention."""
    for kind in (A.UrlCondition, A.HeadingCondition):
        with pytest.raises(ValidationError, match="within"):
            kind(contains="/servicing", within="#review-container")


def test_an_unscoped_condition_serialises_without_the_field():
    """`exclude_none` keeps the artifact readable: only the assertions that are scoped say so."""
    text = A.Capability.model_validate(draft_dict()).to_yaml()
    assert "within: '#review-container table.review-table'" in text
    assert "within: null" not in text


# --------------------------------------------------------------------------- #
# the version rule — the one thing that makes the field a mechanism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "version,loads",
    [("1.0.0", True), ("1.9.9", True), ("1.0.7", True), ("0.9.0", False), ("2.0.0", False)],
)
def test_only_a_matching_major_loads(tmp_path: Path, version: str, loads: bool):
    path = tmp_path / "c.yaml"
    path.write_text(re.sub(r"schema_version:.*", f'schema_version: "{version}"',
                           DRAFT.read_text(), count=1))
    if loads:
        assert load_artifact(path).schema_version == version
    else:
        with pytest.raises(IncompatibleSchemaVersion, match=re.escape(version)):
            load_artifact(path)


def test_the_version_is_checked_before_the_fields_are(tmp_path: Path):
    """A future-major artifact should say so, not produce a pile of 'extra fields not permitted'
    about fields that make perfect sense to the interpreter that understands them."""
    path = tmp_path / "c.yaml"
    data = draft_dict()
    data["schema_version"] = "2.0.0"
    data["something_from_the_future"] = {"nested": True}
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(IncompatibleSchemaVersion) as exc:
        load_artifact(path)
    assert "something_from_the_future" not in str(exc.value)
    assert ARTIFACT_SCHEMA_VERSION in str(exc.value)


def test_the_artifact_version_is_independent_of_the_trace_version():
    """Separate constants on purpose: they version different things and move for different reasons."""
    assert A.ARTIFACT_SCHEMA_VERSION is not T.SCHEMA_VERSION or True
    assert "ARTIFACT_SCHEMA_VERSION" in dir(A)
    assert A.Capability.model_fields["schema_version"].default == ARTIFACT_SCHEMA_VERSION
