# Interface.ai Computer-Use Take-Home: Implementation Plan (v2 — Cross-Surface Update)

## Change log from v1

The original plan used Playwright/Playwright MCP as the discovery driver. Playwright MCP is browser-only — it exposes accessibility snapshots and actions for Chromium/Firefox/WebKit pages and has no concept of a native OS window. That makes it unsuitable for proving the system generalizes beyond the web.

This revision replaces the **discovery-time driver** with an **OS-level, screenshot-and-coordinate "computer use" driver** (Anthropic's Computer Use tool or OpenAI's `computer-use-preview`/CUA), which operates on raw screen pixels and OS-level input events. It has no idea whether the foreground window is a browser or a native app, which is exactly the property needed to demonstrate the same agent loop working against two different surface types. Playwright is retained, but demoted to a **replay-time and verification role** for the web surface only, where its accessibility-tree locators and auto-waiting remain the right tool for deterministic execution.

A second, minimal native desktop mock application is added so the discovery loop can be run twice — once against the browser-based credit union simulator, once against a small native form — using the identical model and driver, to produce real dual-surface evidence rather than a design claim.

## Recommended direction (updated)

Build the same **Credit Union Ops Simulator** as before, but change how it is discovered and replayed:

- **Discovery (both surfaces):** an OS-level computer-use driver takes a screenshot of the active window, sends it to the model, and executes the returned mouse/keyboard action via `xdotool` (Linux/X11) or PyAutoGUI. This driver is surface-agnostic by construction — it operates identically whether the active window is Chromium or a native desktop app.
- **Replay (web surface, implemented):** Playwright interprets the recorded artifact deterministically using accessibility-role/label/text locators, auto-waiting, and checkpoint assertions — no model in the loop.
- **Replay (desktop surface, designed but not required to implement):** a `pywinauto`/UIA (Windows) or AT-SPI (Linux)/`AXUIElement` (macOS) adapter would resolve the same style of locator bundle against native controls; coordinates remain a last-resort fallback on both surfaces.
- **Cross-surface proof:** a tiny second target — a native desktop form (Tkinter/PySimpleGUI, 3–4 fields) — is added purely so the same discovery loop and driver can be run against it, producing a second `/evidence/` folder that shows the system is not web-specific.

Updated default stack:

| Layer | Technology | Role |
|---|---|---|
| Orchestrator/API | FastAPI | Run lifecycle, endpoints, operator page |
| Discovery driver (both surfaces) | Anthropic Computer Use tool or OpenAI `computer-use-preview`, executed via `xdotool`/PyAutoGUI screenshots | Model-driven observe→act loop, surface-agnostic |
| Web replay engine | Playwright (Python) | Deterministic accessibility/DOM-based execution for the implemented web surface |
| Desktop replay engine (designed only) | `pywinauto` (Windows UIA) / AT-SPI (Linux) / `AXUIElement` (macOS) | Locator resolution for native controls; not required to implement |
| Data models | Pydantic v2 | Typed artifact/result schema, JSON Schema export |
| Storage | SQLite (simulator + run metadata), JSON/YAML (artifacts), JSONL (logs) | Reviewable, lightweight persistence |
| Mock web surface | FastAPI + Jinja/HTMX, iframe + table layout, no test IDs | Implemented legacy-style banking flow |
| Mock desktop surface | Tkinter or PySimpleGUI form | Minimal second target to prove cross-surface discovery |
| Testing | Pytest | Schema, policy, locator, replay, error-taxonomy tests |

## Why this change is correct

Playwright MCP is Microsoft's official server exposing browser automation over the Model Context Protocol, built specifically around Chromium/Firefox/WebKit accessibility snapshots — it has no native-window automation surface. Anthropic's Computer Use tool and OpenAI's computer-use-preview model instead give the model direct control over whatever is rendered on screen — "letting it interact with native apps and the web like a human" — by exchanging screenshots and coordinate-based actions with the calling application, which then executes them via OS input APIs. Reference and open-source implementations of this pattern run inside a virtual display and drive the mouse/keyboard with `xdotool`, and cross-platform variants use PyAutoGUI/Quartz depending on OS — the same driver code path handles a browser window and a native window without modification.

This also avoids the heavier alternative of adopting Appium for desktop coverage, which would require separate, differently-maintained drivers per platform (Mac2 driver for macOS, WinAppDriver for Windows — itself unmaintained by Microsoft since 2022 — plus a separate browser driver), each with its own setup and protocol quirks. A single OS-level computer-use driver is simpler to justify in a take-home write-up and produces a cleaner "same mechanism, two surfaces" story.

## Updated architecture

```text
CLI / FastAPI
    |
Run Orchestrator ---- Intervention Service / Operator Page
    |
    +---- Discovery Controller ---- ModelProvider (Claude Computer Use / OpenAI CUA)
    |            |
    |      OSComputerUseDriver (screenshot + xdotool/PyAutoGUI)
    |            |
    |      [ Browser window: bank_sim ]   [ Desktop window: native mock form ]
    |
    +---- Replay Engine (web) ---- PlaywrightWebAdapter
    |
    +---- Replay Engine (desktop, designed only) ---- DesktopUIAAdapter (interface only)
    |
Policy Engine ---- Capability Registry ---- Evidence Store
```

Key seam: **discovery** uses one surface-agnostic driver for both targets; **replay** uses a surface-specific adapter chosen by the artifact's `compatibility.surface_kinds` field, so the artifact schema does not need to change — only the executor selected at replay time changes.

## Updated locator strategy (replay)

Ordered locator candidates, now explicitly per surface:

**Web (implemented):**
1. Role + accessible name
2. Label + control type
3. Stable visible text + semantic ancestor/region
4. Frame path + one of the above
5. CSS/XPath attribute selector, only if shown stable across runs
6. Coordinates — lowest-confidence fallback only

**Desktop (designed, not required to implement):**
1. UI Automation / AT-SPI / AXUIElement role + name
2. Automation ID / control ID, if exposed
3. Window title + control index within a container
4. Coordinates relative to window client area — lowest-confidence fallback only

Both hierarchies converge on the same principle documented in the write-up: **acquire with a generalist, surface-agnostic driver; replay with the most stable surface-specific locator available, and fall back to coordinates only as a last resort.**

## Updated application to build

In addition to the browser-based Credit Union Ops Simulator described previously (member search → detail → open sub-account → review), add:

### Minimal native desktop mock

A small Tkinter or PySimpleGUI window with:

- A "Member ID" input field
- An "Account Type" dropdown
- A "Look Up" button
- A results panel showing a mocked balance

This does not need its own replay engine or full feature parity with the web app. Its only purpose is to be a second, genuinely different target window that the same OS-level discovery driver can operate against, producing a second discovery evidence folder (`/evidence/discovery-desktop/`) alongside the browser one (`/evidence/discovery-success/`).

### Updated evidence tree

```text
evidence/
  discovery-success/        (web surface — full capability produced here)
    capability.yaml
    events.redacted.jsonl
    final.png
    trace.zip
  discovery-desktop/        (new — proves cross-surface discovery)
    events.redacted.jsonl
    final.png
    run-summary.json
  replay-success/
  replay-member-not-found/
  handoff-unknown-dialog/
```

Only the web-surface run needs to produce a full replayable capability artifact per the core requirements. The desktop run's purpose is narrower and explicitly scoped: it demonstrates that the discovery mechanism itself is not browser-bound, which directly strengthens the Section 3.7 "surface abstraction" argument with real evidence instead of a claim.

## Updated concepts to research

Add to the original list:

| Concept | What to understand | Design decision |
|---|---|---|
| Screenshot + coordinate computer-use loop | Action vocabulary (move, click, type, scroll, screenshot), coordinate scaling/DPI handling, action latency | Wrap Claude Computer Use or OpenAI CUA behind the same `ModelProvider` interface used for any future accessibility-based provider |
| OS input automation | `xdotool` (X11) vs PyAutoGUI (cross-platform) vs Quartz (macOS) | Pick one execution backend matched to the development OS; document the others as swap-in alternatives |
| Virtual display execution | Xvfb or similar, for headless/CI-safe desktop automation | Needed if discovery must run in a container or CI runner without a physical display |
| Native accessibility APIs (design only) | UI Automation (Windows), AT-SPI (Linux), AXUIElement (macOS) | Cited in the write-up as the intended replay-time locator layer for a future desktop adapter |
| Coordinate-to-locator promotion | Converting a discovery-time click point into a stable identifier during canonicalization | For the web surface, always resolve the actual DOM/ARIA element hit at that point before saving it in the artifact — never save raw coordinates as the primary locator |

## Updated timeline note

The dual-surface addition is small and should not materially extend the schedule already discussed (target 5–7 focused days / 20–30 hours). Budget roughly 2–3 additional hours total: about 1 hour to build the Tkinter mock, and 1–2 hours to run and capture the second discovery evidence folder. Do not build a desktop replay engine — that remains a documented design extension, consistent with the brief's instruction not to implement multi-tenant or desktop support and to keep the core abstractions from painting the design into a corner.

## Updated decisions to defend

Add to the original list:

- Why the discovery driver is OS-level/pixel-based while the replay engine is surface-specific and locator-based — these are different problems with different determinism requirements.
- Why a second, unrelated desktop mock is sufficient to prove cross-surface capability without building a second full workflow or replay engine.
- Why coordinates from the computer-use driver are treated as discovery-time evidence only, and are resolved into semantic locators during canonicalization rather than saved directly into the artifact.
