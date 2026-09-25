"""
How a run ends.

The point of this module is that these four outcomes are *different kinds of thing*
and must stay distinguishable to the caller:

  success          — the goal was reached AND independently verified
  business_outcome — the application gave a legitimate domain answer ("no such member")
  failure          — the automation broke (target unresolved, checkpoint missing, loop)
  escalated        — a human was asked to take over, and the session is parked

Collapsing these into exceptions is the mistake this file exists to prevent: a member
that does not exist is not a crash, and a crash is not a member that does not exist.
Callers switch on `status`; every variant carries `run_id` so a result is traceable
back to its evidence folder on its own.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BusinessOutcomeCode(StrEnum):
    """Legitimate answers from the application. Never retried — retrying a
    not-found member just asks the same question twice."""

    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    MEMBER_INELIGIBLE = "MEMBER_INELIGIBLE"


class FailureCode(StrEnum):
    """The automation could not do its job. Each value names one distinct cause so a
    reader never has to guess which limit fired."""

    # Target resolution (mostly replay-time, defined here so the taxonomy is whole)
    TARGET_NOT_RESOLVED = "TARGET_NOT_RESOLVED"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    # Verification
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    # Bounded execution — the stopping rules of Step 11
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"
    WALL_CLOCK_EXCEEDED = "WALL_CLOCK_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NO_PROGRESS = "NO_PROGRESS"
    INVALID_ACTIONS_EXCEEDED = "INVALID_ACTIONS_EXCEEDED"
    REPEATED_TARGET_FAILURE = "REPEATED_TARGET_FAILURE"
    # A run that keeps needing a human is not making progress either, and a handoff
    # loop would otherwise be bounded only by the wall clock.
    MAX_HANDOFFS_EXCEEDED = "MAX_HANDOFFS_EXCEEDED"
    # Control and policy
    NOT_CONTROL_OWNER = "NOT_CONTROL_OWNER"
    # A human finished or abandoned the run by hand while the loop was parked.
    HUMAN_ENDED_RUN = "HUMAN_ENDED_RUN"
    POLICY_DENIED = "POLICY_DENIED"
    ROUTE_NOT_ALLOWED = "ROUTE_NOT_ALLOWED"
    # Environment
    SURFACE_UNAVAILABLE = "SURFACE_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CANCELLED = "CANCELLED"


class EscalationReason(StrEnum):
    UNKNOWN_DIALOG = "UNKNOWN_DIALOG"
    IRREVERSIBLE_REQUIRES_APPROVAL = "IRREVERSIBLE_REQUIRES_APPROVAL"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    MODEL_REQUESTED = "MODEL_REQUESTED"
    POLICY_ESCALATION = "POLICY_ESCALATION"


class StopReason(StrEnum):
    """Why the loop stopped, independent of how the run is reported. Recorded on every
    result so "succeeded" and "succeeded because the model said so" stay separable."""

    TERMINAL_DECLARATION = "TERMINAL_DECLARATION"
    CHECKPOINT_VERIFIED = "CHECKPOINT_VERIFIED"
    MAX_STEPS = "MAX_STEPS"
    WALL_CLOCK = "WALL_CLOCK"
    BUDGET = "BUDGET"
    NO_PROGRESS = "NO_PROGRESS"
    INVALID_ACTIONS = "INVALID_ACTIONS"
    ESCALATION = "ESCALATION"
    MAX_HANDOFFS = "MAX_HANDOFFS"
    HUMAN_ENDED_RUN = "HUMAN_ENDED_RUN"
    CANCELLED = "CANCELLED"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    stop_reason: StopReason | None = None
    steps_used: int = Field(0, ge=0)


class Success(_Result):
    status: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    # Set only when observed state confirmed the goal, not when the model claimed it.
    checkpoint_verified: bool = False


class BusinessOutcomeResult(_Result):
    status: Literal["business_outcome"] = "business_outcome"
    code: BusinessOutcomeCode
    detail: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Failure(_Result):
    status: Literal["failure"] = "failure"
    code: FailureCode
    step_index: int | None = None
    # Expected vs observed is what makes a failure diagnosable without re-running it.
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)


class Escalated(_Result):
    status: Literal["escalated"] = "escalated"
    intervention_id: str
    reason: EscalationReason
    step_index: int | None = None
    context: str | None = None
    evidence: list[str] = Field(default_factory=list)


RunResult = Annotated[
    Success | BusinessOutcomeResult | Failure | Escalated,
    Field(discriminator="status"),
]
