"""
Command line for the discovery layer.

`drive` is the important one right now: it walks the simulator with hardcoded
coordinates and no model at all. That exists so the first real discovery run has one
fewer unknown — when the model does something strange in Step 14, `drive` answers
"do the hands work?" in ten seconds without spending a token.

Run any of these with the stack up (`docker compose up -d`):

    poetry run python -m src.cli sandbox up          # docker compose up -d --wait
    poetry run python -m src.cli sandbox status
    poetry run python -m src.cli drive               # model-free, ~10s, free
    poetry run python -m src.cli discover --provider fake   # the whole loop, free
    poetry run python -m src.cli discover            # the whole loop, for real
    poetry run python -m src.cli fault set overlay
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import typer

from datetime import datetime, timezone

from src.domain.actions import Key, LeftClick, Type, Wait
from src.domain.results import StopReason, Success
from src.domain.trace import Display, ProviderInfo, RunTrace
from src.discovery.controller import Budget, Checkpoint, DiscoveryController
from src.discovery.model_provider import (
    ActionOutcome,
    AnthropicComputerUseProvider,
    FakeProvider,
)
from src.discovery.prompts import system_prompt
from src.evidence.writer import EvidenceDirNotEmpty, EvidenceWriter
from src.policy.engine import PolicyEngine
from src.policy.redaction import Redactor
from src.sessions.manager import SessionManager
from src.sessions.ownership import OwnershipError, Owner
from src.surfaces.base import SurfaceError, SurfaceUnavailable
from src.surfaces.x11_computer import (
    DEFAULT_AGENT_URL,
    DEFAULT_NOVNC_URL,
    X11ComputerAdapter,
)

EVIDENCE_ROOT = Path("evidence")

# How to invoke this CLI, for the instructions it prints mid-run. Spelled out with
# `poetry run` because that is how the project is run everywhere else, and a bare
# `python` is not on PATH in a normal shell here — the first live handoff failed on
# exactly that, with the run parked and the clock going.
CLI = "poetry run python -m src.cli"

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
# The sub-account form and the review panel it swaps in below itself.
OPEN_SUB_ACCOUNT_BUTTON = (757, 215)
OPENING_AMOUNT_FIELD = (739, 124)
DISCLOSURE_CHECKBOX = (197, 181)
FORM_CONTINUE_BUTTON = (695, 212)

app = typer.Typer(
    add_completion=False,
    help="Discovery loop CLI: drive the sandbox, check it, switch fault profiles.",
)
sandbox_app = typer.Typer(help="The stack: bring it up, take it down, check it.")
app.add_typer(sandbox_app, name="sandbox")
fault_app = typer.Typer(help="Fault profiles (operator-only; the agent cannot reach these).")
app.add_typer(fault_app, name="fault")
session_app = typer.Typer(help="Control transfer: take the screen from the automation and give it back.")
app.add_typer(session_app, name="session")


def _echo_header(title: str) -> None:
    typer.secho(f"\n{title}", fg=typer.colors.CYAN, bold=True)



def _stepper(
    surface: X11ComputerAdapter,
    writer: EvidenceWriter,
    policy: PolicyEngine,
    trace: RunTrace,
) -> DiscoveryController:
    """A controller used only for its per-action path — no model, no loop.

    `drive` and `discover` execute an action the same way because they execute it
    through the same method. That matters more than it looks: the per-action path is
    where the two policy phases and the probe live, and a second copy of it here would
    drift from the real one exactly when it mattered.
    """
    return DiscoveryController(
        surface=surface,
        provider=None,
        policy=policy,
        writer=writer,
        trace=trace,
    )


@sandbox_app.command("up")
def sandbox_up(wait: bool = typer.Option(True, help="Block until the sandbox reports healthy.")) -> None:
    """Start the stack (`docker compose up -d`) and wait for it to be usable."""
    _compose("up", "-d", *(("--wait",) if wait else ()))
    typer.secho("stack up", fg=typer.colors.GREEN)
    typer.echo(f"  watch it: {DEFAULT_NOVNC_URL}")
    typer.echo(f"  the app : {BANK_SIM_URL}")


@sandbox_app.command("down")
def sandbox_down(
    volumes: bool = typer.Option(False, help="Also remove volumes."),
) -> None:
    """Stop the stack. Evidence on disk is untouched — it lives on the host."""
    _compose("down", *(("-v",) if volumes else ()))
    typer.secho("stack down", fg=typer.colors.GREEN)


@sandbox_app.command("status")
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
            typer.echo(f"fault      : {_active_fault_profile() or 'unreachable'}")
            typer.echo(f"noVNC      : {DEFAULT_NOVNC_URL}")

    _run_or_exit(_run())


@app.command("sandbox-status", hidden=True)
def sandbox_status_alias(agent_url: str = DEFAULT_AGENT_URL) -> None:
    """Deprecated alias for `sandbox status`."""
    sandbox_status(agent_url)


def _compose(*args: str) -> None:
    """Run a docker compose command, surfacing its output as it happens."""
    import subprocess

    command = ["docker", "compose", *args]
    typer.secho(f"$ {' '.join(command)}", fg=typer.colors.BRIGHT_BLACK)
    try:
        result = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        typer.secho("docker not found on PATH — is Docker Desktop installed?", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


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


@fault_app.command("show")
def fault_show() -> None:
    """What is currently armed."""
    profile = _active_fault_profile()
    if profile is None:
        typer.secho("could not reach the simulator — is the stack up?", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(profile)


def _active_fault_profile() -> str | None:
    """Read the armed profile, so a run records the conditions it ran under.

    Returns None rather than raising: not knowing the fault profile is a reason to leave
    the field empty in the trace, not a reason to abandon the run.
    """
    try:
        response = httpx.get(f"{BANK_SIM_URL}/dev/fault-profile", timeout=5)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    # The GET returns the profile object (`name`); the POST returns `active_profile`.
    # Reading only the latter reported every run as "unreachable".
    return payload.get("name") or payload.get("active_profile")


@app.command()
def drive(
    agent_url: str = DEFAULT_AGENT_URL,
    member_id: str = "12345",
    evidence_dir: Path | None = typer.Option(
        None, help="Write here instead of evidence/run_<timestamp>/ (Step 14 uses this)."
    ),
    overwrite: bool = typer.Option(
        False, help="Replace --evidence-dir if it already holds a run. Refuses otherwise."
    ),
) -> None:
    """Model-free walkthrough: search for a member and open their detail page.

    Proves the whole chain — host → relay → agent → xdotool → Chromium → the sim —
    before any of it is asked to work under a model's direction, and produces a real
    evidence folder while doing it: the same writer, trace and policy path the model
    driven loop will use, minus the model.
    """

    async def _run() -> None:
        # The writer first, so a folder collision is reported before anything that looks
        # like the run has started.
        declared = {"member_id": member_id}
        writer = EvidenceWriter(
            evidence_dir=evidence_dir, redactor=Redactor(declared), overwrite=overwrite
        )
        typer.secho(f"watch it live: {DEFAULT_NOVNC_URL}", fg=typer.colors.BRIGHT_BLACK)
        policy = PolicyEngine(run_id=writer.run_id, declared_inputs=declared)
        trace = RunTrace(
            run_id=writer.run_id,
            goal=f"Find member {member_id} and open their detail page (model-free drive)",
            target="http://bank-sim:8001/",
            display=Display(width=1280, height=800),
            provider=ProviderInfo(name="none", model="model-free drive"),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        writer.event("run_started", goal=trace.goal, target=trace.target)

        async with X11ComputerAdapter(agent_url) as surface:
            step_through = _stepper(surface, writer, policy, trace)
            health = await surface.health()
            display = health.get("display", {})
            _echo_header("1. surface")
            typer.echo(f"   {display.get('width')}x{display.get('height')}, scale {surface.scale}")

            # The sandbox is stateful: Chromium keeps whatever page the last run left
            # behind. Without this reset, `drive` would appear to fail on a second
            # invocation for reasons that have nothing to do with the plumbing it is
            # meant to be testing.
            _echo_header("2. reset to a known screen")
            await step_through.execute_action(
                LeftClick(coordinate=MEMBERS_NAV_LINK),
                model_reason="reset to a known screen via the app's own nav",
            )
            before = await surface.observe()
            before.screenshot = writer.screenshot("01-before", surface.last_screenshot_png)
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
                step = await step_through.execute_action(
                    action, model_reason="search for the requested member"
                )
                typer.echo(f"   {action.kind:<14} settled in {step.timing.settled_ms}ms")
            await step_through.execute_action(
                LeftClick(coordinate=SEARCH_RESULT_ROW),
                model_reason="open the result row to reach Member Detail",
            )
            typer.echo("   left_click     opened the result row")

            _echo_header("5. after")
            after = await surface.observe()
            after.screenshot = writer.screenshot("02-after", surface.last_screenshot_png)
            typer.echo(f"   {after.frame_urls[-1] if after.frame_urls else '?'}")
            typer.echo(f"   headings: {after.headings}")
            typer.echo(f"   screenshot: {after.screenshot}")

            # Member Detail has two identical Back buttons. Every candidate should
            # report match_count 2 — the signal that says "do not use this locator
            # without more context" rather than silently picking the first match.
            #
            # Recorded as a step rather than probed for display. An earlier version
            # called surface.probe() directly here and printed the result, which meant
            # the one signal §7 calls the reason the trace is worth having appeared in
            # no artifact at all — only in a terminal someone had to be watching.
            _echo_header("6. record an ambiguous control (two 'Back' buttons)")
            step = await step_through.execute_action(
                LeftClick(coordinate=DETAIL_BACK_BUTTON),
                model_reason="probe an intentionally duplicated control",
            )
            back = step.probe
            typer.echo(f"   <{back.tag}> role={back.role} name={back.accessible_name!r}")
            for candidate in back.candidates:
                flag = "  <- ambiguous" if (candidate.match_count or 0) > 1 else ""
                typer.echo(
                    f"     - {candidate.kind:<16} match_count={candidate.match_count}{flag}"
                )
            typer.echo(f"   is_ambiguous    : {back.is_ambiguous}")
            typer.echo(f"   recorded as     : step {step.index} in trace.yaml")

            # The screen must actually have changed; identical hashes mean the clicks
            # went nowhere, which is exactly the failure this command exists to catch.
            _echo_header("result")
            changed = before.observation_hash != after.observation_hash
            found = member_id in (after.visible_text or "")

            # Set on the result itself, not only on the summary: a reader inspecting
            # trace.outcome.stop_reason should see the same answer for `drive` as for
            # `discover`, or the two runs are not comparable.
            verified = bool(changed and found)
            stop_reason = StopReason.CHECKPOINT_VERIFIED if verified else StopReason.NO_PROGRESS
            run_dir = writer.finish(
                trace,
                Success(
                    run_id=writer.run_id,
                    steps_used=len(trace.steps),
                    checkpoint_verified=verified,
                    stop_reason=stop_reason,
                ),
                final_png=await surface.capture_evidence(),
                stop_reason=stop_reason,
            )
            typer.echo(f"   evidence: {run_dir}")
            typer.secho(
                f"   screen changed: {changed}   member {member_id} on screen: {found}",
                fg=typer.colors.GREEN if (changed and found) else typer.colors.RED,
            )
            if not (changed and found):
                raise typer.Exit(1)

    _run_or_exit(_run())


@session_app.command("status")
def session_status(run_id: str) -> None:
    """Who holds the screen, at what control version, and why."""
    manager = _resolve_session(run_id)
    state = manager.state
    typer.echo(f"run          : {state.run_id}")
    typer.secho(
        f"owner        : {state.owner}",
        fg=typer.colors.YELLOW if not state.automation_may_act else typer.colors.GREEN,
    )
    typer.echo(f"version      : {state.control_version}")
    typer.echo(f"reason       : {state.reason or '-'}")
    typer.echo(f"operator     : {state.operator or '-'}")
    typer.echo(f"intervention : {manager.intervention_id or '-'}")
    if not state.automation_may_act:
        typer.echo(f"take over at : {manager.novnc_url}")
    typer.echo("\ntransitions:")
    for entry in state.audit:
        who = f" by {entry.operator}" if entry.operator else ""
        typer.echo(
            f"  v{entry.from_version}->v{entry.to_version}  "
            f"{entry.from_owner} -> {entry.to_owner}{who}  ({entry.at})"
        )


@session_app.command("accept")
def session_accept(
    run_id: str,
    operator: str = typer.Option("operator", help="Who is taking control."),
    control_version: int | None = typer.Option(
        None, help="Version you believe is current; omit to use the one on disk."
    ),
) -> None:
    """Take the screen. Note this does NOT resume automation — that is `resume`."""
    manager = _resolve_session(run_id)
    version = control_version if control_version is not None else manager.state.control_version
    _transition_or_exit(
        lambda: manager.accept(operator=operator, control_version=version),
        "you now hold the screen; the run stays parked until you `resume`",
        manager,
    )


@session_app.command("resume")
def session_resume(
    run_id: str,
    control_version: int | None = typer.Option(None),
) -> None:
    """Hand control back. The loop re-observes before acting."""
    manager = _resolve_session(run_id)
    version = control_version if control_version is not None else manager.state.control_version
    _transition_or_exit(
        lambda: asyncio.run(manager.resume(control_version=version)),
        "automation resumed",
        manager,
    )


@session_app.command("complete")
def session_complete(
    run_id: str,
    operator: str = typer.Option("operator"),
    control_version: int | None = typer.Option(None),
) -> None:
    """Finish the run by hand. Terminal — it cannot be reopened."""
    manager = _resolve_session(run_id)
    version = control_version if control_version is not None else manager.state.control_version
    _transition_or_exit(
        lambda: manager.complete(operator=operator, control_version=version),
        "run marked complete",
        manager,
    )


@session_app.command("cancel")
def session_cancel(
    run_id: str,
    operator: str = typer.Option("operator"),
    control_version: int | None = typer.Option(None),
) -> None:
    """Abandon the run. Releases anything parked so the loop can exit."""
    manager = _resolve_session(run_id)
    version = control_version if control_version is not None else manager.state.control_version
    _transition_or_exit(
        lambda: manager.cancel(operator=operator, control_version=version),
        "run cancelled",
        manager,
    )


def _parked_runs() -> list[tuple[str, Path]]:
    """Every intervention file under evidence/, as (run_id, directory)."""
    found: list[tuple[str, Path]] = []
    for path in sorted(EVIDENCE_ROOT.glob("*/intervention.json")):
        try:
            run_id = json.loads(path.read_text()).get("run_id")
        except (OSError, json.JSONDecodeError):
            continue
        if run_id:
            found.append((run_id, path.parent))
    return found


def _resolve_session(ref: str) -> SessionManager:
    """Find a parked run by id, or by the folder it happens to live in.

    The run id is the identity; the folder name is incidental. An earlier version
    assumed `evidence/<run_id>/`, which is true only while nobody passes
    `--evidence-dir` — and the capture procedure in the README passes it on every run.
    A capture that parked for a human therefore could not be handed back, reporting
    "no intervention found" about a path that was never going to exist.
    """
    # 1. A path, given as one.
    candidate = Path(ref)
    if candidate.is_dir() or "/" in ref:
        if not (candidate / "intervention.json").exists():
            _no_such_session(ref, f"{candidate}/intervention.json does not exist")
        return SessionManager.load(_run_id_in(candidate) or ref, evidence_dir=candidate)

    # 2. The folder named after the run — `handoff-demo`, and any run without
    #    --evidence-dir.
    direct = EVIDENCE_ROOT / ref
    if (direct / "intervention.json").exists():
        return SessionManager.load(ref, evidence_dir=direct)

    # 3. The run id recorded INSIDE an intervention file, wherever that file lives.
    for run_id, directory in _parked_runs():
        if run_id == ref:
            return SessionManager.load(run_id, evidence_dir=directory)

    _no_such_session(ref, "no run with that id has an intervention on disk")


def _no_such_session(ref: str, why: str) -> None:
    """Fail with the list of runs that *can* be taken.

    The previous message pointed at `handoff-demo` regardless of what was running, which
    is unhelpful precisely when someone is mid-handoff and the clock is going.
    """
    typer.secho(f"cannot find session {ref!r}: {why}", fg=typer.colors.RED, bold=True)
    parked = _parked_runs()
    if not parked:
        typer.echo(f"  nothing under {EVIDENCE_ROOT}/ has an intervention.json.")
        typer.echo("  (is a run actually parked? it prints the command to use when it parks)")
    else:
        typer.echo("\n  runs you can take control of:")
        for run_id, directory in parked:
            typer.echo(f"    {CLI} session accept {run_id}       # in {directory}")
    raise typer.Exit(1)


def _run_id_in(directory: Path) -> str | None:
    try:
        return json.loads((directory / "intervention.json").read_text()).get("run_id")
    except (OSError, json.JSONDecodeError):
        return None


def _transition_or_exit(action: Any, message: str, manager: SessionManager) -> None:
    """Turn a refused transfer into a readable line rather than a traceback.

    A stale version is the interesting failure: it means someone else moved control
    while you were deciding, and the right response is to re-read, not to retry.
    """
    try:
        action()
    except OwnershipError as exc:
        typer.secho(f"refused: {exc}", fg=typer.colors.RED, bold=True)
        typer.echo(f"  current owner is {manager.state.owner} at v{manager.state.control_version}")
        raise typer.Exit(1) from exc
    typer.secho(
        f"owner={manager.state.owner}  version={manager.state.control_version}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  {message}")



@app.command()
def smoke(
    agent_url: str = DEFAULT_AGENT_URL,
    goal: str = (
        "Find member 12345 and prepare a savings sub-account, opening amount 25.00; "
        "stop at review"
    ),
    dry_run: bool = typer.Option(False, help="Print the request shape without sending it."),
    round_trip: bool = typer.Option(
        False,
        help="Also send the tool_results back, proving the result-block shape (one extra call).",
    ),
) -> None:
    """One real API call: send a screenshot, print what the model asks for, execute NOTHING.

    This exists to prove the request shape while exactly one action is in flight. The
    current computer-use API rejects several fields that older references still show,
    and a 400 discovered here costs a fraction of a cent; discovered during a paid
    multi-step run it costs the run.
    """

    async def _run() -> None:
        provider = AnthropicComputerUseProvider(goal)

        if dry_run:
            import json

            _echo_header("tools")
            typer.echo(json.dumps(provider.tool_definitions(), indent=2)[:1200])
            _echo_header("request")
            kwargs = {k: v for k, v in provider.request_kwargs().items()
                      if k not in {"tools", "system"}}
            typer.echo(json.dumps(kwargs, indent=2))
            _echo_header("system prompt (first 600 chars)")
            typer.echo(system_prompt(goal)[:600])
            return

        async with X11ComputerAdapter(agent_url) as surface:
            _echo_header("1. what the model will see")
            observation = await surface.observe()
            png = surface.last_screenshot_png
            typer.echo(f"   {observation.main_frame_url}")
            typer.echo(f"   headings: {observation.headings}")
            typer.echo(f"   screenshot: {len(png or b'')} bytes")

            _echo_header("2. one request to claude-opus-5")
            batch = await provider.propose(observation, png)

            _echo_header("3. what it asked for (NOT executed)")
            for action in batch.actions:
                typer.secho(f"   {action.model_dump(mode='json')}", fg=typer.colors.GREEN)
            for invalid in batch.invalid:
                typer.secho(f"   invalid: {invalid.name} -> {invalid.reason}", fg=typer.colors.RED)
            if batch.reason:
                typer.echo(f"   said: {batch.reason[:200]}")

            if round_trip:
                # The request direction is only half the shape. Result blocks are the
                # other 400 risk (§4: a result missing toolset_name is rejected), and
                # they are only exercised by sending them. Synthetic outcomes — still
                # nothing executed against the screen.
                _echo_header("4. send results back (synthetic; still nothing executed)")
                outcomes = [
                    ActionOutcome(
                        tool_use_id=tool_use_id,
                        ok=True,
                        detail="(smoke test: not executed)",
                        image_png=png if action.kind in {"screenshot", "zoom"} else None,
                    )
                    for action, tool_use_id in zip(batch.actions, batch.tool_use_ids)
                ]
                provider.record_results(outcomes)
                blocks = provider.messages[-1]["content"]
                tagged = sum(1 for b in blocks if "toolset_name" in b)
                typer.echo(f"   {len(blocks)} result blocks, {tagged} carrying toolset_name")

                second = await provider.propose(observation, png)
                typer.secho(
                    "   accepted — result-block shape is valid", fg=typer.colors.GREEN
                )
                for action in second.actions:
                    typer.echo(f"   next: {action.model_dump(mode='json')}")

            _echo_header("5. cost" if round_trip else "4. cost")
            usage = provider.usage
            typer.echo(f"   in {usage.input_tokens:,}  out {usage.output_tokens:,}"
                       f"  ~${usage.usd_estimate:.4f}  ({usage.turns} turn(s))")
            typer.echo(f"   cache: {usage.cache_read_tokens:,} read, "
                       f"{usage.cache_creation_tokens:,} written"
                       f"  ({usage.cache_hit_rate:.0%} of billed input)")
            typer.secho("\n   nothing was executed against the screen.",
                        fg=typer.colors.BRIGHT_BLACK)

    _run_or_exit(_run())


DEFAULT_TARGET = "http://bank-sim:8001/"
FAKE_SCRIPT = Path("tests/fixtures/scripts/full_workflow.yaml")


@app.command()
def discover(
    goal: str | None = typer.Option(
        None, help="What to accomplish. Default: built from the declared inputs below."
    ),
    target: str = typer.Option(DEFAULT_TARGET, help="Recorded in the trace as the run's target."),
    provider: str = typer.Option(
        "anthropic",
        help="'anthropic' spends money; 'fake' replays --script against the real sandbox for free.",
    ),
    script: Path = typer.Option(
        FAKE_SCRIPT, help="Action script for --provider fake.", show_default=False
    ),
    fault: str | None = typer.Option(
        None, help="Arm a fault profile first: default | overlay | dialog | session | tenant_b."
    ),
    agent_url: str = DEFAULT_AGENT_URL,
    member_id: str = "12345",
    account_type: str = "savings",
    opening_amount: str = "25.00",
    max_steps: int = typer.Option(
        20, help="Hard stop. Keep this low on the first runs — each step costs a real call."
    ),
    max_usd: float = typer.Option(2.0, help="Stop when the estimated spend passes this."),
    wall_clock_s: float = typer.Option(600.0, help="Stop after this many seconds."),
    max_handoffs: int = typer.Option(3, help="Stop after this many human interventions."),
    evidence_dir: Path | None = typer.Option(
        None,
        help="Write here instead of evidence/run_<timestamp>/ — use it to name a keeper run.",
    ),
    overwrite: bool = typer.Option(
        False, help="Replace --evidence-dir if it already holds a run. Refuses otherwise."
    ),
    reset: bool = typer.Option(
        True, help="Click 'Members' first. The sandbox keeps whatever page the last run left."
    ),
) -> None:
    """The real thing: Claude drives the simulator until the loop stops it.

    Every ending is named. `CHECKPOINT_VERIFIED` means it reached the review screen and
    the screen agreed; `MAX_STEPS_EXCEEDED` means the loop bounded itself, which is a
    correct outcome and not a crash. The evidence folder is written either way — that is
    the deliverable, not the exit code.

    `--provider fake` runs the identical loop against the identical sandbox with a
    scripted set of actions instead of a model. Everything below the provider behaves the
    same, so it rehearses the whole path — policy, probe, evidence, checkpoint — for
    nothing and with no API key.
    """
    if provider not in {"anthropic", "fake"}:
        typer.secho(f"unknown provider {provider!r}: use 'anthropic' or 'fake'", fg=typer.colors.RED)
        raise typer.Exit(2)
    if provider == "fake" and not script.exists():
        typer.secho(f"no such script: {script}", fg=typer.colors.RED)
        raise typer.Exit(2)

    # Before the run, never during it: a test condition set by the thing under test would
    # not be a test condition.
    if fault is not None:
        fault_set(fault)

    async def _run() -> None:
        declared = {
            "member_id": member_id,
            "account_type": account_type,
            "opening_amount": opening_amount,
        }
        run_goal = goal or (
            f"Find member {member_id} and prepare a {account_type} sub-account with an "
            f"opening amount of {opening_amount}. Stop at the review screen — do NOT "
            f"submit the account."
        )

        # Constructed before the banner: a folder collision should be reported before
        # anything that looks like the run has started.
        writer = EvidenceWriter(
            evidence_dir=evidence_dir, redactor=Redactor(declared), overwrite=overwrite
        )
        typer.secho(f"watch it live: {DEFAULT_NOVNC_URL}", fg=typer.colors.BRIGHT_BLACK)
        policy = PolicyEngine(run_id=writer.run_id, declared_inputs=declared)

        if provider == "fake":
            model = FakeProvider.from_yaml(script)
            provider_info = ProviderInfo(name="fake", model=str(script))
        else:
            model = AnthropicComputerUseProvider(run_goal)
            provider_info = ProviderInfo(name="anthropic", model=model.model, tool="computer")

        trace = RunTrace(
            run_id=writer.run_id,
            goal=run_goal,
            target=target,
            display=Display(width=1280, height=800),
            provider=provider_info,
            fault_profile=fault or _active_fault_profile(),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        writer.event("run_started", goal=run_goal, target=trace.target)

        typer.echo(f"   run      : {writer.run_id}")
        typer.echo(f"   evidence : {writer.dir}")
        typer.echo(f"   provider : {provider_info.name} ({provider_info.model})")
        typer.echo(f"   fault    : {trace.fault_profile}")
        typer.echo(
            f"   bounds   : {max_steps} steps, ${max_usd:.2f}, {wall_clock_s:.0f}s, "
            f"{max_handoffs} handoffs"
        )

        async with X11ComputerAdapter(agent_url) as surface:
            manager = SessionManager(
                writer.run_id,
                evidence_dir=writer.dir,
                adapter=surface,
                novnc_url=DEFAULT_NOVNC_URL,
            )

            if reset:
                # Same reason as `drive`: without this the first observation is whatever
                # the previous run happened to leave on screen, and the model spends
                # paid steps working out where it is.
                await surface.act(LeftClick(coordinate=MEMBERS_NAV_LINK))
                await surface.act(Wait(duration=1.5))

            controller = DiscoveryController(
                surface=surface,
                provider=model,
                policy=policy,
                writer=writer,
                trace=trace,
                session=manager,
                budget=Budget(
                    max_steps=max_steps,
                    wall_clock_s=wall_clock_s,
                    max_usd=max_usd,
                    max_handoffs=max_handoffs,
                ),
                checkpoint=Checkpoint.for_review(declared),
                on_event=_event_printer(writer.run_id, writer.dir),
            )

            result = await controller.run()

        _echo_header("outcome")
        colour = {
            "success": typer.colors.GREEN,
            "business_outcome": typer.colors.CYAN,
            "escalated": typer.colors.YELLOW,
        }.get(result.status, typer.colors.RED)
        typer.secho(f"   {result.status.upper()}  stop_reason={result.stop_reason}", fg=colour,
                    bold=True)
        for field in ("code", "reason", "intervention_id", "checkpoint_verified"):
            value = getattr(result, field, None)
            if value is not None:
                typer.echo(f"   {field:<20}: {value}")
        if getattr(result, "observed", None):
            typer.echo(f"   observed            : {result.observed}")

        # The controller writes the budget onto the trace itself, before the folder is
        # closed; anything assigned here would be too late to reach disk.
        usage = model.usage
        typer.echo(f"   steps               : {len(trace.steps)}")
        if provider == "anthropic":
            typer.echo(
                f"   cost                : ~${usage.usd_estimate:.4f} over {usage.turns} turn(s)"
                f"  (cache {usage.cache_hit_rate:.0%} of billed input)"
            )
        else:
            typer.echo(f"   cost                : $0.00 — scripted, no model was called")
        denied = [
            s.policy.code
            for s in trace.steps
            if s.policy is not None and s.policy.decision != "allow"
        ]
        typer.echo(f"   policy refusals     : {denied or 'none'}")
        human = trace.human_steps()
        typer.echo(f"   human interventions : {len(human)}{f' at steps {human}' if human else ''}")
        typer.echo(f"   steps missing probe : {trace.steps_missing_probe() or 'none'}")
        typer.secho(f"\n   evidence: {writer.dir}", fg=typer.colors.GREEN)

        if result.status == "escalated":
            # Only reached for an escalation a handoff cannot clear; the recoverable
            # ones printed their instructions when they parked.
            typer.echo(f"   take over  : {DEFAULT_NOVNC_URL}")
            typer.echo(f"   then run   : {CLI} session accept {writer.run_id}")

    _run_or_exit(_run())


def _event_printer(run_id: str, evidence_dir: Path) -> Any:
    """Live commentary. A ten-minute paid run with no output is not something anyone
    should be asked to sit through — and a run that has PARKED with no output looks
    identical to one that has hung.
    """

    def emit(event: str, **fields: Any) -> None:
        if event == "proposed":
            actions = ", ".join(fields.get("actions") or []) or "(nothing)"
            typer.secho(f"\n-> {actions}", fg=typer.colors.BRIGHT_BLACK)
            if fields.get("reason"):
                typer.secho(f"   \"{str(fields['reason'])[:120]}\"", fg=typer.colors.BRIGHT_BLACK)
        elif event == "step":
            decision = fields.get("decision")
            colour = typer.colors.GREEN if decision == "allow" else typer.colors.RED
            line = f"   {fields.get('index'):>2}  {fields.get('kind'):<14} {decision}"
            if fields.get("error"):
                line += f"  {fields['error']}"
            typer.secho(line, fg=colour)
        elif event == "escalated":
            typer.secho(
                f"\n   ESCALATED  {fields.get('reason')}  "
                f"intervention={fields.get('intervention')}",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            # Printed at the moment of parking, not at the end: this is a prompt for
            # someone to act, and it is useless after the fact.
            typer.echo(f"   take over  : {DEFAULT_NOVNC_URL}")
            typer.echo(f"   then run   : {CLI} session accept {run_id} --operator you")
            typer.echo(f"   and then   : {CLI} session resume {run_id}")
            # The evidence dir is printed because it need not be named after the run.
            # When it was not, the handoff failed in the other terminal with nothing
            # on screen to explain why.
            typer.echo(f"   evidence   : {evidence_dir}")
            typer.secho("   waiting for a human (Ctrl-C to abandon)...",
                        fg=typer.colors.BRIGHT_BLACK)
        elif event == "resumed":
            typer.secho(
                f"   RESUMED  handoff #{fields.get('handoffs')} recorded as step "
                f"{fields.get('index')}  screen changed={fields.get('changed')}",
                fg=typer.colors.GREEN,
                bold=True,
            )
            for phrase in fields.get("new_text") or []:
                typer.secho(f"     + {str(phrase)[:100]}", fg=typer.colors.BRIGHT_BLACK)

    return emit


@app.command("handoff-demo")
def handoff_demo(
    agent_url: str = DEFAULT_AGENT_URL,
    member_id: str = "12345",
    run_id: str = typer.Option("handoff-demo", help="Run id; also the evidence folder."),
) -> None:
    """Drive to the review screen, hit the unexpected dialog, and park for a human.

    The model-free version of Step 11's escalation path: it proves control transfer
    end to end without spending a token. Arm the fault first:

        poetry run python -m src.cli fault set dialog
    """

    async def _run() -> None:
        evidence_dir = EVIDENCE_ROOT / run_id
        # Always replaced: evidence/handoff-demo/ is a gitignored dev aid that exists to
        # be re-run, not a capture to protect.
        writer = EvidenceWriter(run_id, evidence_dir=evidence_dir, overwrite=True)
        async with X11ComputerAdapter(agent_url) as surface:
            manager = SessionManager(
                run_id, evidence_dir=evidence_dir, adapter=surface, novnc_url=DEFAULT_NOVNC_URL
            )

            async def act(action: Any, settle: float = 1.2) -> None:
                # Before EVERY action: wait if a human holds the screen, then confirm
                # automation still owns it. Two guards, deliberately.
                await manager.barrier()
                manager.assert_automation_owns()
                await surface.act(action)
                await surface.act(Wait(duration=settle))

            _echo_header("1. drive to the review screen")
            await act(LeftClick(coordinate=MEMBERS_NAV_LINK))
            await act(LeftClick(coordinate=MEMBER_ID_FIELD), settle=0.3)
            await act(Type(text=member_id), settle=0.3)
            await act(Key(text="Return"), settle=1.5)
            await act(LeftClick(coordinate=SEARCH_RESULT_ROW), settle=1.5)
            await act(LeftClick(coordinate=OPEN_SUB_ACCOUNT_BUTTON), settle=1.5)
            await act(LeftClick(coordinate=OPENING_AMOUNT_FIELD), settle=0.3)
            await act(Type(text="25.00"), settle=0.3)
            await act(LeftClick(coordinate=DISCLOSURE_CHECKBOX), settle=0.5)
            await act(LeftClick(coordinate=FORM_CONTINUE_BUTTON), settle=2.0)

            _echo_header("2. observe")
            observation = await surface.observe()
            observation.screenshot = writer.screenshot(
                "01-before-handoff", surface.last_screenshot_png)
            typer.echo(f"   headings: {observation.headings}")
            typer.echo(f"   dialogs : {[d.text for d in observation.dialogs]}")

            if not observation.dialogs:
                typer.secho(
                    "\n   no dialog on screen — arm it first:\n"
                    "     poetry run python -m src.cli fault set dialog",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)

            _echo_header("3. escalate")
            intervention = await manager.escalate(
                "UNKNOWN_DIALOG",
                step_index=10,
                context=(observation.dialogs[0].text or "")[:200],
            )
            typer.secho(
                f"   ESCALATED  intervention={intervention}  reason=UNKNOWN_DIALOG",
                fg=typer.colors.YELLOW,
                bold=True,
            )
            typer.echo(f"   take over  : {DEFAULT_NOVNC_URL}")
            typer.echo(f"   then run   : {CLI} session accept {run_id}")
            typer.echo(f"   and later  : {CLI} session resume {run_id}")

            _echo_header("4. parked — automation has stopped")
            typer.echo("   waiting for a human. Ctrl-C to abandon.")
            await manager.barrier()

            _echo_header("5. resumed")
            after = await surface.observe()
            after.screenshot = writer.screenshot("02-after-handoff", surface.last_screenshot_png)
            typer.echo(f"   owner   : {manager.state.owner} v{manager.state.control_version}")
            typer.echo(f"   dialogs : {[d.text for d in after.dialogs]}")
            typer.echo(f"   changed : {observation.observation_hash != after.observation_hash}")
            typer.echo(f"   note to the model: {manager.resume_note()['content'][:70]}...")
            typer.secho(
                f"\n   audit trail: {evidence_dir / 'intervention.json'}",
                fg=typer.colors.GREEN,
            )

    _run_or_exit(_run())


def _run_or_exit(coro: Any) -> None:
    """Turn surface and API errors into a readable message and a non-zero exit, rather
    than a traceback that buries the one line that matters.

    Surface errors are checked first because they are the common case and already
    carry a diagnosis; anything else gets offered to the API reporter, and only a
    genuinely unrecognised exception is allowed to raise with its traceback intact.
    """
    try:
        asyncio.run(coro)
    except EvidenceDirNotEmpty as exc:
        typer.secho(f"\n{exc}", fg=typer.colors.RED, bold=True)
        typer.echo("  Nothing was run and nothing was written — this is checked first, on")
        typer.echo("  purpose, so a capture never costs money before failing.")
        raise typer.Exit(1) from exc
    except SurfaceUnavailable as exc:
        typer.secho(f"\nsandbox unreachable: {exc.message}", fg=typer.colors.RED, bold=True)
        typer.echo("  is the stack up?  docker compose up -d && docker compose ps")
        raise typer.Exit(1) from exc
    except SurfaceError as exc:
        typer.secho(f"\n{exc}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(1) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised below unless recognised
        if not _report_api_error(exc):
            raise
        raise typer.Exit(1) from exc


def _report_api_error(exc: Exception) -> bool:
    """Print an actionable message for an Anthropic API failure.

    Worth distinguishing carefully: a 400 about *credits* says nothing at all about
    whether the request shape is right, because billing is checked before the request
    is validated. Reporting both as "bad request" would hide that the shape is still
    unproven.
    """
    name = type(exc).__name__
    message = str(exc)

    if "credit balance is too low" in message:
        typer.secho("\nAnthropic API: out of credit.", fg=typer.colors.RED, bold=True)
        typer.echo("  Add credit at console.anthropic.com -> Plans & Billing, then retry.")
        typer.echo("  NOTE: billing is checked BEFORE the request is validated, so this")
        typer.echo("        tells you nothing about whether the request shape is correct.")
        return True

    if name in {"AuthenticationError", "PermissionDeniedError"}:
        typer.secho(f"\nAnthropic API: {name}.", fg=typer.colors.RED, bold=True)
        typer.echo("  Check ANTHROPIC_API_KEY in .env (and that you exported it).")
        return True

    if name == "BadRequestError":
        # This is the one the smoke run exists to catch.
        typer.secho("\nAnthropic API rejected the request shape:", fg=typer.colors.RED, bold=True)
        typer.echo(f"  {message[:400]}")
        typer.echo("\n  Check against plan §4 — the usual causes:")
        typer.echo("    * display_width_px / display_height_px / name on the toolset (rejected)")
        typer.echo("    * a tool_result missing toolset_name: 'computer'")
        typer.echo("    * a beta header that does not match the feature being used")
        return True

    if name in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
        typer.secho(f"\nAnthropic API: {name} — transient, retry.", fg=typer.colors.YELLOW)
        return True

    return False



if __name__ == "__main__":
    app()
