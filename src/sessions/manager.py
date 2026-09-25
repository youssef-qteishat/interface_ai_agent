"""
The pause barrier and the human handshake.

Two processes are involved and that shapes everything here. The discovery loop runs in
one; the human types `session accept` in another. There is no operator UI in this
layer (deferred by design), so they talk through a file:
`evidence/<run_id>/intervention.json`. That file is not a convenience — it *is* the
audit trail the brief asks for, and unlike a shared in-memory object it survives the
process that wrote it, which is the whole point of an audit trail.

Two independent guards, on purpose:

    await manager.barrier()      makes a well-behaved loop WAIT
    manager.assert_automation_owns()   makes a stray call FAIL

A loop that forgot the barrier would still be stopped by the assertion; a call site
that skipped the assertion would still be parked by the barrier. Either alone is a
single point of failure in the one mechanism that must not have one.

Waiting is intentional; hanging is a bug. The barrier blocks indefinitely for a human —
any fixed window would be hostile to a reviewer actually reading the intervention
before deciding — but a deadline can be supplied, so an unattended run ends as
`escalated` with evidence flushed rather than pinning a container forever.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any, Protocol

from src.sessions.ownership import (
    ControlState,
    NotControlOwner,
    Owner,
    StaleControlVersion,
)

DEFAULT_NOVNC_URL = "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"

# How often the parked loop re-reads the handshake file. Short enough that a human
# does not notice the lag, long enough not to spin a core while waiting.
POLL_INTERVAL_S = 0.5


class Pausable(Protocol):
    """The slice of the surface adapter this module touches."""

    async def pause(self) -> None: ...
    async def resume(self) -> None: ...


class InterventionTimeout(Exception):
    """The run's budget expired while parked. Not a control error — the transfer was
    valid, nobody came."""


class RunEndedByHuman(Exception):
    """The operator finished or abandoned the run while the loop was parked.

    Raised instead of waiting, because `COMPLETED` and `CANCELLED` are terminal: no
    transition out of them exists, so a loop that kept polling would poll until its
    deadline and then report a timeout — blaming an absent human for a decision one
    actually made.
    """

    def __init__(self, owner: Any) -> None:
        super().__init__(f"the run was ended by a human ({owner})")
        self.owner = owner


class SessionManager:
    """Owns control for one run.

    The in-memory `ControlState` is authoritative for this process; the JSON file is
    how the other process reads and changes it.
    """

    def __init__(
        self,
        run_id: str,
        *,
        evidence_dir: str | Path | None = None,
        adapter: Pausable | None = None,
        novnc_url: str = DEFAULT_NOVNC_URL,
    ) -> None:
        self.run_id = run_id
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.adapter = adapter
        self.novnc_url = novnc_url

        self.state = ControlState(run_id=run_id)
        self.intervention_id: str | None = None
        self.intervention_reason: str | None = None
        # Held on the manager, not passed per-write: the operator's context is the
        # thing they most need, and an early version rewrote the file on every
        # transition — so accepting the intervention erased the reason for it.
        self.intervention_step_index: int | None = None
        self.intervention_context: str | None = None

        # Set == automation may proceed. Starts set: a run begins in control.
        self._gate = asyncio.Event()
        self._gate.set()

    # ---- the two guards ----

    async def barrier(self, *, deadline: float | None = None) -> None:
        """Called before every action. Instant while automation holds control.

        While parked it polls the handshake file, so a `session resume` typed in
        another terminal releases this loop.
        """
        if self._gate.is_set():
            return

        while not self._gate.is_set():
            if deadline is not None and time.monotonic() > deadline:
                raise InterventionTimeout(
                    f"run budget expired while waiting for a human "
                    f"(intervention {self.intervention_id})"
                )
            self._reload_from_disk()
            if self.state.automation_may_act:
                # Un-pause the adapter HERE, not only in `resume()`.
                #
                # `resume()` runs in the operator's process, where `adapter` is None —
                # the CLI loads control state from disk and has no surface. Only this
                # process holds the adapter that `escalate()` paused, so only this
                # process can release it. Without this line the barrier opened, the loop
                # carried on, and every action came back
                # "surface is paused; automation does not hold control" until the
                # no-progress rule stopped the run. The guard was right; nothing was
                # clearing it.
                if self.adapter is not None:
                    await self.adapter.resume()
                self._gate.set()
                break
            if self.state.is_terminal:
                # `complete` and `cancel` are ends, not pauses. Waiting for a transition
                # out of them would wait forever.
                raise RunEndedByHuman(self.state.owner)
            await asyncio.sleep(POLL_INTERVAL_S)

    def assert_automation_owns(self) -> None:
        """Raises `NotControlOwner` if anyone else holds the screen."""
        self.state.assert_owner(Owner.AUTOMATION)

    # ---- transitions ----

    async def escalate(
        self,
        reason: str,
        *,
        step_index: int | None = None,
        context: str | None = None,
        detail: str | None = None,
    ) -> str:
        """Hand the screen to a human and stop acting.

        Order matters: pause the adapter and clear the gate BEFORE announcing, so
        there is no window in which the intervention is visible but the automation is
        still able to click.
        """
        self._gate.clear()
        if self.adapter is not None:
            await self.adapter.pause()

        self.state = self.state.transition(
            Owner.HUMAN_PENDING, reason=reason, detail=detail
        )
        self.intervention_id = f"int_{secrets.token_hex(4)}"
        self.intervention_reason = reason
        self.intervention_step_index = step_index
        self.intervention_context = context
        self._write_intervention()
        return self.intervention_id

    def accept(self, *, operator: str, control_version: int | None = None) -> ControlState:
        """A human takes the screen. Does NOT resume automation — accepting and
        handing back are separate acts, because the whole point is the interval
        between them."""
        self.state = self.state.transition(
            Owner.HUMAN, expected_version=control_version, operator=operator
        )
        self._write_intervention()
        return self.state

    async def resume(self, *, control_version: int | None = None) -> ControlState:
        """Hand control back. The caller must then re-observe: the human may have
        changed anything, and acting on a pre-handoff observation is exactly the bug
        this mechanism exists to prevent."""
        self.state = self.state.transition(
            Owner.AUTOMATION, expected_version=control_version
        )
        if self.adapter is not None:
            await self.adapter.resume()
        self._gate.set()
        self._write_intervention()
        return self.state

    def complete(self, *, control_version: int | None = None, operator: str | None = None):
        self.state = self.state.transition(
            Owner.COMPLETED, expected_version=control_version, operator=operator
        )
        self._write_intervention()
        return self.state

    def cancel(self, *, control_version: int | None = None, operator: str | None = None):
        self.state = self.state.transition(
            Owner.CANCELLED, expected_version=control_version, operator=operator
        )
        self._gate.set()  # release anything parked so the run can end
        self._write_intervention()
        return self.state

    # ---- what the controller says to the model after a handoff ----

    def resume_note(self) -> dict[str, str]:
        """A mid-conversation system message for after a human acted.

        A system message rather than a user turn: it carries operator authority, and on
        Opus 5 it does not invalidate the cached prefix. The content is deliberately
        about *state*, not instructions — the model is being told the world moved, not
        told what to conclude.
        """
        return {
            "role": "system",
            "content": (
                "A human operator took control of this session and may have changed the "
                "screen. Ignore your previous expectations about what is displayed: "
                "re-observe before acting, and continue toward the original goal."
            ),
        }

    # ---- the handshake file ----

    @property
    def intervention_path(self) -> Path | None:
        if self.evidence_dir is None:
            return None
        return Path(self.evidence_dir) / "intervention.json"

    def _write_intervention(self) -> None:
        path = self.intervention_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "intervention_id": self.intervention_id,
            "reason": self.intervention_reason,
            "step_index": self.intervention_step_index,
            "context": self.intervention_context,
            "novnc_url": self.novnc_url,
            **self.state.to_dict(),
        }
        # Write-then-rename: the other process must never read a half-written file.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    def _reload_from_disk(self) -> None:
        """Pick up a transition made by the operator's process.

        Only ever moves forward: a file at an older version than memory is ignored,
        so a stale reader cannot roll this process back.
        """
        path = self.intervention_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            incoming = ControlState.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return  # a partially written or foreign file is not a reason to crash
        if incoming.control_version > self.state.control_version:
            self.state = incoming

    # ---- operator-side entry point (the other process) ----

    @classmethod
    def load(
        cls, run_id: str, evidence_dir: str | Path, **kwargs: Any
    ) -> SessionManager:
        """Open an existing run's control state from disk — what `cli session *` uses."""
        manager = cls(run_id, evidence_dir=evidence_dir, **kwargs)
        path = manager.intervention_path
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            manager.state = ControlState.from_dict(data)
            manager.intervention_id = data.get("intervention_id")
            manager.intervention_reason = data.get("reason")
            # Carried forward so the operator's own transitions do not erase the
            # context that explains why they were called in.
            manager.intervention_step_index = data.get("step_index")
            manager.intervention_context = data.get("context")
            if not manager.state.automation_may_act:
                manager._gate.clear()
        return manager


__all__ = [
    "InterventionTimeout",
    "NotControlOwner",
    "Owner",
    "RunEndedByHuman",
    "SessionManager",
    "StaleControlVersion",
]
