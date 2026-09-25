"""
The evidence writer — the only thing in this system that writes a run to disk.

That exclusivity is the point. Because every byte goes through here, two statements
are true rather than merely intended: everything on disk has passed the redactor, and
a run that dies still leaves a folder someone can read.

The second property drives most of the design. Evidence written at the end of a run is
evidence you lose exactly when you want it most — a run that crashed is the one worth
reading. So events are appended and flushed one at a time, and `trace.yaml` is
rewritten atomically after every step. At forty steps the rewrite costs nothing
measurable, and it buys a file that is always complete-as-of-the-last-step rather than
truncated mid-write.

    evidence/run_20260923_144512_a3f1/
      events.redacted.jsonl   one line per transition, flushed as it happens
      steps/007-before.png    before/after for each step
      steps/007-after.png
      trace.yaml              the §7 contract, rewritten atomically each step
      run-summary.json        goal, outcome, counts, budget, timings
      final.png               the last thing the agent saw
      intervention.json       written by SessionManager, if a human was involved

Screenshots are deliberately NOT redacted. The simulator holds synthetic data, and a
blurred screenshot cannot corroborate the log beside it — which is the entire reason a
reviewer opens one. That decision is recorded in Step 7; do not "fix" it here.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.trace import RecordedStep, RunTrace
from src.policy.redaction import Redactor

DEFAULT_ROOT = Path("evidence")

# run_YYYYmmdd_HHMMSS_xxxx. The `run_` prefix is load-bearing: .gitignore carries an
# `evidence/run_*/` rule so scratch runs stay out of the repo while curated folders
# (discovery-success/, handoff-unknown-dialog/) remain committable.
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{4}$")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"run_{stamp}_{secrets.token_hex(2)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EvidenceLeak(Exception):
    """A declared sensitive value reached the serialized output.

    Raised instead of writing. A leak here means the redactor has a hole, and the file
    it would land in is committed to a public repository — so stopping the run is far
    cheaper than shipping the value. The message names the INPUT, never the value.
    """

    def __init__(self, names: list[str], where: str) -> None:
        super().__init__(f"unredacted {', '.join(names)} would have been written to {where}")
        self.names = names
        self.where = where


class EvidenceDirNotEmpty(Exception):
    """A named evidence folder already holds a run.

    Raised rather than merging, because merging is silent and produces an artifact that
    looks complete and is not: `events.redacted.jsonl` is appended to, so two runs'
    events interleave with sequence numbers restarting partway through; the previous
    run's step screenshots survive as orphans that the new trace does not reference; and
    `run-summary.json` builds its artifact list by walking the directory, so it lists
    those orphans as its own.

    Only ever raised for an explicitly named directory. A `run_<timestamp>_<hex>` folder
    is unique by construction and never collides.
    """

    def __init__(self, path: Path, entries: int) -> None:
        super().__init__(
            f"{path} already contains {entries} file(s) from an earlier run. "
            f"Pass --overwrite to replace it, or choose another --evidence-dir."
        )
        self.path = path


class RecorderAssertionError(Exception):
    """The trace is missing something the canonicalizer needs.

    Deliberately an exception rather than a `Failure` result. A leak or a limit is a
    condition the run can report; this is a *bug in the recorder*, and a run that
    quietly downgraded it would hand the next layer an artifact with a hole in it. The
    trace on disk keeps its last valid version, because writes are write-then-rename.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def assert_recordable(trace: RunTrace) -> None:
    """The checks Step 12 runs before every write.

    All three are about the same thing: a step whose evidence cannot be reconstructed
    later. They run on every incremental write rather than once at the end, so a
    violation surfaces at the step that caused it instead of after forty more.
    """
    problems: list[str] = []

    missing_policy = trace.steps_missing_policy()
    if missing_policy:
        problems.append(f"automation steps with no policy decision: {missing_policy}")

    missing_probe = trace.steps_missing_probe()
    if missing_probe:
        problems.append(
            f"coordinate steps with neither a probe nor a probe_unavailable reason: "
            f"{missing_probe}"
        )

    indices = [s.index for s in trace.steps]
    if indices != sorted(set(indices)):
        # Both failures at once: a duplicate index overwrites another step's
        # screenshots, and out-of-order indices make the trace unreadable as a sequence.
        problems.append(f"step indices are not unique and ascending: {indices}")

    if problems:
        raise RecorderAssertionError(problems)


class EvidenceWriter:
    """Writes one run's evidence folder.

    Usable as a context manager; the JSONL handle is closed on exit, but every line is
    already flushed by then, so an abandoned writer loses nothing.
    """

    def __init__(
        self,
        run_id: str | None = None,
        *,
        root: str | Path = DEFAULT_ROOT,
        redactor: Redactor | None = None,
        evidence_dir: str | Path | None = None,
        overwrite: bool = False,
    ) -> None:
        self.run_id = run_id or new_run_id()
        # `evidence_dir` overrides the layout entirely, so Step 14 can write straight
        # into evidence/discovery-success/ rather than producing a run_* folder and
        # copying it by hand.
        self.dir = Path(evidence_dir) if evidence_dir else Path(root) / self.run_id
        self.redactor = redactor or Redactor({})
        self._seq = 0
        self._events_handle = None

        # A named folder that already holds a run is a collision; an auto-generated
        # run_* folder cannot be. Checked before anything is written, so a capture run
        # fails on the first line rather than after the model has been paid.
        if evidence_dir is not None and self.dir.exists():
            existing = [p for p in self.dir.rglob("*") if p.is_file()]
            if existing and not overwrite:
                raise EvidenceDirNotEmpty(self.dir, len(existing))
            if existing:
                shutil.rmtree(self.dir)

        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "steps").mkdir(exist_ok=True)

    # ---- paths ----

    @property
    def events_path(self) -> Path:
        return self.dir / "events.redacted.jsonl"

    @property
    def trace_path(self) -> Path:
        return self.dir / "trace.yaml"

    @property
    def summary_path(self) -> Path:
        return self.dir / "run-summary.json"

    def step_id(self, index: int) -> str:
        """Correlation id. Without it a forty-line JSONL is just forty lines; with it
        an action, its policy decisions, its probe and its screenshots are one story."""
        return f"{self.run_id}#{index:03d}"

    # ---- the redaction gate ----

    def _serialize(self, payload: Any, where: str) -> str:
        """Redact, serialize, then verify. The verify step is not paranoia: it is the
        only thing that would catch a redactor that silently stopped matching."""
        redacted = self.redactor.redact(payload)
        blob = json.dumps(redacted, ensure_ascii=False, default=str)
        leaked = self.redactor.contains_unredacted(blob)
        if leaked:
            raise EvidenceLeak(leaked, where)
        return blob

    # ---- events ----

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one line and flush it.

        Flushing per event is what makes a killed run readable. The cost is one write
        syscall per event, against runs of a few dozen events.
        """
        self._seq += 1
        record: dict[str, Any] = {
            "seq": self._seq,
            "at": _now(),
            "run_id": self.run_id,
            "kind": kind,
            **fields,
        }
        if "step_index" in fields and fields["step_index"] is not None:
            record["step_id"] = self.step_id(int(fields["step_index"]))

        line = self._serialize(record, self.events_path.name)
        if self._events_handle is None:
            self._events_handle = self.events_path.open("a", encoding="utf-8")
        self._events_handle.write(line + "\n")
        self._events_handle.flush()
        return record

    # ---- screenshots ----

    def screenshot(self, label: str, png: bytes) -> str:
        """Write a PNG and return its path relative to the run folder.

        Relative on purpose: the trace should stay readable after the folder is moved
        or committed, and an absolute path from my machine helps nobody.
        """
        path = self.dir / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        return str(path.relative_to(self.dir))

    def step_screenshot(self, index: int, phase: str, png: bytes) -> str:
        """`steps/007-before.png` — zero-padded so the directory sorts in run order."""
        return self.screenshot(f"steps/{index:03d}-{phase}", png)

    # ---- steps and trace ----

    def record_step(
        self,
        step: RecordedStep,
        *,
        before_png: bytes | None = None,
        after_png: bytes | None = None,
        trace: RunTrace | None = None,
    ) -> RecordedStep:
        """Persist one step: its screenshots, its event line, and the updated trace.

        Screenshot paths are written back onto the step's observations so the trace
        points at real files rather than describing them.
        """
        if before_png is not None and step.observation_before is not None:
            step.observation_before.screenshot = self.step_screenshot(
                step.index, "before", before_png
            )
        if after_png is not None and step.observation_after is not None:
            step.observation_after.screenshot = self.step_screenshot(
                step.index, "after", after_png
            )

        self.event(
            "step",
            step_index=step.index,
            actor=step.actor,
            action=step.action,
            policy=step.policy.model_dump(mode="json") if step.policy else None,
            probe_unavailable=step.probe_unavailable,
            candidates=(
                [c.model_dump(mode="json") for c in step.probe.candidates] if step.probe else None
            ),
            dom_changed=(
                step.observation_after.dom_changed if step.observation_after else None
            ),
            timing=step.timing.model_dump(mode="json"),
            model_reason=step.model_reason,
            error=step.error,
        )

        if trace is not None:
            self.write_trace(trace)
        return step

    def write_trace(self, trace: RunTrace) -> None:
        """Rewrite `trace.yaml` atomically, redacted.

        The redacted copy is re-validated as a `RunTrace` on the way out, which checks
        something worth checking: that redaction did not break the schema the
        canonicalizer will read. (An earlier version here checked for leaks without
        redacting first — the gate caught it, which is the argument for having one.)

        Write-then-rename, so a reader (or a crash) never sees a half-written trace:
        the file is either the previous complete version or the new one.
        """
        assert_recordable(trace)
        redacted = RunTrace.model_validate(self.redactor.redact(trace.model_dump(mode="json")))
        text = redacted.to_yaml()
        leaked = self.redactor.contains_unredacted(text)
        if leaked:
            raise EvidenceLeak(leaked, self.trace_path.name)

        tmp = self.trace_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.trace_path)

    # ---- finishing ----

    def finish(
        self,
        trace: RunTrace,
        result: Any | None = None,
        *,
        final_png: bytes | None = None,
        stop_reason: str | None = None,
    ) -> Path:
        """Write the closing artefacts. Safe to call on a failed or cancelled run —
        that is precisely when a summary is most useful."""
        if final_png is not None:
            self.screenshot("final", final_png)

        trace.ended_at = _now()
        if result is not None:
            trace.outcome = result
        self.write_trace(trace)

        outcome = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        summary = {
            "run_id": self.run_id,
            "goal": trace.goal,
            "target": trace.target,
            "surface_kind": trace.surface_kind,
            "provider": trace.provider.model_dump(mode="json"),
            "fault_profile": trace.fault_profile,
            "started_at": trace.started_at,
            "ended_at": trace.ended_at,
            "steps_recorded": len(trace.steps),
            # "Was a person involved?" is the first thing a reviewer asks, and counting
            # human steps by hand means reading the whole trace to answer it.
            "human_steps": len(trace.human_steps()),
            "events_recorded": self._seq,
            "stop_reason": stop_reason,
            "outcome": outcome,
            "budget": trace.budget.model_dump(mode="json"),
            # Named so a reader knows what to open, without listing the directory.
            "artifacts": sorted(
                str(p.relative_to(self.dir)) for p in self.dir.rglob("*") if p.is_file()
            ),
        }
        self.summary_path.write_text(
            self._serialize(summary, self.summary_path.name), encoding="utf-8"
        )
        self.event("run_finished", status=getattr(result, "status", None), stop_reason=stop_reason)
        self.close()
        return self.dir

    def close(self) -> None:
        if self._events_handle is not None:
            self._events_handle.close()
            self._events_handle = None

    def __enter__(self) -> EvidenceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
