"""
The demonstration runs, as tests. All four use the **same artifact**, differing only in inputs and fault
profile — that is the claim.

Needs `docker compose up -d bank-sim`. Every test here is marked `integration`.

What each one is for:

  * `success` — parameterization and determinism, on a *different member* than discovery used, with the
    declared outputs extracted and `provider: none` in the summary.
  * `MEMBER_NOT_FOUND` — a domain answer is not a crash.
  * `--fault overlay` — recoverability, with the recovery visible in the evidence.
  * `--fault dialog` — recovery from a dialog **discovery watched a human dismiss**. The canonicalizer
    turned that intervention into a `dismiss` rule, so replay handles it without asking anyone. See
    `test_the_dialog_the_plan_expected_to_escalate_is_now_handled` for why this is not the escalation the
    plan predicted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.domain.artifact import load_artifact
from src.domain.trace import Budget, Display, ProviderInfo, RunTrace
from src.evidence.writer import EvidenceWriter
from src.policy.engine import PolicyEngine
from src.policy.redaction import Redactor
from src.replay.engine import REPLAY_ACTION_KINDS, Bounds, ReplayEngine
from src.sessions.manager import SessionManager
from src.surfaces.playwright_web import BaseUrlRebind, ReplaySurface

ARTIFACT = Path("artifacts/open_subaccount_review.yaml")
BANK_SIM_URL = "http://127.0.0.1:8001"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def capability():
    return load_artifact(ARTIFACT)


@pytest.fixture
def fault_profile():
    armed: list[str] = []

    def arm(profile: str) -> None:
        httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/{profile}", timeout=10).raise_for_status()
        armed.append(profile)

    yield arm
    httpx.post(f"{BANK_SIM_URL}/dev/fault-profile/default", timeout=10)


async def replay(capability, inputs: dict[str, str], evidence_dir: Path) -> tuple[Any, Path]:
    """One replay run, through the same engine the CLI drives. Returns the result and its evidence dir."""
    rebind = BaseUrlRebind.for_capability(capability, BANK_SIM_URL)
    sensitive = {
        name: inputs[name]
        for name, spec in capability.contract.inputs.items()
        if spec.sensitive and name in inputs
    }

    with EvidenceWriter(
        evidence_dir=evidence_dir, overwrite=True, redactor=Redactor(sensitive)
    ) as writer:
        trace = RunTrace(
            run_id=writer.run_id,
            goal=f"replay {capability.capability.id}",
            target=BANK_SIM_URL,
            display=Display(width=1280, height=800),
            provider=ProviderInfo(name="none", model="none"),
            budget=Budget(),
        )
        async with ReplaySurface(rebind) as surface:
            await surface.goto(capability.entry.url)
            engine = ReplayEngine(
                surface=surface,
                policy=PolicyEngine(
                    run_id=writer.run_id,
                    allowed_origins=rebind.policy_origins,
                    declared_inputs=inputs,
                    allowed_action_kinds=REPLAY_ACTION_KINDS,
                ),
                writer=writer,
                trace=trace,
                capability=capability,
                inputs=inputs,
                session=SessionManager(writer.run_id, evidence_dir=writer.dir, adapter=surface),
                bounds=Bounds(max_steps=20, wall_clock_s=120.0),
            )
            result = await engine.run()
    return result, writer.dir


def events_of(evidence_dir: Path, kind: str) -> list[dict[str, Any]]:
    path = evidence_dir / "events.redacted.jsonl"
    return [
        event
        for line in path.read_text().splitlines()
        if (event := json.loads(line)).get("event", event.get("kind")) == kind
    ]


# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "account_type, amount",
    [("savings", "50.00"), ("checking", "125.50")],
)
async def test_the_artifact_replays_for_inputs_discovery_never_saw(
    capability, tmp_path, account_type, amount
):
    """Member `23456`, not the `12345` discovery used, and both account types.

    The whole point of a contract: the artifact is a capability with parameters, not a recording of one
    session. The review panel is checked directly, because an extracted value that agrees with a checkpoint
    that reads the same region could still both be wrong about what the app did.
    """
    inputs = {"member_id": "23456", "account_type": account_type, "opening_amount": amount}
    result, evidence = await replay(capability, inputs, tmp_path / "success")

    assert result.status == "success", getattr(result, "observed", result)
    assert result.checkpoint_verified is True

    review = result.outputs["review"]
    assert review["account_type"].lower() == account_type
    assert review["opening_amount"] == amount
    assert review["member"] == "***56", "the sensitive output must come back masked"
    assert "23456" not in review["member"]


async def test_the_run_summary_records_that_no_model_was_called(capability, tmp_path):
    """The zero-model claim as data, in the same summary format discovery writes. A reviewer comparing the
    two folders sees `anthropic/claude-opus-5` against `none`, and zero tokens."""
    inputs = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}
    _, evidence = await replay(capability, inputs, tmp_path / "summary")

    summary = json.loads((evidence / "run-summary.json").read_text())
    assert summary["provider"]["name"] == "none"
    assert summary["budget"]["input_tokens"] == 0
    assert summary["budget"]["output_tokens"] == 0
    assert summary["human_steps"] == 0


async def test_a_sensitive_input_does_not_reach_the_evidence(capability, tmp_path):
    """Same redactor discovery uses, wired to the artifact's `sensitive: true` inputs."""
    inputs = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}
    _, evidence = await replay(capability, inputs, tmp_path / "redaction")

    for name in ("trace.yaml", "events.redacted.jsonl", "run-summary.json"):
        assert "23456" not in (evidence / name).read_text(), f"{name} leaked the member id"


async def test_an_unseeded_member_is_a_business_outcome(capability, tmp_path):
    """`88888` is not seeded. `99999` is a trap — despite being named `Test, NotFound` it *is* seeded and
    would not produce this path."""
    inputs = {"member_id": "88888", "account_type": "savings", "opening_amount": "50.00"}
    result, _ = await replay(capability, inputs, tmp_path / "not-found")

    assert result.status == "business_outcome"
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.steps_used < len(capability.steps), "it should stop, not walk the rest of the form"


async def test_a_loading_overlay_is_waited_out_and_recorded(capability, tmp_path, fault_profile):
    """`fp_overlay` holds the response for 1200ms. The rule's budget is 4 x 500ms — it was 2 x 500ms, which
    is 1000ms, and every run under this profile escalated 200ms short of clearing."""
    fault_profile("overlay")
    inputs = {"member_id": "23456", "account_type": "savings", "opening_amount": "50.00"}
    result, evidence = await replay(capability, inputs, tmp_path / "recovery")

    assert result.status == "success", getattr(result, "observed", result)
    waits = [e for e in events_of(evidence, "recovery") if e["strategy"] == "wait_and_retry"]
    assert waits, "the recovery must be in the evidence, not just in the outcome"
    assert all(e["cleared"] for e in waits)
    # Three 500ms looks to clear a 1200ms delay, which is why two was not enough.
    assert max(e["attempts"] for e in waits) >= 3


async def test_a_dialog_discovery_learned_to_dismiss_is_dismissed(capability, tmp_path, fault_profile):
    """The loop closing.

    A human dismissed `System Notice` during discovery. The canonicalizer read that intervention, named the
    dialog, and wrote a `dismiss` rule with an `else: escalated` for when dismissal stops working. So replay
    clears it with nobody watching — and the run succeeds where discovery needed a person.
    """
    fault_profile("dialog")
    inputs = {"member_id": "34567", "account_type": "savings", "opening_amount": "75.00"}
    result, evidence = await replay(capability, inputs, tmp_path / "dialog")

    assert result.status == "success", getattr(result, "observed", result)
    dismissals = [e for e in events_of(evidence, "recovery") if e["strategy"] == "dismiss"]
    assert dismissals and dismissals[0]["cleared"]
    assert "System Notice" in dismissals[0]["detail"]


async def test_the_dialog_the_plan_expected_to_escalate_is_now_handled(capability):
    """Recorded as a finding rather than asserted as a gap.

    `_review_panel.html` says its dialog is "deliberately NOT in the capability artifact's known dialogs —
    the replay engine cannot resolve it, which is what forces an escalation to a human". That stopped being
    true: discovery *observed* the dialog and a human dismissing it, so `System Notice` is in
    `named_dialogs` and the artifact handles it.

    Which means the live escalation path has nothing to fire on — the simulator has exactly one dialog and
    the artifact now knows it. The `unmatched` rule is real and tested offline
    (`test_an_unnamed_dialog_escalates_rather_than_proceeding`), but demonstrating it end to end needs a
    dialog the artifact has never seen.
    """
    assert capability.named_dialogs == ("System Notice",)
    dismiss_rules = [
        r for r in capability.outcome_rules if r.recover and r.recover.strategy == "dismiss"
    ]
    assert dismiss_rules, "discovery's human intervention should have become a dismiss rule"
    assert dismiss_rules[0].else_ is not None, "and a fallback for when dismissal stops working"
