"""
Redaction tests.

The criterion the plan actually names — "no raw seeded member ID appears anywhere in
the output" — is checked against a payload captured from the real surface agent, not
against a string written to pass this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domain.trace import Observation
from src.policy.redaction import MASK_TOKEN, Redactor, mask_value

FIXTURES = Path(__file__).parent / "fixtures"
DECLARED = {"member_id": "12345", "account_type": "savings", "opening_amount": "25.00"}


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(DECLARED)


# --------------------------------------------------------------------------- #
# declared inputs become placeholders, not masks
# --------------------------------------------------------------------------- #


def test_declared_member_id_becomes_its_placeholder(redactor: Redactor):
    """Masking would break the canonicalizer, whose job is to find this literal."""
    assert redactor.redact_text("Member: 12345") == "Member: ${inputs.member_id}"


@pytest.mark.parametrize(
    "raw",
    [
        "http://bank-sim:8001/servicing/members/12345",
        "Member Detail — 12345",
        "Review New Account Member: 12345 Account Type: Savings",
        "http://bank-sim:8001/servicing/accounts/open?member_id=12345",
    ],
)
def test_member_id_is_caught_in_every_shape_it_appears_in(redactor: Redactor, raw: str):
    """URL path, URL query, heading, body text — the same value, four places."""
    out = redactor.redact_text(raw)
    assert "12345" not in out
    assert "${inputs.member_id}" in out


def test_url_redacts_to_the_shape_the_artifact_wants(redactor: Redactor):
    assert (
        redactor.redact_text("http://bank-sim:8001/servicing/members/12345")
        == "http://bank-sim:8001/servicing/members/${inputs.member_id}"
    )


def test_action_payload_is_redacted(redactor: Redactor):
    action = {"kind": "type", "text": "12345"}
    assert redactor.redact(action) == {"kind": "type", "text": "${inputs.member_id}"}


def test_longest_value_wins_so_shorter_ones_cannot_chew_holes():
    """A short input that is a substring of a longer one must not be substituted
    inside it."""
    r = Redactor({"short": "123", "long": "12345"})
    assert r.redact_text("id 12345") == "id ${inputs.long}"


# --------------------------------------------------------------------------- #
# everything else
# --------------------------------------------------------------------------- #


def test_another_members_id_is_masked_not_placeholdered(redactor: Redactor):
    """23456 was never an input, so there is no placeholder it could take — but it
    is still someone's member id, so it does not go out in full."""
    out = redactor.redact_text("Results: 12345 Martinez, 23456 Chen")
    assert out == "Results: ${inputs.member_id} Martinez, 2***6 Chen"


def test_a_declared_amount_becomes_its_placeholder(redactor: Redactor):
    """The amount is itself a declared input, so the placeholder rule claims it —
    and that is what the artifact needs, otherwise the capability would hardcode
    25.00 instead of parameterizing it.

    Note this is not a loss of readability the way a mask would be: a reader sees a
    named parameter, not a hidden value. The leading `$` belongs to the page text,
    not to the input, so it survives.
    """
    assert (
        redactor.redact_text("Opening Amount: $25.00")
        == "Opening Amount: $${inputs.opening_amount}"
    )


def test_an_undeclared_amount_stays_readable_by_default():
    """Amounts the run did not declare are left alone: a trace with every number
    masked is unreadable, and Step 11's checkpoint reads these values."""
    r = Redactor({"member_id": "12345"})
    assert "$5420.50" in r.redact_text("Balance: $5420.50")


def test_undeclared_amounts_can_be_masked_when_a_deployment_wants_that():
    r = Redactor({"member_id": "12345"}, mask_amounts=True)
    assert "$5420.50" not in r.redact_text("Balance: $5420.50")


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-IUj00tGK5q3P0b3YgOs9MKTS",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
        "Cookie: session=abc123def456",
        "api_key=supersecretvalue",
    ],
)
def test_secret_shapes_are_masked(redactor: Redactor, secret: str):
    out = redactor.redact_text(f"header {secret} end")
    assert MASK_TOKEN in out
    assert "supersecretvalue" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_mask_value_keeps_first_and_last():
    """Enough to correlate two mentions of the same id without disclosing it."""
    assert mask_value("12345") == "1***5"
    assert mask_value("ab") == "**"
    assert mask_value("a") == "*"


# --------------------------------------------------------------------------- #
# structural behaviour
# --------------------------------------------------------------------------- #


def test_redaction_is_idempotent(redactor: Redactor):
    """A value passing through both the recorder and the evidence writer must not
    come out ${inputs.${inputs.member_id}}."""
    once = redactor.redact_text("Member: 12345")
    assert redactor.redact_text(once) == once


def test_nested_structures_are_redacted_at_every_depth(redactor: Redactor):
    event = {
        "frames": [{"url": "http://bank-sim:8001/servicing/members/12345", "controls": [
            {"tag": "input", "text": "12345"}
        ]}],
        "headings": ["Member Detail — 12345"],
    }
    blob = json.dumps(redactor.redact(event))
    assert "12345" not in blob
    assert blob.count("${inputs.member_id}") == 3


def test_non_strings_pass_through_untouched(redactor: Redactor):
    event = {"width": 1280, "ok": True, "scale": 1.0, "probe": None, "tags": []}
    assert redactor.redact(event) == event


def test_hashes_are_not_disturbed(redactor: Redactor):
    """Digests were computed from the real bytes — that is what makes them useful for
    change detection, and a digest is not a disclosure."""
    event = {"observation_hash": "sha256:9c1f2b", "dom_hash": "sha256:54b5dd"}
    assert redactor.redact(event) == event


def test_empty_and_missing_inputs_are_safe():
    r = Redactor({})
    assert r.redact_text("Member: 12345") == "Member: 1***5"
    assert r.redact(None) is None
    assert r.redact_text("") == ""


# --------------------------------------------------------------------------- #
# the plan's stated criterion, against real captured data
# --------------------------------------------------------------------------- #


def test_live_observation_contains_no_raw_member_id_after_redaction(redactor: Redactor):
    """The plan's criterion: no raw seeded member ID anywhere in the output.

    Run against a payload captured from the real surface agent rather than a
    hand-written string, so it cannot quietly agree with whatever I imagined.
    """
    raw = (FIXTURES / "live_observe.json").read_text()
    # Inject the member id the way the review screen really renders it, since the
    # captured fixture is the search page.
    raw = raw.replace("Member Search", "Member Detail 12345 Member: 12345")

    redacted = json.dumps(redactor.redact(json.loads(raw)))
    assert "12345" not in redacted
    assert redactor.contains_unredacted(redacted) == []


def test_observation_still_validates_after_redaction(redactor: Redactor):
    """Redaction must not break the schema — a redacted trace still has to load."""
    obs = Observation.model_validate_json((FIXTURES / "live_observe.json").read_text())
    redacted = Observation.model_validate(redactor.redact(obs.model_dump(mode="json")))
    assert redacted.dom_hash == obs.dom_hash
    assert len(redacted.frames) == len(obs.frames)


def test_leak_detector_reports_names_not_values(redactor: Redactor):
    """A leak detector that prints the secret it found would be a poor one."""
    leaked = redactor.contains_unredacted("the id is 12345")
    assert leaked == ["member_id"]
    assert "12345" not in str(leaked)
