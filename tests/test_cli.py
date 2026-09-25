"""
CLI tests — finding a parked run, and refusing to spoil a capture folder.

Both of these are about the seam between two features that each worked alone. A capture
run writes to `--evidence-dir evidence/discovery-success/`; a handoff is taken with
`session accept <run_id>`. Nothing tested them together, and together they were broken:
the folder is not named after the run, and the lookup assumed it was. A real capture
parked for a human and could not be handed back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from src import cli
from src.evidence.writer import EvidenceDirNotEmpty, EvidenceWriter
from src.policy.redaction import Redactor

RUN_ID = "run_20260925_003244_4b61"


@pytest.fixture
def evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a throwaway evidence root."""
    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.setattr(cli, "EVIDENCE_ROOT", root)
    return root


def park(root: Path, folder: str, run_id: str) -> Path:
    """An intervention file, exactly as SessionManager writes one."""
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "intervention.json").write_text(
        json.dumps(
            {
                "intervention_id": "int_99b529ac",
                "reason": "MODEL_REQUESTED",
                "step_index": 16,
                "context": None,
                "novnc_url": "http://localhost:6080/vnc.html",
                "run_id": run_id,
                "owner": "HUMAN_PENDING",
                "control_version": 1,
                "operator": None,
                "updated_at": "2026-09-25T00:33:48+00:00",
                "transitions": [],
            }
        )
    )
    return directory


# --------------------------------------------------------------------------- #
# finding a parked run
# --------------------------------------------------------------------------- #


def test_a_run_is_found_when_its_folder_is_not_named_after_it(evidence: Path):
    """The reported bug.

    `discover --evidence-dir evidence/discovery-success` parks, prints
    `session accept run_2026...`, and the folder is called `discovery-success`. The run
    id is the identity; the folder name is incidental.
    """
    park(evidence, "discovery-success", RUN_ID)

    manager = cli._resolve_session(RUN_ID)

    assert manager.state.run_id == RUN_ID
    assert manager.evidence_dir == evidence / "discovery-success"
    assert manager.intervention_id == "int_99b529ac"


def test_a_run_whose_folder_matches_its_id_still_resolves(evidence: Path):
    """The common case, and `handoff-demo`. Must not regress."""
    park(evidence, RUN_ID, RUN_ID)
    assert cli._resolve_session(RUN_ID).evidence_dir == evidence / RUN_ID

    park(evidence, "handoff-demo", "handoff-demo")
    assert cli._resolve_session("handoff-demo").evidence_dir == evidence / "handoff-demo"


def test_a_path_can_be_given_instead_of_a_run_id(evidence: Path):
    """An escape hatch for a folder somewhere else entirely."""
    directory = park(evidence, "elsewhere", RUN_ID)
    manager = cli._resolve_session(str(directory))
    assert manager.evidence_dir == directory
    assert manager.state.run_id == RUN_ID


def test_the_folder_name_is_preferred_over_a_scan(evidence: Path):
    """A direct hit must win, so an unrelated folder mentioning the id cannot hijack it."""
    park(evidence, RUN_ID, RUN_ID)
    park(evidence, "aaa-decoy", RUN_ID)
    assert cli._resolve_session(RUN_ID).evidence_dir == evidence / RUN_ID


def test_an_unknown_run_lists_the_ones_that_do_exist(evidence: Path, capsys):
    """The old message blamed `handoff-demo` no matter what was running — unhelpful
    precisely when someone is mid-handoff with the clock going."""
    park(evidence, "discovery-success", RUN_ID)
    park(evidence, "handoff-demo", "handoff-demo")

    with pytest.raises(typer.Exit):
        cli._resolve_session("run_that_does_not_exist")

    out = capsys.readouterr().out
    assert RUN_ID in out
    assert "handoff-demo" in out
    assert "poetry run python -m src.cli" in out, "the listed command must be runnable"


def test_no_parked_runs_at_all_says_so(evidence: Path, capsys):
    with pytest.raises(typer.Exit):
        cli._resolve_session(RUN_ID)
    assert "nothing under" in capsys.readouterr().out


def test_a_corrupt_intervention_file_is_skipped_not_fatal(evidence: Path):
    """A half-written file from a killed run must not hide the run you can still take."""
    (evidence / "broken").mkdir()
    (evidence / "broken" / "intervention.json").write_text("{not json")
    park(evidence, "discovery-success", RUN_ID)

    assert cli._resolve_session(RUN_ID).evidence_dir == evidence / "discovery-success"


def test_the_printed_commands_use_poetry(capsys):
    """A bare `python` is not on PATH in a normal shell here, and the first live handoff
    died on `zsh: command not found: python` with the run parked."""
    cli._event_printer("run_abc", Path("evidence/discovery-success"))(
        "escalated", reason="MODEL_REQUESTED", intervention="int_1"
    )
    out = capsys.readouterr().out
    assert "poetry run python -m src.cli session accept run_abc" in out
    assert "poetry run python -m src.cli session resume run_abc" in out
    # And the evidence dir, so a folder/run-id mismatch is visible where it happens.
    assert "evidence/discovery-success" in out


# --------------------------------------------------------------------------- #
# not spoiling a capture folder
# --------------------------------------------------------------------------- #


def previous_run(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "steps").mkdir(exist_ok=True)
    (directory / "events.redacted.jsonl").write_text('{"seq": 1, "run_id": "old"}\n')
    (directory / "trace.yaml").write_text("run_id: old\n")
    for index in range(30):
        (directory / "steps" / f"{index:03d}-before.png").write_bytes(b"old")


def test_a_named_folder_holding_a_run_is_refused(tmp_path: Path):
    """Loud, and before anything is written — a capture must not cost money and then
    produce a folder with two runs in it."""
    target = tmp_path / "discovery-success"
    previous_run(target)

    with pytest.raises(EvidenceDirNotEmpty) as exc:
        EvidenceWriter(evidence_dir=target, redactor=Redactor({}))

    assert "discovery-success" in str(exc.value)
    assert "--overwrite" in str(exc.value)
    # Untouched: the previous capture is still there to look at or rename.
    assert (target / "trace.yaml").read_text() == "run_id: old\n"
    assert len(list((target / "steps").glob("*.png"))) == 30


def test_overwrite_leaves_no_trace_of_the_previous_run(tmp_path: Path):
    """The orphans are the dangerous part: a trace referencing steps 0-17 beside 30 step
    screenshots, with `run-summary.json` listing all of them as its own artifacts."""
    target = tmp_path / "discovery-success"
    previous_run(target)

    writer = EvidenceWriter(evidence_dir=target, redactor=Redactor({}), overwrite=True)
    writer.event("run_started", goal="new")

    assert not (target / "trace.yaml").exists()
    assert list((target / "steps").glob("*.png")) == []
    lines = (target / "events.redacted.jsonl").read_text().splitlines()
    assert len(lines) == 1, "the old run's events must not be appended to"
    assert json.loads(lines[0])["seq"] == 1


def test_an_empty_named_folder_is_fine(tmp_path: Path):
    """Creating the folder ahead of time, or an aborted run that wrote nothing, is not
    a collision."""
    target = tmp_path / "discovery-success"
    target.mkdir()
    assert EvidenceWriter(evidence_dir=target, redactor=Redactor({})).dir == target


def test_auto_generated_run_folders_are_never_refused(tmp_path: Path):
    """`run_<timestamp>_<hex>` is unique by construction; the check must not reach it."""
    for _ in range(3):
        writer = EvidenceWriter(root=tmp_path, redactor=Redactor({}))
        writer.event("run_started")
    assert len(list(tmp_path.glob("run_*"))) == 3
