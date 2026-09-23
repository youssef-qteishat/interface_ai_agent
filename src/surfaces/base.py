"""
The surface abstraction.

A surface is anything the driver can see and act on: a browser in a container today,
a native desktop later. The protocol below is deliberately small — screenshot in,
typed action out, plus a read-only semantic probe — because that is the whole of what
a pixel-level driver needs, and keeping it that small is what lets a second surface
exist without rewriting the loop.

What is NOT here matters as much as what is:

  * no policy — authorization happens on the host, above this layer (§12.2)
  * no model  — an adapter never decides what to do, only how to do it
  * no retries or waiting strategy — bounded waits belong to the controller, which
    owns the budget and can record why it waited

`resolve_target` is declared and unimplemented on purpose: it is the replay-time
entry point, and its absence here is what stops discovery code and replay code from
growing into each other.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from src.domain.trace import Observation, ProbeResult


class SurfaceError(Exception):
    """Base for everything a surface can refuse or fail to do.

    Each subclass maps to a different decision upstream, which is the reason they are
    separate types rather than one exception with a string.
    """

    code: str = "SURFACE_ERROR"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.context:
            return f"{self.code}: {self.message} {self.context}"
        return f"{self.code}: {self.message}"


class SurfaceUnavailable(SurfaceError):
    """The sandbox is not answering at all — a dead container, not a bad action.
    The controller maps this to FailureCode.SURFACE_UNAVAILABLE and stops; retrying
    individual actions against a surface that is gone only wastes the budget."""

    code = "SURFACE_UNAVAILABLE"


class ActionRejected(SurfaceError):
    """The surface understood the action and refused it — an off-screen coordinate,
    a malformed key combination. The action is well-formed but wrong for this screen,
    so the loop can report it and let the model try something else."""

    code = "ACTION_REJECTED"


class UnsupportedAction(SurfaceError):
    """This driver cannot express the action at all.

    Distinct from ActionRejected because it is a property of the *driver*, not of the
    screen: no amount of retrying or re-aiming will help, and the honest response is
    to say so rather than execute something approximate. Typing truncated text or
    dropping a modifier key would both "work" while doing the wrong thing.
    """

    code = "UNSUPPORTED_ACTION"


class ProbeFailed(SurfaceError):
    """No element under the point, or the probe script itself broke.

    Never silently degraded to "no candidates": a step with no locator evidence must
    say why, because the canonicalizer treats those two cases very differently.
    """

    code = "PROBE_FAILED"


@runtime_checkable
class SurfaceAdapter(Protocol):
    """What the discovery loop is allowed to ask of a surface.

    Async throughout: the Step 8 pause barrier and SIGINT cancellation both need to
    interrupt a run between actions, and that only works if the calls are awaitable.
    """

    async def health(self) -> dict[str, Any]:
        """Liveness plus geometry. Used once at startup to fail fast with a clear
        message rather than midway through a run."""
        ...

    async def observe(self, *, with_screenshot: bool = True) -> Observation:
        """One look at the screen: frames, dialogs, headings, and both hashes."""
        ...

    async def act(self, action: Any) -> dict[str, Any]:
        """Execute one typed action from `src.domain.actions`.

        Returns what actually happened, including whether any parameter had to be
        clamped to what this driver supports — the trace records the executed reality,
        not the request.
        """
        ...

    async def probe(self, x: int, y: int) -> ProbeResult:
        """Read-only: what is under this point, and how to find it again."""
        ...

    async def zoom(self, region: tuple[int, int, int, int]) -> dict[str, Any]:
        """Crop of the current screen. Coordinates stay in full-screenshot space."""
        ...

    async def capture_evidence(self, label: str) -> str | None:
        """Write a screenshot for the record; returns its path."""
        ...

    async def pause(self) -> None:
        """Stop accepting actions. The ownership state machine lives in Step 8; this
        is the adapter-level enforcement that makes it real."""
        ...

    async def resume(self) -> None: ...

    async def resolve_target(self, target: Any) -> Any:
        """Replay-time entry point: turn an artifact's locator bundle into a concrete
        element, verifying uniqueness before acting.

        Unimplemented by design. Discovery never resolves a stored locator — it
        records what it hit. Keeping this declared but empty marks the seam without
        letting replay logic leak into this layer.
        """
        raise NotImplementedError("resolve_target belongs to the replay engine")
