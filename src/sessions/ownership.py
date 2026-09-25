"""
Who holds the screen.

There is exactly one screen, and two parties that can act on it. The failure this
module exists to prevent is both acting at once — an automation clicking Continue
while a human is mid-correction, each believing it has control. That is not a race
you can test your way out of after the fact; it has to be impossible by construction.

So every transition is **compare-and-set on `control_version`**. A caller says "I
believe control is at version 3, move it to HUMAN"; if control has since moved, the
call fails rather than silently winning. The version is the whole mechanism — an
`owner` field alone would let a stale writer overwrite a newer decision and never
know.

    AUTOMATION --escalate--> HUMAN_PENDING --accept--> HUMAN --resume--> AUTOMATION
                                                            --complete--> COMPLETED
         any live owner --cancel--> CANCELLED

Deliberately small: no locks, no database. A frozen state plus an integer, which is
enough to be correct in-process and to serialize into the audit trail that proves,
afterwards, who held the screen when.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Owner(StrEnum):
    AUTOMATION = "AUTOMATION"
    # Escalated, nobody acting: the automation has stopped but no human has picked it
    # up yet. A real state, not a formality — it is where a run waits, and where an
    # unattended run is found later.
    HUMAN_PENDING = "HUMAN_PENDING"
    HUMAN = "HUMAN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


TERMINAL_OWNERS = frozenset({Owner.COMPLETED, Owner.CANCELLED})

# The only legal moves. Everything absent is refused — notably COMPLETED -> anything,
# because a finished run must not be quietly reopened.
LEGAL_TRANSITIONS: dict[Owner, frozenset[Owner]] = {
    Owner.AUTOMATION: frozenset({Owner.HUMAN_PENDING, Owner.COMPLETED, Owner.CANCELLED}),
    Owner.HUMAN_PENDING: frozenset({Owner.HUMAN, Owner.AUTOMATION, Owner.CANCELLED}),
    Owner.HUMAN: frozenset({Owner.AUTOMATION, Owner.COMPLETED, Owner.CANCELLED}),
    Owner.COMPLETED: frozenset(),
    Owner.CANCELLED: frozenset(),
}


class OwnershipError(Exception):
    """Base for control-transfer refusals."""


class StaleControlVersion(OwnershipError):
    """The caller acted on a version of the world that has moved on.

    This is the important one. It means two parties tried to decide the same thing
    from different starting points, and rather than letting the later writer win by
    accident, the transfer is refused and the caller must re-read.
    """

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"control version {expected} is stale; current version is {actual}")
        self.expected = expected
        self.actual = actual


class IllegalTransition(OwnershipError):
    def __init__(self, source: Owner, target: Owner) -> None:
        super().__init__(f"{source} -> {target} is not a legal control transition")
        self.source = source
        self.target = target


class NotControlOwner(OwnershipError):
    """Something tried to act while another party held the screen.

    Maps to `FailureCode.NOT_CONTROL_OWNER`. Distinct from the barrier: the barrier
    makes a well-behaved loop *wait*, this makes a stray call *fail loudly*.
    """

    def __init__(self, expected: Owner, actual: Owner) -> None:
        super().__init__(f"{expected} does not hold control; {actual} does")
        self.expected = expected
        self.actual = actual


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEntry:
    """One transfer, as it will be read months later by someone asking who did what."""

    at: str
    from_owner: Owner
    to_owner: Owner
    from_version: int
    to_version: int
    operator: str | None = None
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "from": str(self.from_owner),
            "to": str(self.to_owner),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "operator": self.operator,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ControlState:
    """Immutable. A transition returns a NEW state rather than mutating this one, so a
    stale reference can never be mistaken for current — it simply carries an old
    version, which the next compare-and-set will reject."""

    run_id: str
    owner: Owner = Owner.AUTOMATION
    control_version: int = 0
    reason: str | None = None
    operator: str | None = None
    updated_at: str = field(default_factory=_now)
    audit: tuple[AuditEntry, ...] = ()

    # ---- queries ----

    @property
    def is_terminal(self) -> bool:
        return self.owner in TERMINAL_OWNERS

    @property
    def automation_may_act(self) -> bool:
        return self.owner is Owner.AUTOMATION

    def assert_owner(self, expected: Owner = Owner.AUTOMATION) -> None:
        if self.owner is not expected:
            raise NotControlOwner(expected, self.owner)

    # ---- transitions ----

    def transition(
        self,
        target: Owner,
        *,
        expected_version: int | None = None,
        operator: str | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> ControlState:
        """Compare-and-set.

        `expected_version=None` means "I have not read the state" and is accepted only
        for the automation's own escalation, where there is no competing writer by
        definition. Every human-initiated transfer passes the version it saw.
        """
        if expected_version is not None and expected_version != self.control_version:
            raise StaleControlVersion(expected_version, self.control_version)

        if target not in LEGAL_TRANSITIONS[self.owner]:
            raise IllegalTransition(self.owner, target)

        next_version = self.control_version + 1
        entry = AuditEntry(
            at=_now(),
            from_owner=self.owner,
            to_owner=target,
            from_version=self.control_version,
            to_version=next_version,
            operator=operator,
            reason=reason,
            detail=detail,
        )
        return replace(
            self,
            owner=target,
            control_version=next_version,
            # `reason` describes why control left automation; it survives until control
            # returns, so the operator page (and the trace) can still show it.
            reason=reason if reason is not None else self.reason,
            operator=operator if operator is not None else self.operator,
            updated_at=entry.at,
            audit=self.audit + (entry,),
        )

    # ---- serialization (this file IS the audit trail) ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "owner": str(self.owner),
            "control_version": self.control_version,
            "reason": self.reason,
            "operator": self.operator,
            "updated_at": self.updated_at,
            "transitions": [entry.to_dict() for entry in self.audit],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ControlState:
        return cls(
            run_id=data["run_id"],
            owner=Owner(data["owner"]),
            control_version=int(data["control_version"]),
            reason=data.get("reason"),
            operator=data.get("operator"),
            updated_at=data.get("updated_at") or _now(),
            audit=tuple(
                AuditEntry(
                    at=e["at"],
                    from_owner=Owner(e["from"]),
                    to_owner=Owner(e["to"]),
                    from_version=e["from_version"],
                    to_version=e["to_version"],
                    operator=e.get("operator"),
                    reason=e.get("reason"),
                    detail=e.get("detail"),
                )
                for e in data.get("transitions", [])
            ),
        )
