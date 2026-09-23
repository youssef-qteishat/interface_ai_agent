"""
Surface agent — the sandbox's HTTP face.

Runs INSIDE the sandbox container and exposes exactly three capabilities to the
host-side driver: see (screenshot/zoom), act (xdotool), and understand (a read-only
CDP probe).

Deliberately dumb. There is no policy, no model, and no loop logic here, and nothing
in this file imports from the orchestrator (`src/`). Those live on the host so that
every decision about what is allowed is made in one place. Validation below is
syntactic only — coordinates in range, capped durations, well-formed key names —
never "is this action permitted", which is the policy engine's job.

Security notes that must survive edits:
  * xdotool is invoked with an argv list, never a shell string.
  * The probe applies a FIXED function (probe.js) to an element handle. No endpoint
    accepts JavaScript from the caller.
  * CDP is treated as read-only: hit-testing, the frame tree, and the AX tree. No
    navigation, no input, no script injection into the page's own world.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import websockets
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

AGENT_VERSION = "0.3.0"

DISPLAY = os.environ.get("DISPLAY", ":99")
CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
CDP_BASE = f"http://{CDP_HOST}:{CDP_PORT}"

PROBE_JS = (Path(__file__).parent / "probe.js").read_text()

# Bounds — syntactic guards, not policy.
MAX_WAIT_S = 10.0
MAX_TEXT_LEN = 500
MAX_SETTLE_MS = 5000
DEFAULT_SETTLE_MS = 250
MAX_SCROLL_AMOUNT = 20

# xdotool key syntax: optional modifiers joined by '+', then a keysym.
KEY_COMBO_RE = re.compile(r"^[A-Za-z0-9_+]{1,40}$")

STARTED_AT = time.monotonic()


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #


async def run_cmd(*argv: str, timeout: float = 15.0) -> str:
    """Run a command as an argv list. No shell, ever."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "DISPLAY": DISPLAY},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"command timed out: {' '.join(argv)}")
    if proc.returncode != 0:
        raise HTTPException(
            500,
            {
                "error": "COMMAND_FAILED",
                "command": list(argv),
                "returncode": proc.returncode,
                "stderr": err.decode(errors="replace")[:500],
            },
        )
    return out.decode(errors="replace")


async def display_geometry() -> tuple[int, int]:
    out = await run_cmd("xdotool", "getdisplaygeometry")
    w, h = out.split()
    return int(w), int(h)


async def capture_png() -> bytes:
    """Full-display screenshot. scrot writes a file; we hand back the bytes."""
    path = f"/tmp/agent-shot-{os.getpid()}.png"
    await run_cmd("scrot", "-o", "-z", path)
    data = Path(path).read_bytes()
    return data


# --------------------------------------------------------------------------- #
# CDP — read-only
# --------------------------------------------------------------------------- #


async def cdp_targets() -> list[dict[str, Any]]:
    out = await run_cmd("curl", "-sf", f"{CDP_BASE}/json")
    return json.loads(out)


async def cdp_version() -> dict[str, Any]:
    out = await run_cmd("curl", "-sf", f"{CDP_BASE}/json/version")
    return json.loads(out)


async def page_target() -> dict[str, Any]:
    targets = [t for t in await cdp_targets() if t.get("type") == "page"]
    if not targets:
        raise HTTPException(503, {"error": "NO_PAGE_TARGET"})
    # Prefer a real http page over chrome:// surfaces.
    for t in targets:
        if t.get("url", "").startswith("http"):
            return t
    return targets[0]


class CDP:
    """One websocket, one request. Simple beats fast at ~40 actions per run, and a
    fresh connection can never be stale after a Chromium restart."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg_id = self._id
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=15.0)
            msg = json.loads(raw)
            if msg.get("id") != msg_id:
                continue  # an event, or another call's reply
            if "error" in msg:
                raise HTTPException(
                    502, {"error": "CDP_ERROR", "method": method, "detail": msg["error"]}
                )
            return msg.get("result", {})


@asynccontextmanager
async def cdp_session():
    target = await page_target()
    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=None, open_timeout=10
    ) as ws:
        yield CDP(ws)


def frame_path_for(frame_tree: dict, frame_id: str) -> list[str]:
    """Turn a CDP frameId into ['servicing-frame'] — the §7 trace's frame_path."""

    def walk(node: dict, path: list[str]) -> list[str] | None:
        frame = node["frame"]
        if frame["id"] == frame_id:
            return path
        for child in node.get("childFrames", []):
            cf = child["frame"]
            label = cf.get("name") or cf.get("url", "").rsplit("/", 1)[-1] or cf["id"]
            found = walk(child, path + [label])
            if found is not None:
                return found
        return None

    return walk(frame_tree, []) or []


def flatten_frames(node: dict, path: list[str] | None = None) -> list[dict]:
    path = path or []
    frame = node["frame"]
    out = [{"path": path, "url": frame.get("url", ""), "id": frame["id"]}]
    for child in node.get("childFrames", []):
        cf = child["frame"]
        label = cf.get("name") or cf.get("url", "").rsplit("/", 1)[-1] or cf["id"]
        out.extend(flatten_frames(child, path + [label]))
    return out


# Fixed observation script. Like probe.js this is shipped, not supplied.
OBSERVE_JS = """
(() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const visible = (n) => {
    const r = n.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const st = getComputedStyle(n);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const dialogs = Array.from(
    document.querySelectorAll('.modal-overlay, .modal-box, [role=dialog], dialog[open]')
  ).filter(visible).map((n) => ({ selector: n.className || n.tagName.toLowerCase(),
                                  text: clean(n.innerText).slice(0, 400) }));
  const overlays = Array.from(document.querySelectorAll('.overlay, .loading-overlay, .spinner'))
    .filter(visible).map((n) => clean(n.innerText).slice(0, 120) || n.className);
  const banners = Array.from(document.querySelectorAll('.warning-banner, .error-banner, .info-banner'))
    .filter(visible).map((n) => clean(n.innerText).slice(0, 300));
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,legend'))
    .filter(visible).map((n) => clean(n.innerText)).filter(Boolean);
  const controls = Array.from(document.querySelectorAll('button, a[href], input, select, textarea'))
    .filter(visible).map((n) => ({
      tag: n.tagName.toLowerCase(),
      type: n.getAttribute('type'),
      name: n.getAttribute('name'),
      text: clean(n.innerText || n.value || '').slice(0, 80),
    }));
  return {
    url: location.href,
    title: document.title,
    dialogs, overlays, banners, headings, controls,
    text: clean(document.body ? document.body.innerText : '').slice(0, 4000),
  };
})()
"""


# --------------------------------------------------------------------------- #
# request / response models
# --------------------------------------------------------------------------- #

Coord = Annotated[int, Field(ge=0, le=10000)]


class ClickAction(BaseModel):
    kind: Literal["left_click", "double_click"]
    coordinate: tuple[Coord, Coord]
    settle_ms: int = Field(DEFAULT_SETTLE_MS, ge=0, le=MAX_SETTLE_MS)


class TypeAction(BaseModel):
    kind: Literal["type"]
    text: str = Field(..., max_length=MAX_TEXT_LEN)
    delay_ms: int = Field(12, ge=0, le=200)
    settle_ms: int = Field(DEFAULT_SETTLE_MS, ge=0, le=MAX_SETTLE_MS)


class KeyAction(BaseModel):
    kind: Literal["key"]
    # Syntactic validation only. Deciding that, say, ctrl+l is forbidden is policy,
    # and policy lives on the host (plan §12.2).
    text: str = Field(..., pattern=KEY_COMBO_RE.pattern)
    repeat: int = Field(1, ge=1, le=20)
    settle_ms: int = Field(DEFAULT_SETTLE_MS, ge=0, le=MAX_SETTLE_MS)


class ScrollAction(BaseModel):
    kind: Literal["scroll"]
    scroll_direction: Literal["up", "down", "left", "right"]
    scroll_amount: int = Field(3, ge=1, le=MAX_SCROLL_AMOUNT)
    coordinate: tuple[Coord, Coord] | None = None
    settle_ms: int = Field(DEFAULT_SETTLE_MS, ge=0, le=MAX_SETTLE_MS)


class WaitAction(BaseModel):
    kind: Literal["wait"]
    duration: float = Field(..., gt=0, le=MAX_WAIT_S)


class CursorPositionAction(BaseModel):
    kind: Literal["cursor_position"]


ActionRequest = Annotated[
    ClickAction | TypeAction | KeyAction | ScrollAction | WaitAction | CursorPositionAction,
    Field(discriminator="kind"),
]


class ProbeRequest(BaseModel):
    x: Coord
    y: Coord


class ZoomRequest(BaseModel):
    region: tuple[Coord, Coord, Coord, Coord]  # x0, y0, x1, y1 in screenshot space


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="Sandbox Surface Agent",
    version=AGENT_VERSION,
    description=(
        "Screenshot / act / probe primitives for the sandbox desktop. "
        "Executes what it is told; every decision about what MAY be done lives on the host."
    ),
)


@app.get("/health", tags=["status"])
async def health() -> dict[str, Any]:
    width, height = await display_geometry()
    chromium_alive = True
    try:
        await run_cmd("pgrep", "-x", "chromium")
    except HTTPException:
        chromium_alive = False
    cdp: dict[str, Any] | None = None
    try:
        cdp = await cdp_version()
    except HTTPException:
        pass
    return {
        "ok": chromium_alive and cdp is not None,
        "agent_version": AGENT_VERSION,
        "uptime_s": round(time.monotonic() - STARTED_AT, 1),
        "display": {"name": DISPLAY, "width": width, "height": height},
        "chromium_alive": chromium_alive,
        "cdp": {"reachable": cdp is not None, "browser": (cdp or {}).get("Browser")},
    }


@app.get("/screenshot", tags=["see"])
async def screenshot() -> dict[str, Any]:
    """Full display. The sha256 is computed here so the host has one canonical
    observation hash for §7's observation_hash and Step 11's no-progress rule."""
    png = await capture_png()
    with Image.open(io.BytesIO(png)) as img:
        width, height = img.size
    return {
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(png).hexdigest(),
        "captured_at_ms": int(time.time() * 1000),
        "png_base64": base64.b64encode(png).decode(),
    }


@app.get("/screenshot.png", tags=["see"], response_class=Response)
async def screenshot_png() -> Response:
    """Same frame, raw bytes — so a human can just open this in a browser."""
    png = await capture_png()
    return Response(content=png, media_type="image/png")


@app.post("/zoom", tags=["see"])
async def zoom(req: ZoomRequest) -> dict[str, Any]:
    """Crop a region at full resolution.

    The region is in FULL-SCREENSHOT pixel space and the result never redefines the
    coordinate system: the model keeps expressing clicks in screenshot space after a
    zoom (plan §4).
    """
    x0, y0, x1, y1 = req.region
    if x1 <= x0 or y1 <= y0:
        raise HTTPException(422, {"error": "EMPTY_REGION", "region": list(req.region)})
    png = await capture_png()
    with Image.open(io.BytesIO(png)) as img:
        width, height = img.size
        box = (max(0, x0), max(0, y0), min(width, x1), min(height, y1))
        crop = img.crop(box)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        data = buf.getvalue()
    return {
        "region": list(box),
        "width": crop.width,
        "height": crop.height,
        "full_display": {"width": width, "height": height},
        "sha256": hashlib.sha256(data).hexdigest(),
        "png_base64": base64.b64encode(data).decode(),
    }


@app.post("/act", tags=["act"])
async def act(action: ActionRequest) -> dict[str, Any]:
    started = time.monotonic()
    detail: dict[str, Any] = {}
    width, height = await display_geometry()

    def check_bounds(x: int, y: int) -> None:
        if not (0 <= x < width and 0 <= y < height):
            raise HTTPException(
                422,
                {
                    "error": "COORDINATE_OUT_OF_BOUNDS",
                    "coordinate": [x, y],
                    "display": [width, height],
                },
            )

    if isinstance(action, ClickAction):
        x, y = action.coordinate
        check_bounds(x, y)
        argv = ["xdotool", "mousemove", "--sync", str(x), str(y), "click"]
        if action.kind == "double_click":
            argv += ["--repeat", "2", "--delay", "80"]
        argv.append("1")
        await run_cmd(*argv)
        detail = {"coordinate": [x, y]}

    elif isinstance(action, TypeAction):
        # `--` then the text as its own argv element: no shell, no interpolation.
        await run_cmd("xdotool", "type", "--delay", str(action.delay_ms), "--", action.text)
        detail = {"chars": len(action.text)}

    elif isinstance(action, KeyAction):
        await run_cmd("xdotool", "key", "--repeat", str(action.repeat), action.text)
        detail = {"key": action.text, "repeat": action.repeat}

    elif isinstance(action, ScrollAction):
        button = {"up": "4", "down": "5", "left": "6", "right": "7"}[action.scroll_direction]
        argv = ["xdotool"]
        if action.coordinate:
            x, y = action.coordinate
            check_bounds(x, y)
            argv += ["mousemove", "--sync", str(x), str(y)]
        argv += ["click", "--repeat", str(action.scroll_amount), button]
        await run_cmd(*argv)
        detail = {"direction": action.scroll_direction, "amount": action.scroll_amount}

    elif isinstance(action, WaitAction):
        await asyncio.sleep(action.duration)
        detail = {"waited_s": action.duration}

    elif isinstance(action, CursorPositionAction):
        out = await run_cmd("xdotool", "getmouselocation", "--shell")
        pos = dict(
            line.split("=", 1) for line in out.strip().splitlines() if "=" in line
        )
        detail = {"coordinate": [int(pos["X"]), int(pos["Y"])]}

    # A short settle so an HTMX swap has a chance to land before the caller observes.
    # Bounded and reported; the host owns any longer, condition-based waiting.
    settle_ms = getattr(action, "settle_ms", 0)
    if settle_ms:
        await asyncio.sleep(settle_ms / 1000)

    return {
        "ok": True,
        "kind": action.kind,
        "settled_ms": int((time.monotonic() - started) * 1000),
        "detail": detail,
    }


@app.post("/probe", tags=["understand"])
async def probe(req: ProbeRequest) -> dict[str, Any]:
    """Read-only semantic lookup at a screen coordinate.

    CDP's own hit-testing (DOM.getNodeForLocation) descends into iframes, so the
    frame-descent bug class disappears: the node we describe is the node the click
    would hit, in the frame it actually lives in.
    """
    async with cdp_session() as cdp:
        await cdp.call("DOM.enable")
        await cdp.call("Page.enable")

        try:
            hit = await cdp.call(
                "DOM.getNodeForLocation",
                {"x": req.x, "y": req.y, "includeUserAgentShadowDOM": False},
            )
        except HTTPException as exc:
            raise HTTPException(
                404,
                {"error": "NO_NODE_AT_POINT", "coordinate": [req.x, req.y], "detail": exc.detail},
            )

        backend_node_id = hit.get("backendNodeId")
        frame_id = hit.get("frameId")

        tree = await cdp.call("Page.getFrameTree")
        frame_path = frame_path_for(tree["frameTree"], frame_id) if frame_id else []

        # Authoritative role + accessible name, rather than re-deriving ARIA by hand.
        role = name = None
        try:
            ax = await cdp.call(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend_node_id, "fetchRelatives": False},
            )
            for node in ax.get("nodes", []):
                if node.get("backendDOMNodeId") == backend_node_id:
                    role = (node.get("role") or {}).get("value")
                    name = (node.get("name") or {}).get("value")
                    break
        except HTTPException:
            pass  # AX unavailable: the JS ladder below still produces candidates

        resolved = await cdp.call("DOM.resolveNode", {"backendNodeId": backend_node_id})
        object_id = resolved["object"]["objectId"]

        # functionDeclaration must be ONE function expression, which CDP calls with
        # `this` bound to the node. probe.js defines a named function, so wrap it and
        # forward `this` — concatenating the declaration with a bare reference does
        # not evaluate to a callable and silently yields undefined.
        result = await cdp.call(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": (
                    "function() {\n" + PROBE_JS + "\nreturn probeElement.call(this);\n}"
                ),
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        # Never let a probe failure pass as an empty result: an empty candidate list
        # would look like "this element has no locators" instead of "the probe broke".
        if result.get("exceptionDetails"):
            raise HTTPException(
                502,
                {
                    "error": "PROBE_SCRIPT_FAILED",
                    "coordinate": [req.x, req.y],
                    "detail": result["exceptionDetails"],
                },
            )
        details = result["result"].get("value")
        if not details:
            raise HTTPException(
                502,
                {
                    "error": "PROBE_RETURNED_NOTHING",
                    "coordinate": [req.x, req.y],
                    "hint": "probe.js must return a JSON-serializable object",
                },
            )

    accessible_name = name or None
    candidates = list(details.get("candidates", []))

    # The AX lookup is keyed to the node the coordinate hit. If probe.js retargeted to
    # an interactive ancestor, that answer describes the wrong element (an <img>'s
    # role is "none"), so prefer the hints computed for the element we actually
    # describe. Recorded either way, so the trace shows which source was used.
    role_source = "ax_tree"
    if details.get("retargeted_from"):
        role = details.get("role_hint") or role
        accessible_name = details.get("name_hint") or accessible_name
        role_source = "retargeted_element"

    # Where did the accessible name come from? Chromium will happily compute one from
    # a placeholder, and "$0.00" is a terrible locator — it is a formatting hint, not
    # an identity. Flag that so the canonicalizer can demote it instead of trusting it.
    placeholder = details.get("placeholder")
    name_source = None
    if accessible_name:
        if placeholder and accessible_name.strip() == placeholder.strip():
            name_source = "placeholder"
        elif details.get("visible_text") and accessible_name.strip() == details["visible_text"].strip():
            name_source = "visible_text"
        elif details.get("nearby_label") and accessible_name.strip() == details["nearby_label"].strip():
            name_source = "label"
        else:
            name_source = "computed"

    # The role+name candidate is only meaningful when a name actually exists — which
    # on this app it usually does not for inputs. That absence is the point: the trace
    # should show the ladder degrading, not a fabricated name.
    if role and accessible_name:
        role_candidate = {
            "kind": "role",
            "role": role,
            "name": accessible_name,
            "name_source": name_source,
            # Only the text candidate actually counts matches for the same element.
            # null (not 0, not -1) means "not counted here" — the resolver must check.
            "match_count": next(
                (c["match_count"] for c in candidates if c.get("kind") == "text"), None
            ),
        }
        # A placeholder-derived name goes below the structural candidates: it looks
        # authoritative and is not.
        if name_source == "placeholder":
            candidates.append(role_candidate)
        else:
            candidates.insert(0, role_candidate)

    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "tag": details.get("tag"),
                "role": role,
                "name": accessible_name,
                "classes": details.get("classes"),
                "region": details.get("enclosing_region"),
                "frame": frame_path,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()

    return {
        "coordinate": [req.x, req.y],
        "frame_path": frame_path,
        "role": role,
        "role_source": role_source,
        "accessible_name": accessible_name,
        "accessible_name_source": name_source,
        **details,
        "candidates": candidates,
        "element_fingerprint": f"sha256:{fingerprint}",
    }


@app.post("/probe/observe", tags=["understand"])
async def observe() -> dict[str, Any]:
    """Whole-screen state: frames, dialogs, overlays, headings, and a DOM hash.

    Feeds observation_before / observation_after. The dom_hash is what makes
    `dom_changed` and the repeated-observation stop rule possible without diffing
    screenshots.
    """
    async with cdp_session() as cdp:
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        tree = await cdp.call("Page.getFrameTree")
        frames = flatten_frames(tree["frameTree"])

        per_frame: list[dict[str, Any]] = []
        for frame in frames:
            # An isolated world keeps this read out of the page's own JS context.
            try:
                world = await cdp.call(
                    "Page.createIsolatedWorld",
                    {"frameId": frame["id"], "worldName": "surface-agent-observe"},
                )
                res = await cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": OBSERVE_JS,
                        "contextId": world["executionContextId"],
                        "returnByValue": True,
                    },
                )
                value = res["result"].get("value") or {}
            except HTTPException:
                value = {}
            per_frame.append({"path": frame["path"], "url": frame["url"], **value})

    main = per_frame[0] if per_frame else {}
    dialogs = [d for f in per_frame for d in f.get("dialogs", [])]
    overlays = [o for f in per_frame for o in f.get("overlays", [])]
    banners = [b for f in per_frame for b in f.get("banners", [])]
    headings = [h for f in per_frame for h in f.get("headings", [])]
    text_blob = "\n".join(f.get("text", "") for f in per_frame)

    return {
        "main_frame_url": main.get("url"),
        "title": main.get("title"),
        "frames": [
            {
                "path": f["path"],
                "url": f["url"],
                "headings": f.get("headings", []),
                "controls": f.get("controls", []),
            }
            for f in per_frame
        ],
        "dialogs": dialogs,
        "overlays": overlays,
        "banners": banners,
        "headings": headings,
        "visible_text": text_blob[:4000],
        "dom_hash": "sha256:" + hashlib.sha256(text_blob.encode()).hexdigest(),
        "observed_at_ms": int(time.time() * 1000),
    }
