"""Committed capability specs — the half of an artifact a trace cannot know.

Input types, output extractors, the capability's name and risk classification are judgments about the
domain. A trace can show that `member_id` was typed into a field; it cannot show that the value is
always five digits, or that `account_type` admits exactly two values, or that the review panel's rows
map to these four outputs. Deriving those from one observed run would be overfitting.

Lives beside the canonicalizer rather than in `tests/` because the CLI reads it too.
"""
from src.discovery.canonicalizer import CapabilitySpec
from src.domain.trace import CssCandidate
from src.domain.artifact import (
    Extract, ExtractField, InputSpec, OutcomeReturn, OutcomeRule,
    OutputSpec, Recover, TextCondition, OverlayCondition,
)

OPEN_SUBACCOUNT = CapabilitySpec(
    id="corebank.open_subaccount_review",
    title="Prepare a new sub-account for review",
    risk="reversible",
    # 1.0.1: the `verify-outcome` checkpoint now reads the review table rather than the whole frame, so
    # it can actually fail. Inputs and outputs are unchanged, which is why this is a patch and not a
    # major — while the *format* moved to 1.1.0 for the optional field that made it possible. One change,
    # two version fields, moving by different amounts for different reasons.
    version="1.0.1",
    inputs={
        "member_id": InputSpec(type="string", pattern=r"^[0-9]{5}$", sensitive=True),
        "account_type": InputSpec(type="enum", values=["savings", "checking"]),
        "opening_amount": InputSpec(type="decimal", minimum=0),
    },
    outputs={
        "review": OutputSpec(
            type="object",
            properties={
                "member": {"type": "string", "sensitive": True},
                "account_type": {"type": "string"},
                "opening_amount": {"type": "decimal"},
                "funding_source": {"type": "string", "sensitive": True},
            },
            extract=Extract(
                scope=CssCandidate(selector="#review-container table.review-table"),
                fields={
                    "member": ExtractField(row_label="Member:", transform="mask_member_id"),
                    "account_type": ExtractField(row_label="Account Type:"),
                    "opening_amount": ExtractField(row_label="Opening Amount:", transform="strip_currency"),
                    "funding_source": ExtractField(row_label="Funding Source:"),
                },
            ),
        )
    },
    extra_outcome_rules=(
        OutcomeRule(
            when=TextCondition(contains="No members found"),
            return_=OutcomeReturn(status="business_outcome", code="MEMBER_NOT_FOUND"),
        ),
        OutcomeRule(
            when=OverlayCondition(present=True),
            # 4 x 500ms = 2s, against a fault profile that holds the response for 1200ms
            # (`seed.sql`: fp_overlay). It was 2 x 500ms, which is 1000ms — and a replay under
            # `--fault overlay` escalated every time, 200ms short. A recovery budget has to be sized
            # against the delay it is meant to absorb, not picked as a round number.
            recover=Recover(strategy="wait_and_retry", max_attempts=4, backoff_ms=500),
        ),
    ),
)
