"""
Command line for the discovery layer.

`drive` is the important one right now: it walks the simulator with hardcoded
coordinates and no model at all. That exists so the first real discovery run has one
fewer unknown — when the model does something strange in Step 14, `drive` answers
"do the hands work?" in ten seconds without spending a token.

Run any of these with the stack up (`docker compose up -d`):

    poetry run python -m src.cli sandbox-status
    poetry run python -m src.cli drive
    poetry run python -m src.cli fault set overlay
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import typer

from src.domain.actions import Key, LeftClick, Type
from src.surfaces.base import SurfaceError, SurfaceUnavailable
from src.surfaces.x11_computer import (
    DEFAULT_AGENT_URL,
    DEFAULT_NOVNC_URL,
    X11ComputerAdapter,
)

# The sim is published to the host for operator use only; the agent never sees it.
BANK_SIM_URL = "http://127.0.0.1:8001"

# Coordinates of the search form on the 1280x800 display. Hardcoded on purpose: this
# command proves the plumbing, and discovering these is the model's job, not ours.
# The outer shell's left nav. Clicking "Members" reloads the search page into the
# iframe — the app's own way back to a known state, using only allowed actions and no
# URL entry (which the agent deliberately cannot do).
MEMBERS_NAV_LINK = (46, 58)
MEMBER_ID_FIELD = (550, 96)
SEARCH_RESULT_ROW = (360, 148)
# Member Detail renders two identical "Back" buttons. Probing one shows the ambiguity
# signal the canonicalizer needs, so the smoke test demonstrates it rather than
# leaving it to be discovered during a paid run.
DETAIL_BACK_BUTTON = (233, 239)

app = typer.Typer(
    add_completion=False,
    help="Discovery loop CLI: drive the sandbox, check it, switch fault profiles.",
)
fault_app = typer.Typer(help="Fault profiles (operator-only; the agent cannot reach these).")
app.add_typer(fault_app, name="fault")


def _echo_header(title: str) -> None:
    typer.secho(f"\n{title}", fg=typer.colors.CYAN, bold=True)


@app.command("sandbox-status")
def sandbox_status(agent_url: str = DEFAULT_AGENT_URL) -> None:
    """Health of the surface agent, the display, and the browser behind it."""

    async def _run() -> None:
        async with X11ComputerAdapter(agent_url) as surface:
            health = await surface.health()
            display = health.get("display", {})
            typer.echo(f"agent      : {health.get('agent_version')} (up {health.get('uptime_s')}s)")
            typer.echo(f"display    : {display.get('width')}x{display.get('height')} "
                       f"on {display.get('name')}")
            typer.echo(f"chromium   : {'alive' if health.get('chromium_alive') else 'DEAD'}")
            typer.echo(f"cdp        : {health.get('cdp', {}).get('browser') or 'unreachable'}")
            typer.echo(f"scale      : {surface.scale} (screenshot px per display px)")
            typer.echo(f"noVNC      : {DEFAULT_NOVNC_URL}")

    _run_or_exit(_run())


@fault_app.command("set")
def fault_set(profile: str) -> None:
    """Switch the simulator's fault profile: default | overlay | dialog | session | tenant_b.

    Deliberately a host-side call. `/dev/*` is reachable on the network but the agent
    has no way to navigate there, and the policy engine denies the route — test
    conditions are set by the operator, never by the thing under test.
    """
    try:
        response = httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/{profile}", timeout=10)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        typer.secho(f"could not set fault profile: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"fault profile -> {response.json().get('active_profile')}", fg=typer.colors.GREEN)


@app.command()
def drive(
    agent_url: str = DEFAULT_AGENT_URL,
    member_id: str = "12345",
    out: Path = Path("evidence/drive"),
) -> None:
    """Model-free walkthrough: search for a member and open their detail page.

    Proves the whole chain — host → relay → agent → xdotool → Chromium → the sim —
    before any of it is asked to work under a model's direction.
    """

    async def _run() -> None:
        typer.secho(f"watch it live: {DEFAULT_NOVNC_URL}", fg=typer.colors.BRIGHT_BLACK)

        async with X11ComputerAdapter(agent_url, evidence_dir=out) as surface:
            health = await surface.health()
            display = health.get("display", {})
            _echo_header("1. surface")
            typer.echo(f"   {display.get('width')}x{display.get('height')}, scale {surface.scale}")

            # The sandbox is stateful: Chromium keeps whatever page the last run left
            # behind. Without this reset, `drive` would appear to fail on a second
            # invocation for reasons that have nothing to do with the plumbing it is
            # meant to be testing.
            _echo_header("2. reset to a known screen")
            await surface.act(LeftClick(coordinate=MEMBERS_NAV_LINK))
            before = await surface.observe(label="01-before")
            if "Member Search" not in before.headings:
                typer.secho(
                    f"   expected the search page, found {before.headings}",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            typer.echo(f"   {before.main_frame_url}")
            typer.echo(f"   headings: {before.headings}")
            typer.echo(f"   screenshot: {before.screenshot}")

            # Probed here, while the field is still on screen: this is the ladder
            # degrading gracefully, which is the whole reason the probe exists.
            _echo_header("3. probe the Member ID field (before navigating away)")
            field = await surface.probe(*MEMBER_ID_FIELD)
            typer.echo(f"   <{field.tag}> role={field.role} name={field.accessible_name!r}"
                       f"  <- no accessible name, by the app's design")
            typer.echo(f"   nearby_label    : {field.nearby_label!r}")
            typer.echo(f"   dom_id          : {field.dom_id} ({field.dom_id_stability})")
            for candidate in field.candidates:
                typer.echo(f"     - {candidate.kind:<16} match_count={candidate.match_count}")

            _echo_header("4. act (click, type, Return)")
            for action in (
                LeftClick(coordinate=MEMBER_ID_FIELD),
                Type(text=member_id),
                Key(text="Return"),
            ):
                result = await surface.act(action)
                typer.echo(f"   {action.kind:<14} settled in {result.get('settled_ms')}ms")
            await surface.act(LeftClick(coordinate=SEARCH_RESULT_ROW))
            typer.echo("   left_click     opened the result row")

            _echo_header("5. after")
            after = await surface.observe(label="02-after")
            typer.echo(f"   {after.frame_urls[-1] if after.frame_urls else '?'}")
            typer.echo(f"   headings: {after.headings}")
            typer.echo(f"   screenshot: {after.screenshot}")

            # Member Detail has two identical Back buttons. Every candidate should
            # report match_count 2 — the signal that says "do not use this locator
            # without more context" rather than silently picking the first match.
            _echo_header("6. probe an ambiguous control (two 'Back' buttons)")
            back = await surface.probe(*DETAIL_BACK_BUTTON)
            typer.echo(f"   <{back.tag}> role={back.role} name={back.accessible_name!r}")
            for candidate in back.candidates:
                flag = "  <- ambiguous" if (candidate.match_count or 0) > 1 else ""
                typer.echo(
                    f"     - {candidate.kind:<16} match_count={candidate.match_count}{flag}"
                )
            typer.echo(f"   is_ambiguous    : {back.is_ambiguous}")

            # The screen must actually have changed; identical hashes mean the clicks
            # went nowhere, which is exactly the failure this command exists to catch.
            _echo_header("result")
            changed = before.observation_hash != after.observation_hash
            found = member_id in (after.visible_text or "")
            typer.secho(
                f"   screen changed: {changed}   member {member_id} on screen: {found}",
                fg=typer.colors.GREEN if (changed and found) else typer.colors.RED,
            )
            if not (changed and found):
                raise typer.Exit(1)

    _run_or_exit(_run())


def _run_or_exit(coro: Any) -> None:
    """Turn surface errors into a readable message and a non-zero exit, rather than a
    traceback that buries the one line that matters."""
    try:
        asyncio.run(coro)
    except SurfaceUnavailable as exc:
        typer.secho(f"\nsandbox unreachable: {exc.message}", fg=typer.colors.RED, bold=True)
        typer.echo("  is the stack up?  docker compose up -d && docker compose ps")
        raise typer.Exit(1) from exc
    except SurfaceError as exc:
        typer.secho(f"\n{exc}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    app()
