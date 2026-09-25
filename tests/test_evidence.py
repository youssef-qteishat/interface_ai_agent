"""
Evidence writer tests.

The two properties worth proving are the ones a reviewer depends on without knowing
it: a run that died still leaves a readable folder, and nothing sensitive reached
disk. Most of what follows is one of those two.
"""

from __future__ import annotations

import json
import re

import pytest

from src.domain.results import StopReason, Success
from src.domain.trace import (
    Display,
    Observation,
    PolicyDecision,
    ProviderInfo,
    RecordedStep,
    RunTrace,
)
from src.evidence.writer import RUN_ID_RE, EvidenceLeak, EvidenceWriter, new_run_id
from src.policy.redaction import Redactor

DECLARED = {"member_id": "12345", "opening_amount": "25.00"}
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


@pytest.fixture
def writer(tmp_path) -> EvidenceWriter:
    return EvidenceWriter(root=tmp_path, redactor=Redactor(DECLARED))


def make_trace(steps: int = 0) -> RunTrace:
    trace = RunTrace(
        run_id="run_20260923_144512_a3f1",
        goal="Find member 12345 and prepare a savings sub-account; stop at review",
        target="http://bank-sim:8001/",
        display=Display(width=1280, height=800),
        provider=ProviderInfo(name="fake", model="none"),
        started_at="2026-09-23T14:45:12Z",
    )
    for index in range(steps):
        trace.steps.append(make_step(index))
    return trace


def make_step(index: int = 0, *, with_observations: bool = True) -> RecordedStep:
    return RecordedStep(
        index=index,
        action={"kind": "left_click", "coordinate": [550, 96]},
        policy=PolicyDecision(decision="allow", risk="reversible", rule="action_allowlist"),
        observation_before=Observation(main_frame_url="http://bank-sim:8001/")
        if with_observations
        else None,
        observation_after=Observation(
            main_frame_url="http://bank-sim:8001/", dom_changed=True
        )
        if with_observations
        else None,
        probe_unavailable="not probed in this test",
    )


# --------------------------------------------------------------------------- #
# run ids and layout
# --------------------------------------------------------------------------- #


def test_run_id_matches_the_gitignore_rule():
    """`.gitignore` carries `evidence/run_*/`, so scratch runs stay out of the repo
    while curated folders remain committable. A different prefix would silently make
    every experimental run committable."""
    run_id = new_run_id()
    assert RUN_ID_RE.match(run_id), run_id
    assert run_id.startswith("run_")


def test_writer_creates_the_expected_layout(tmp_path):
    writer = EvidenceWriter(root=tmp_path, redactor=Redactor(DECLARED))
    assert writer.dir.parent == tmp_path
    assert (writer.dir / "steps").is_dir()
    assert writer.dir.name.startswith("run_")


def test_evidence_dir_override_writes_exactly_there(tmp_path):
    """Step 14 produces evidence/discovery-success/ directly rather than copying a
    run_* folder by hand."""
    target = tmp_path / "discovery-success"
    writer = EvidenceWriter("whatever", evidence_dir=target, redactor=Redactor(DECLARED))
    assert writer.dir == target
    assert not list(tmp_path.glob("run_*"))


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #


def test_every_event_line_is_valid_json_with_correlation(writer: EvidenceWriter):
    writer.event("observation", step_index=7, actor="automation")
    line = writer.events_path.read_text().strip()
    record = json.loads(line)
    assert record["run_id"] == writer.run_id
    assert record["kind"] == "observation"
    assert record["step_id"] == f"{writer.run_id}#007"
    assert record["seq"] == 1
    assert record["at"]


def test_seq_is_strictly_increasing(writer: EvidenceWriter):
    for i in range(5):
        writer.event("tick", step_index=i)
    seqs = [json.loads(l)["seq"] for l in writer.events_path.read_text().splitlines()]
    assert seqs == [1, 2, 3, 4, 5]


def test_events_are_flushed_immediately(writer: EvidenceWriter):
    """Not buffered until close: a killed process must leave its events behind."""
    writer.event("observation", step_index=0)
    assert writer.events_path.read_text().count("\n") == 1  # readable before close


def test_events_without_a_step_index_have_no_step_id(writer: EvidenceWriter):
    writer.event("run_started")
    assert "step_id" not in json.loads(writer.events_path.read_text().strip())


# --------------------------------------------------------------------------- #
# the redaction gate
# --------------------------------------------------------------------------- #


def test_declared_values_are_replaced_on_the_way_to_disk(writer: EvidenceWriter):
    writer.event("action", step_index=1, action={"kind": "type", "text": "12345"})
    blob = writer.events_path.read_text()
    assert "12345" not in blob
    assert "${inputs.member_id}" in blob


def test_a_leak_raises_and_writes_nothing(tmp_path):
    """If the redactor has a hole, the run stops. The alternative is committing the
    value to a public repo, which is far more expensive than a failed run."""

    class BrokenRedactor(Redactor):
        def redact(self, value):  # pretends to redact, doesn't
            return value

    writer = EvidenceWriter(root=tmp_path, redactor=BrokenRedactor(DECLARED))
    with pytest.raises(EvidenceLeak) as exc:
        writer.event("action", step_index=1, action={"text": "12345"})

    assert exc.value.names == ["member_id"]
    assert "12345" not in str(exc.value), "the error must name the input, not print the value"
    assert not writer.events_path.exists() or writer.events_path.read_text() == ""


def test_trace_write_is_also_gated(tmp_path):
    class BrokenRedactor(Redactor):
        def redact(self, value):
            return value

    writer = EvidenceWriter(root=tmp_path, redactor=BrokenRedactor(DECLARED))
    trace = make_trace()  # its goal contains "12345"
    with pytest.raises(EvidenceLeak):
        writer.write_trace(trace)
    assert not writer.trace_path.exists()


# --------------------------------------------------------------------------- #
# screenshots and steps
# --------------------------------------------------------------------------- #


def test_step_screenshots_are_zero_padded_so_they_sort(writer: EvidenceWriter):
    writer.step_screenshot(7, "before", PNG)
    writer.step_screenshot(12, "after", PNG)
    names = sorted(p.name for p in (writer.dir / "steps").iterdir())
    assert names == ["007-before.png", "012-after.png"]


def test_screenshot_paths_are_relative_to_the_run_folder(writer: EvidenceWriter):
    """An absolute path from my machine helps nobody reading the committed folder."""
    path = writer.step_screenshot(1, "before", PNG)
    assert path == "steps/001-before.png"
    assert not path.startswith("/")


def test_screenshots_are_written_verbatim(writer: EvidenceWriter):
    """Deliberately unredacted — a blurred screenshot cannot corroborate the log
    beside it, which is the only reason a reviewer opens one."""
    writer.step_screenshot(1, "before", PNG)
    assert (writer.dir / "steps" / "001-before.png").read_bytes() == PNG


def test_record_step_links_screenshots_into_the_trace(writer: EvidenceWriter):
    trace = make_trace()
    step = make_step(3)
    trace.steps.append(step)
    writer.record_step(step, before_png=PNG, after_png=PNG, trace=trace)

    assert step.observation_before.screenshot == "steps/003-before.png"
    assert step.observation_after.screenshot == "steps/003-after.png"
    assert (writer.dir / "steps" / "003-after.png").exists()

    reloaded = RunTrace.from_yaml(writer.trace_path.read_text())
    assert reloaded.steps[0].observation_before.screenshot == "steps/003-before.png"


# --------------------------------------------------------------------------- #
# crash safety — the step's named criterion
# --------------------------------------------------------------------------- #


def test_trace_is_readable_after_every_step(writer: EvidenceWriter):
    trace = make_trace()
    for index in range(3):
        step = make_step(index)
        trace.steps.append(step)
        writer.record_step(step, before_png=PNG, trace=trace)
        # Parses at every intermediate point, not only at the end.
        assert len(RunTrace.from_yaml(writer.trace_path.read_text()).steps) == index + 1


def test_an_abandoned_run_still_leaves_a_readable_folder(writer: EvidenceWriter):
    """Simulates Ctrl-C: three steps written, then nothing — no finish(), no close().

    A folder that only parses after a clean exit is not crash-safe, it is tidy.
    """
    trace = make_trace()
    for index in range(3):
        step = make_step(index)
        trace.steps.append(step)
        writer.record_step(step, before_png=PNG, after_png=PNG, trace=trace)

    del writer.__dict__["_events_handle"]  # abandon without closing

    run_dir = trace and None  # noqa: F841 - readability only
    reloaded = RunTrace.from_yaml((writer.dir / "trace.yaml").read_text())
    assert len(reloaded.steps) == 3

    lines = (writer.dir / "events.redacted.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["run_id"] == writer.run_id for line in lines)
    assert len(list((writer.dir / "steps").iterdir())) == 6


def test_trace_rewrite_is_atomic(writer: EvidenceWriter):
    """No .tmp left behind, so a reader never finds a half-written trace."""
    trace = make_trace(steps=2)
    writer.write_trace(trace)
    assert writer.trace_path.exists()
    assert not list(writer.dir.glob("*.tmp"))


# --------------------------------------------------------------------------- #
# finishing
# --------------------------------------------------------------------------- #


def test_finish_writes_a_summary_a_reviewer_can_read(writer: EvidenceWriter):
    trace = make_trace(steps=2)
    result = Success(run_id=trace.run_id, steps_used=2, checkpoint_verified=True)
    writer.finish(trace, result, final_png=PNG, stop_reason=StopReason.CHECKPOINT_VERIFIED)

    summary = json.loads(writer.summary_path.read_text())
    assert summary["outcome"]["status"] == "success"
    assert summary["steps_recorded"] == 2
    assert summary["stop_reason"] == "CHECKPOINT_VERIFIED"
    assert summary["ended_at"]
    assert "final.png" in summary["artifacts"]
    assert (writer.dir / "final.png").read_bytes() == PNG
    # The goal mentions the member id; the summary must not.
    assert "12345" not in writer.summary_path.read_text()


def test_finish_works_on_a_failed_run(writer: EvidenceWriter):
    """A summary is most useful precisely when the run did not succeed."""
    trace = make_trace(steps=1)
    writer.finish(trace, None, stop_reason=StopReason.CANCELLED)
    summary = json.loads(writer.summary_path.read_text())
    assert summary["outcome"] is None
    assert summary["stop_reason"] == "CANCELLED"


def test_run_id_is_recoverable_from_the_folder_name(tmp_path):
    writer = EvidenceWriter(root=tmp_path, redactor=Redactor(DECLARED))
    assert writer.dir.name == writer.run_id
    assert re.match(RUN_ID_RE, writer.dir.name)
