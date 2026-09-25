"""
The replay surface: Playwright, on the host, driving roles and labels.

The contrast with discovery is the point of this file. `X11ComputerAdapter` moves a mouse at
coordinates inside a contained desktop, because a model has to be watched and a model has to be
contained. Replay has no model to contain, so it needs none of that — no Xvfb, no VNC, no xdotool, no
surface agent, and not even the sandbox container. It needs `bank-sim`, which has no `depends_on` and
publishes 8001 to the host:

    docker compose up -d bank-sim

What it does need is **actionability**: waiting until an element is attached, visible, stable, enabled
and hit-testable before touching it. Playwright does that on every action, and reimplementing it on CDP
would have been most of the work of this layer. That is the determinism argument, not an
implementation detail.

The one thing here that can silently break safety is `BaseUrlRebind` — see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.domain.artifact import Capability
from src.domain.trace import DialogInfo, FrameInfo, Observation, ProbeResult
from src.surfaces.base import SurfaceError, SurfaceUnavailable

DEFAULT_BASE_URL = "http://127.0.0.1:8001"

# What the simulator renders when something is covering the page or demanding attention. Read from
# the page rather than guessed, so `observe()` can answer the dialog/overlay conditions the artifact's
# outcome rules are written against.
DIALOG_SELECTOR = ".modal-overlay"
# `.overlay`, not `.loading-overlay`: the app renders `<div id="loading-overlay" class="overlay
# htmx-indicator">`, so `loading-overlay` is the *id*. The earlier value matched nothing at all, which
# made `observation.overlays` permanently empty and the artifact's `overlay(present: true)` outcome rule
# impossible to fire. The discovery-side agent had it right (`sandbox/surface_agent.py`); this side
# drifted, and the two surfaces must report the same screen the same way or a condition derived from a
# discovery trace means something else at replay time.
OVERLAY_SELECTOR = ".overlay"
BANNER_SELECTOR = ".warning-banner"

# Both overlays in this app live in the DOM permanently and are hidden by `.htmx-indicator { opacity: 0;
# visibility: hidden }` until htmx adds `htmx-request`. So presence in the DOM is not the question —
# visibility is, or every idle page reports an overlay that is covering nothing.
#
# Playwright's `:visible` ignores `opacity: 0` where the discovery agent's predicate also rejects it.
# Every hidden overlay here sets `visibility: hidden` alongside the opacity, so the two agree on this
# app; porting the JS predicate for a case that cannot arise would be the more fragile choice.
VISIBLE = ":visible"


class RebindError(SurfaceError):
    """The artifact's origin cannot be mapped onto where the app actually is."""


@dataclass(frozen=True)
class BaseUrlRebind:
    """Maps the origin the artifact declares onto the origin replay can actually reach.

    The artifact says `http://bank-sim:8001` — the hostname *the agent* saw from inside
    `sandbox_net`. Replay runs on the host, where the same app is `http://127.0.0.1:8001`. Without a
    rebind every URL replay observes is refused, because `PolicyEngine._check_urls` does a plain
    `origin not in allowed_origins` against the artifact's value.

    The two obvious ways to make that error go away are both wrong, and both are silent:

      * pass an empty allowlist — every origin passes, including one an injected link navigated to;
      * pass *both* origins — the artifact's origin is now permanently allowed at replay time, which
        is exactly the check the allowlist exists to make.

    So the mapping is **one origin to one origin**, and `policy_origins` returns only the runtime one.
    Anything else is still refused.

    It maps one way only, and that is deliberate. An earlier version also rewrote observed URLs *back*
    to the artifact's origin so the trace would read against the artifact — which quietly meant the
    policy engine was checking a rewritten view of the world rather than where the browser actually
    was. Evidence records what happened; `describe()` goes in the trace once so a reader can see the
    correspondence without any URL being edited.
    """

    artifact_origin: str
    runtime_origin: str

    @classmethod
    def for_capability(cls, capability: Capability, base_url: str = DEFAULT_BASE_URL) -> BaseUrlRebind:
        origins = capability.policy.allowed_origins
        if len(origins) != 1:
            # Guessing which of several to rebind would be a coin flip with the origin allowlist
            # riding on it.
            raise RebindError(
                f"expected exactly one allowed origin to rebind, found {len(origins)}: {origins}"
            )
        return cls(artifact_origin=_origin_of(origins[0]), runtime_origin=_origin_of(base_url))

    def apply(self, url: str) -> str:
        """Artifact origin → runtime origin. Used for navigation, and nowhere else."""
        if url.startswith(self.artifact_origin):
            return self.runtime_origin + url[len(self.artifact_origin) :]
        return url

    @property
    def policy_origins(self) -> tuple[str, ...]:
        """What `PolicyEngine(allowed_origins=...)` gets: the runtime origin, and nothing else."""
        return (self.runtime_origin,)

    def describe(self) -> str:
        return f"{self.artifact_origin} -> {self.runtime_origin}"


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise RebindError(f"not an absolute origin: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


class ReplaySurface:
    """One browser session, for one replay run.

    Deliberately the same shape as `SurfaceAdapter` — `observe`, `act`-ish primitives,
    `capture_evidence`, `pause`, `resume` — so the cross-surface argument holds in code rather than
    only in prose. It does **not** expose `resolve`: turning a candidate bundle into a locator is the
    resolver's job, and building that in two places is how the two versions drift.
    """

    def __init__(
        self,
        rebind: BaseUrlRebind,
        *,
        headed: bool = False,
        default_timeout_ms: int = 5000,
    ) -> None:
        self.rebind = rebind
        self.headed = headed
        self.default_timeout_ms = default_timeout_ms
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.page: Any = None
        self._paused = False

    # ---- lifecycle ----

    async def start(self) -> ReplaySurface:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=not self.headed)
        # A fresh context per run: no cookies or storage carried in from anywhere else, so a replay
        # cannot pass because of state a previous run left behind.
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._context.set_default_timeout(self.default_timeout_ms)
        self.page = await self._context.new_page()
        return self

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = self.page = None

    async def __aenter__(self) -> ReplaySurface:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- ownership, for the same SessionManager discovery uses ----

    async def pause(self) -> None:
        self._paused = True

    async def resume(self) -> None:
        self._paused = False

    def _assert_active(self) -> None:
        if self._paused:
            raise SurfaceError("surface is paused; automation does not hold control")
        if self.page is None:
            raise SurfaceUnavailable("surface is not started")

    # ---- navigation and frames ----

    async def goto(self, url: str) -> None:
        """Navigate, rebinding the artifact's origin onto the runtime one first."""
        self._assert_active()
        try:
            await self.page.goto(self.rebind.apply(url), wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise SurfaceUnavailable(f"could not open {url}: {exc}") from exc

    def frame(self, frame_path: list[str] | None = None) -> Any:
        """`["servicing-frame"]` -> `page.frame_locator('iframe[name="servicing-frame"]')`.

        Chained, so a nested path works, though this app only nests one deep. Every target in the
        artifact is frame-scoped: the workflow never lives in the main document, which is also why
        `main_frame_url` is useless as an assertion.
        """
        self._assert_active()
        scope: Any = self.page
        for name in frame_path or []:
            scope = scope.frame_locator(f'iframe[name="{name}"]')
        return scope

    # ---- observation ----

    async def observe(self, frame_path: list[str] | None = None) -> Observation:
        """The screen, as the **discovery** `Observation` model.

        Not a new shape on purpose: `PolicyEngine.check_action` takes an `Observation`, so reusing it
        is what lets the same policy engine run at replay time with no changes. The screenshot and DOM
        hashes stay empty — replay has no use for pixel-progress detection, and inventing values for
        them would imply it does.

        URLs are reported **as the browser actually has them**, never rebound back. The policy engine
        reads this object, and a check against an edited view of where the browser is would not be a
        check.
        """
        self._assert_active()
        frames: list[FrameInfo] = [FrameInfo(path=[], url=self.page.url)]

        inner_url = None
        for name in frame_path or []:
            handle = await self.page.query_selector(f'iframe[name="{name}"]')
            child = await handle.content_frame() if handle else None
            if child is not None:
                inner_url = child.url
                frames.append(FrameInfo(path=[name], url=inner_url))

        scope = self.frame(frame_path)
        body = scope.locator("body")
        visible_text = (await body.inner_text()).strip() if await body.count() else ""

        headings = [
            (text or "").strip()
            for text in await scope.locator("h1, h2, h3, h4").all_inner_texts()
            if (text or "").strip()
        ]
        dialogs = [
            DialogInfo(selector=DIALOG_SELECTOR, text=(text or "").strip())
            for text in await scope.locator(DIALOG_SELECTOR + VISIBLE).all_inner_texts()
        ]
        # A visible overlay whose spinner text is empty still *is* an overlay, so it falls back to the
        # class name rather than an empty string — the same thing the discovery agent does, and what
        # keeps `overlays` from being `[""]`, which reads as present to anyone checking truthiness.
        overlay_nodes = await scope.locator(OVERLAY_SELECTOR + VISIBLE).all()
        overlays = [
            (await node.inner_text()).strip()
            or (await node.get_attribute("class") or OVERLAY_SELECTOR)
            for node in overlay_nodes
        ]
        banners = [t.strip() for t in await scope.locator(BANNER_SELECTOR).all_inner_texts()]

        return Observation(
            main_frame_url=self.page.url,
            title=await self.page.title(),
            frames=frames,
            dialogs=dialogs,
            overlays=overlays,
            banners=banners,
            headings=headings,
            visible_text=visible_text,
        )

    # ---- the primitives a step executes through ----

    async def click(self, locator: Any, *, timeout_ms: int | None = None) -> None:
        self._assert_active()
        await locator.click(timeout=timeout_ms or self.default_timeout_ms)

    async def fill(self, locator: Any, value: str, *, timeout_ms: int | None = None) -> None:
        self._assert_active()
        await locator.fill(value, timeout=timeout_ms or self.default_timeout_ms)

    async def select(self, locator: Any, value: str, *, timeout_ms: int | None = None) -> None:
        self._assert_active()
        await locator.select_option(value, timeout=timeout_ms or self.default_timeout_ms)

    async def check(self, locator: Any, *, timeout_ms: int | None = None) -> None:
        self._assert_active()
        await locator.check(timeout=timeout_ms or self.default_timeout_ms)

    async def read(self, locator: Any, *, timeout_ms: int | None = None) -> str:
        self._assert_active()
        return (await locator.inner_text(timeout=timeout_ms or self.default_timeout_ms)).strip()

    # The two reads a `value` / `checked` condition needs. Through the surface rather than straight off
    # the locator so `_assert_active` still applies: a surface that has handed control to a human must
    # not be read behind their back any more than it may be acted on.
    async def input_value(self, locator: Any, *, timeout_ms: int | None = None) -> str:
        self._assert_active()
        return await locator.input_value(timeout=timeout_ms or self.default_timeout_ms)

    async def is_checked(self, locator: Any, *, timeout_ms: int | None = None) -> bool:
        self._assert_active()
        return await locator.is_checked(timeout=timeout_ms or self.default_timeout_ms)

    async def describe_element(self, locator: Any, frame_path: list[str] | None = None) -> ProbeResult:
        """What the resolved element actually is, as the `ProbeResult` the policy engine classifies.

        The same type discovery's probe produces, because it feeds the same `check_target` — which reads
        `classes`, `accessible_name`, `visible_text` and `tag` to decide whether this is a control that
        commits something. Building it from the live element rather than from the artifact is the point:
        an artifact recorded against a page where `Continue` was safe must not authorise a click on a
        button that is now `danger-button`.

        One `evaluate` so the element is read in a single round trip and cannot change between fields.
        """
        facts = await locator.evaluate(
            """el => ({
                tag: el.tagName.toLowerCase(),
                classes: Array.from(el.classList),
                text: (el.innerText || el.value || '').trim().slice(0, 200),
                name: (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim(),
                type: el.getAttribute('type'),
                nameAttr: el.getAttribute('name'),
                domId: el.id || null,
                disabled: !!el.disabled,
            })"""
        )
        return ProbeResult(
            frame_path=list(frame_path or []),
            tag=facts["tag"],
            classes=facts["classes"],
            # `check_target` reads both, and falls back from one to the other. A button's accessible name
            # is usually its own text, which is why `visible_text` carries the signal here.
            accessible_name=facts["name"] or None,
            visible_text=facts["text"] or None,
            type=facts["type"],
            name_attr=facts["nameAttr"],
            dom_id=facts["domId"],
            disabled=facts["disabled"],
            visible=await locator.is_visible(),
        )

    async def scoped_text(self, frame_path: list[str] | None, selector: str) -> str:
        """The visible text of one region of the page, or `""` when it is not there.

        What a `text` condition reads when it names a `within`. Empty rather than an error on purpose: a
        region that has not arrived means the assertion does not hold *yet*, which is exactly what
        `wait_for` polls on. Raising would turn a page that is still loading into a hard failure.
        """
        self._assert_active()
        region = self.frame(frame_path).locator(selector)
        if not await region.count():
            return ""
        return " ".join((await region.first.inner_text()).split())

    async def capture_evidence(self) -> bytes:
        """A full-page PNG for the evidence folder. Never redacted — see `evidence/writer.py`."""
        self._assert_active()
        return await self.page.screenshot(full_page=True)
