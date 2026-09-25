"""
Redaction — applied before serialization, never as a cleanup pass.

The distinction matters. A cleanup pass is a promise that someone remembered to run
it; redacting on the way into the writer is a property of the pipeline. Everything
that reaches `events.redacted.jsonl` or `trace.yaml` goes through here first.

What flows through: the page's whole visible text, every frame URL, headings, probe
values, and the action payloads themselves. On the review screen that means the
member id appears in at least four places at once —

    url      /servicing/members/12345
    heading  "Member Detail — 12345"
    text     "Member: 12345 … Opening Amount: $25.00"
    action   {kind: "type", text: "12345"}

...so a field-by-field redactor would miss most of them. This one walks the structure
recursively and rewrites strings wherever they appear.

The interesting decision is what a *declared input* becomes. Masking it to `1***5`
would be safe but would break the canonicalizer, whose whole job is to find literal
input values in the trace and replace them with `${inputs.*}`. So a declared input
redacts to its own placeholder instead: nothing is lost, and the trace comes out safe
*and* already half canonicalized. `/servicing/members/${inputs.member_id}` is exactly
the shape the artifact wants, and this is the only moment the mapping is unambiguous —
while the run still knows which value came from which input.
"""

from __future__ import annotations

import re
from typing import Any

MASK_TOKEN = "***REDACTED***"

# The artifact schema defines member_id as ^[0-9]{5}$, so this shape is specified
# rather than guessed. It catches OTHER members that appear in search results — data
# the run never declared and would otherwise be written out in full.
MEMBER_ID_RE = re.compile(r"\b\d{5}\b")

# Secret shapes. None of these should ever reach a trace; the redactor is the last
# line of defence rather than the only one.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:Authorization|Cookie|Set-Cookie)\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_\-]*(?:api[_\-]?key|secret|token|password)\s*[:=]\s*[^\s,;]+",
               re.IGNORECASE),
)

# Amounts that are NOT a declared input are deliberately left readable — see
# `mask_amounts`. A declared amount is claimed earlier by the placeholder rule, which
# is what the artifact needs: otherwise the capability would hardcode 25.00 rather
# than parameterizing it.
AMOUNT_RE = re.compile(r"\$?\b\d+\.\d{2}\b")

# Recognises our own output so redaction is idempotent.
PLACEHOLDER_RE = re.compile(r"\$\{inputs\.[A-Za-z0-9_]+\}")


def mask_value(value: str) -> str:
    """Keep the first and last character, hide the middle: `12345` -> `1***5`.

    Enough to correlate two mentions of the same id across a log without disclosing
    it — which is the point of masking rather than dropping.
    """
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[0]}{'*' * (len(value) - 2)}{value[-1]}"


class Redactor:
    """Redacts one run's data.

    Constructed with the run's declared inputs. Note they are held here and NOT
    written into the trace header: storing the sensitive values in the file in order
    to redact the file would rather defeat the exercise.
    """

    def __init__(
        self,
        declared_inputs: dict[str, Any] | None = None,
        *,
        mask_amounts: bool = False,
    ) -> None:
        self.declared_inputs = {k: str(v) for k, v in (declared_inputs or {}).items()}
        self.mask_amounts = mask_amounts

        # Longest value first. A shorter value that is a substring of a longer one
        # would otherwise chew a hole in the middle of it before the longer rule ran.
        self._ordered_inputs: list[tuple[str, str]] = sorted(
            ((name, value) for name, value in self.declared_inputs.items() if value),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )

    # ---- strings ----

    def redact_text(self, text: str) -> str:
        if not text:
            return text

        # 1. Declared inputs become their own placeholder. This must run FIRST: if
        # the generic member-id rule went first it would mask 12345 to 1***5 and the
        # canonicalizer would have nothing left to parameterize.
        for name, value in self._ordered_inputs:
            if value in text:
                text = text.replace(value, f"${{inputs.{name}}}")

        # 2. Secrets, before the numeric rules so a token containing digits is
        # replaced whole rather than partially mangled.
        for pattern in SECRET_PATTERNS:
            text = pattern.sub(MASK_TOKEN, text)

        # 3. Any other member-shaped id: masked, not placeholdered — it is not an
        # input of this run, so there is no placeholder it could legitimately take.
        text = MEMBER_ID_RE.sub(lambda m: mask_value(m.group()), text)

        # 4. Amounts the run did not declare stay readable by default: a trace with
        # every number masked is unreadable, and Step 11's checkpoint reads these.
        if self.mask_amounts:
            text = AMOUNT_RE.sub(MASK_TOKEN, text)

        return text

    # ---- structures ----

    def redact(self, value: Any) -> Any:
        """Walk any JSON-ish structure, rewriting strings wherever they appear.

        Recursive rather than field-by-field because the same sensitive value turns
        up in URLs, headings, free text and action payloads simultaneously, and a
        field list would have to be updated every time the trace grows a field.
        """
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            redacted = [self.redact(v) for v in value]
            return tuple(redacted) if isinstance(value, tuple) else redacted
        # Numbers, booleans, None: nothing to disclose, and coercing them to strings
        # would corrupt the trace's types.
        return value

    def redact_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """The entry point the evidence writer calls on every event before it is
        serialized. Hashes are left alone: they were computed from the real bytes,
        which is what makes them useful for change detection, and a digest is not a
        disclosure.
        """
        return self.redact(event)

    def contains_unredacted(self, blob: str) -> list[str]:
        """Diagnostic: which declared values still appear in a serialized blob.

        Used by the tests and by the evidence writer's self-check. Returns the input
        NAMES, never the values — a leak detector that prints the secret would be a
        poor one.
        """
        return [name for name, value in self._ordered_inputs if value and value in blob]
