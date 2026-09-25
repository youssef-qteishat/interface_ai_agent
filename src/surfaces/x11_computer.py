"""
The X11 surface adapter — the host side of the sandbox.

This is the only place that knows the surface agent speaks HTTP, and the only place
that converts between coordinate spaces. Everything above it works in screenshot
pixels and typed actions; everything below it is xdotool and CDP.

Two responsibilities are worth naming, because they are easy to scatter and painful
to un-scatter later:

1. **Coordinate space.** The model sees screenshots and answers in screenshot pixels.
   The display has its own size. `compute_scale` is the single conversion, and the
   scale is recorded in the trace so a coordinate can always be interpreted later. At
   1280x800 it is exactly 1.0 — the code path still runs, so it cannot rot unnoticed.

2. **Capability gaps.** The domain models mirror Anthropic's schema; the agent has its
   own limits. Where a smaller number preserves intent (a shorter wait, less scroll)
   the adapter clamps and *records that it clamped*. Where it would change meaning
   (truncated text, a dropped modifier key) it refuses. The trace must never say an
   action ran as requested when something else happened.
"""

from __future__ import annotations

import base64
import math
import os
import re
from typing import Any

import httpx

from src.domain.actions import (
    CursorPosition,
    DoubleClick,
    Key,
    LeftClick,
    Scroll,
    Screenshot,
    Type,
    Wait,
    Zoom,
)
from src.domain.trace import Observation, ProbeResult
from src.surfaces.base import (
    ActionRejected,
    ProbeFailed,
    SurfaceUnavailable,
    UnsupportedAction,
)

# Image limits the model's screenshots must fit inside. Conservative defaults: these
# are the limits for models before Opus 4.7 (Opus 5 allows 2576 px / ~3.75 MP). They
# are parameters rather than constants so raising them is a decision, not an edit.
DEFAULT_MAX_LONG_EDGE = 1568
DEFAULT_MAX_PIXELS = 1_150_000

# Mirrors of the agent's caps. Duplicated deliberately: the adapter must decide what
# to do about a limit *before* paying for a round trip that would 422.
AGENT_MAX_WAIT_S = 10.0
AGENT_MAX_SCROLL = 20
AGENT_MAX_TEXT_LEN = 500
AGENT_KEY_RE = re.compile(r"^[A-Za-z0-9_+]{1,40}$")

DEFAULT_AGENT_URL = os.environ.get("SANDBOX_AGENT_URL", "http://127.0.0.1:8900")
DEFAULT_NOVNC_URL = os.environ.get(
    "SANDBOX_NOVNC_URL", "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"
)


# --------------------------------------------------------------------------- #
# scaling — pure, container-free, unit-testable
# --------------------------------------------------------------------------- #


def compute_scale(
    width: int,
    height: int,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> float:
    """Factor to multiply display pixels by to get the screenshot we send the model.

    Never upscales: a small display is sent as-is. Both limits apply, so a wide-but-
    short display is bounded by its long edge and a large square one by total pixels.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid display size: {width}x{height}")
    long_edge_scale = max_long_edge / max(width, height)
    total_pixels_scale = math.sqrt(max_pixels / (width * height))
    return min(1.0, long_edge_scale, total_pixels_scale)


def to_model(coord: tuple[int, int], scale: float) -> tuple[int, int]:
    """Display pixels → the space the model sees."""
    return (round(coord[0] * scale), round(coord[1] * scale))


def to_display(coord: tuple[int, int], scale: float) -> tuple[int, int]:
    """Model coordinates → real display pixels. This is the direction that moves a
    cursor, so it is the one that must never be applied twice."""
    if scale <= 0:
        raise ValueError(f"invalid scale: {scale}")
    return (round(coord[0] / scale), round(coord[1] / scale))


# --------------------------------------------------------------------------- #
# adapter
# --------------------------------------------------------------------------- #


class X11ComputerAdapter:
    """Talks to `sandbox/surface_agent.py` over HTTP.

    Implements `SurfaceAdapter`. One instance per run; call `aclose()` when done, or
    use it as an async context manager.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_AGENT_URL,
        *,
        timeout: float = 30.0,
        max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
        max_pixels: int = DEFAULT_MAX_PIXELS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_long_edge = max_long_edge
        self.max_pixels = max_pixels
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)
        self._paused = False
        # Bytes of the most recent screenshot, for the evidence writer to persist.
        self.last_screenshot_png: bytes | None = None
        # Learned from /health on first use; until then, assume no scaling.
        self.display_width: int | None = None
        self.display_height: int | None = None
        self.scale: float = 1.0

    async def __aenter__(self) -> X11ComputerAdapter:
        await self.health()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- transport ----

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise SurfaceUnavailable(
                f"cannot reach the surface agent at {self.base_url}",
                path=path,
                cause=str(exc),
            ) from exc
        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            # 4xx means "this request was wrong"; 5xx means the surface itself broke.
            if response.status_code >= 500:
                raise SurfaceUnavailable(
                    f"surface agent error on {path}", status=response.status_code, detail=detail
                )
            raise ActionRejected(
                f"surface agent rejected {path}", status=response.status_code, detail=detail
            )
        return response

    # ---- lifecycle ----

    async def health(self) -> dict[str, Any]:
        data = (await self._request("GET", "/health")).json()
        display = data.get("display", {})
        self.display_width = display.get("width")
        self.display_height = display.get("height")
        if self.display_width and self.display_height:
            self.scale = compute_scale(
                self.display_width, self.display_height, self.max_long_edge, self.max_pixels
            )
        return data

    async def pause(self) -> None:
        self._paused = True

    async def resume(self) -> None:
        self._paused = False

    def _assert_active(self) -> None:
        if self._paused:
            # Belt and braces: Step 8's ownership state machine is the real gate, but
            # an adapter that would happily act while paused is a race waiting to
            # happen during a human handoff.
            raise ActionRejected("surface is paused; automation does not hold control")

    # ---- seeing ----

    async def screenshot(self) -> dict[str, Any]:
        """Raw screenshot payload: width, height, sha256, png_base64."""
        return (await self._request("GET", "/screenshot")).json()

    async def observe(self, *, with_screenshot: bool = True) -> Observation:
        """Build a Step 4 `Observation` from the agent's two read endpoints.

        The screenshot's sha256 becomes `observation_hash`; the agent's text digest
        stays `dom_hash`. Keeping both is what lets Step 11 tell "the page repainted"
        from "the page changed".

        The PNG bytes are left on `last_screenshot_png` rather than written here: the
        evidence writer owns the filesystem, which is what makes "everything on disk
        passed the redaction gate" a true statement rather than an intention.
        """
        payload = (await self._request("POST", "/probe/observe")).json()
        observation = Observation.model_validate(payload)
        if with_screenshot:
            shot = await self.screenshot()
            observation.observation_hash = f"sha256:{shot['sha256']}"
            self.last_screenshot_png = base64.b64decode(shot["png_base64"])
        return observation

    async def zoom(self, region: tuple[int, int, int, int]) -> dict[str, Any]:
        x0, y0, x1, y1 = region
        display_region = (*to_display((x0, y0), self.scale), *to_display((x1, y1), self.scale))
        return (
            await self._request("POST", "/zoom", json={"region": list(display_region)})
        ).json()

    async def capture_evidence(self) -> bytes:
        """Return PNG bytes for the evidence writer to persist.

        Deliberately does not write: one component owns the filesystem (see `observe`).
        """
        shot = await self.screenshot()
        self.last_screenshot_png = base64.b64decode(shot["png_base64"])
        return self.last_screenshot_png

    # ---- understanding ----

    async def probe(self, x: int, y: int) -> ProbeResult:
        """Read-only lookup at a MODEL coordinate (converted here, once)."""
        dx, dy = to_display((x, y), self.scale)
        try:
            payload = (await self._request("POST", "/probe", json={"x": dx, "y": dy})).json()
        except ActionRejected as exc:
            # 404/502 from the probe are a different kind of problem from a refused
            # action: there was nothing there, or the probe itself broke.
            raise ProbeFailed("probe failed", coordinate=[x, y], detail=exc.context) from exc
        return ProbeResult.model_validate(payload)

    # ---- acting ----

    async def act(self, action: Any) -> dict[str, Any]:
        """Execute one typed action, translating domain shape → agent wire shape.

        Returns the agent's result plus `clamped` when a parameter had to be reduced,
        so the recorder can write down what actually ran.
        """
        self._assert_active()

        # Imagery is not an input action; it has its own endpoints.
        if isinstance(action, Screenshot):
            shot = await self.screenshot()
            return {"ok": True, "kind": "screenshot", "width": shot["width"],
                    "height": shot["height"], "sha256": shot["sha256"]}
        if isinstance(action, Zoom):
            result = await self.zoom(action.region)
            return {"ok": True, "kind": "zoom", **{k: v for k, v in result.items()
                                                   if k != "png_base64"}}

        payload, clamped = self._to_agent_payload(action)
        result = (await self._request("POST", "/act", json=payload)).json()
        if clamped:
            result["clamped"] = clamped
        return result

    def _to_agent_payload(self, action: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Translate one domain action. Raises rather than approximating."""
        clamped: dict[str, Any] = {}

        if isinstance(action, (LeftClick, DoubleClick)):
            if action.text:
                # Dropping the modifier would produce a plain click that looks like it
                # worked. The X11 driver has no modifier support, so say so.
                raise UnsupportedAction(
                    "this driver cannot hold modifier keys during a click",
                    modifiers=action.text,
                    kind=action.kind,
                )
            return {"kind": action.kind, "coordinate": list(
                to_display(action.coordinate, self.scale))}, None

        if isinstance(action, Type):
            if len(action.text) > AGENT_MAX_TEXT_LEN:
                # Truncating would enter different data than the model intended —
                # in a banking form, silently wrong input is the worst outcome.
                raise UnsupportedAction(
                    "text exceeds what the driver will type in one action",
                    length=len(action.text),
                    limit=AGENT_MAX_TEXT_LEN,
                )
            return {"kind": "type", "text": action.text}, None

        if isinstance(action, Key):
            if not AGENT_KEY_RE.match(action.text):
                raise UnsupportedAction(
                    "not a valid X keysym combination", key=action.text
                )
            repeat = min(action.repeat, 20)
            if repeat != action.repeat:
                clamped["repeat"] = {"requested": action.repeat, "applied": repeat}
            return {"kind": "key", "text": action.text, "repeat": repeat}, clamped or None

        if isinstance(action, Scroll):
            amount = min(action.scroll_amount, AGENT_MAX_SCROLL)
            if amount != action.scroll_amount:
                clamped["scroll_amount"] = {
                    "requested": action.scroll_amount, "applied": amount
                }
            payload: dict[str, Any] = {
                "kind": "scroll",
                "scroll_direction": action.scroll_direction,
                "scroll_amount": amount,
            }
            if action.coordinate is not None:
                payload["coordinate"] = list(to_display(action.coordinate, self.scale))
            return payload, clamped or None

        if isinstance(action, Wait):
            duration = min(action.duration, AGENT_MAX_WAIT_S)
            if duration != action.duration:
                # A shorter wait still means "wait"; the controller owns any longer,
                # condition-based waiting and can call again.
                clamped["duration"] = {"requested": action.duration, "applied": duration}
            return {"kind": "wait", "duration": duration}, clamped or None

        if isinstance(action, CursorPosition):
            return {"kind": "cursor_position"}, None

        raise UnsupportedAction(
            "no translation for this action", kind=getattr(action, "kind", type(action).__name__)
        )

    # ---- replay seam ----

    async def resolve_target(self, target: Any) -> Any:
        raise NotImplementedError("resolve_target belongs to the replay engine")
