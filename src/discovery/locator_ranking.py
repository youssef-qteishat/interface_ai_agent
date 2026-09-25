"""
Which locator the artifact leads with, and why the others were left out.

The probe hands over an unordered-ish pile of candidates with match counts. This picks the order
replay will try them in, and — just as important — records what it refused. An artifact that silently
omits a locator is impossible to review: you cannot tell a candidate that was never available from one
that was rejected for a good reason.

Note that `sandbox/probe.js` and `surface_agent.py` already do a partial version of this: probe.js
emits a rough ladder, and the agent inserts the role candidate first unless its name came from a
placeholder, in which case it appends it last. This module still exists, because the artifact's locator
priority should not be decided by JavaScript running inside the application being observed, and a
reviewer should be able to see the rule that was applied. What it means in practice is that this is a
filter and a re-sort, not a scoring engine.
"""

from __future__ import annotations

from typing import Any

from src.domain.artifact import Rejection
from src.domain.trace import LocatorCandidate, ProbeResult

# §3's ladder. Index is priority; replay tries them in this order.
#
# `attribute` outranks `text` because on this app a form `name=` is what the *server* reads — it is a
# contract, not a coincidence — while visible button text is a label someone can retheme. `css` is
# always last and always `stability: unverified`: it encodes markup order, which is exactly what a
# redesign changes.
LADDER: tuple[str, ...] = ("role", "label", "contextual_text", "attribute", "text", "css")


def _why_not(candidate: Any) -> str | None:
    """The first thing wrong with a candidate, or None.

    Order matters: a candidate can fail two ways at once and the reader wants the specific reason.
    The amount field's role candidate is both placeholder-derived *and* uncounted; "matches any
    element showing the same formatting hint" explains it, "not counted" does not.
    """
    if getattr(candidate, "name_source", None) == "placeholder":
        return (
            "accessible_name_source: placeholder — the name is a formatting hint, not an identity, "
            "and matches any element displaying the same one"
        )

    count = candidate.match_count
    if count is None:
        return "match_count: null — not counted, so it cannot be relied on to be unique"
    if count == 0:
        return "match_count: 0 — matched nothing on the page it was captured from"
    if count > 1:
        return (
            f"match_count: {count} — matches more than one element, so it can never be primary "
            f"(replay treats a multi-match as TARGET_AMBIGUOUS rather than picking one)"
        )
    return None


def _describe(candidate: Any) -> str:
    """A short identifier for the rejection line, so a reviewer knows which one was dropped."""
    for attr in ("selector", "text", "anchor"):
        value = getattr(candidate, attr, None)
        if value:
            return str(value)
    role, name = getattr(candidate, "role", None), getattr(candidate, "name", None)
    if role or name:
        return f"{role}/{name}"
    return candidate.kind


def rank_candidates(
    probe: ProbeResult,
) -> tuple[list[LocatorCandidate], list[Rejection]]:
    """Order the usable candidates; explain the rest.

    Returns `(kept, rejected)`. An empty `kept` is a real answer, not an error: Member Detail's two
    identical `Back` buttons put every candidate at `match_count: 2`, which means nothing on that page
    can identify one of them. A step cannot be written for such an element, and pretending otherwise
    by keeping an ambiguous candidate would turn a clean fall-through into a hard replay failure.
    """
    kept: list[LocatorCandidate] = []
    rejected: list[Rejection] = []

    for candidate in probe.candidates:
        reason = _why_not(candidate)
        if reason is None:
            kept.append(candidate)
        else:
            rejected.append(
                Rejection(kind=candidate.kind, value=_describe(candidate), reason=reason)
            )

    # Stable, so candidates of equal rank keep the order the probe found them in.
    kept.sort(key=lambda c: LADDER.index(c.kind))

    # A rejection with no candidate to reject. `LocatorCandidate` has no `dom_id` variant, so a
    # generated id never becomes a candidate in the first place — which would leave the artifact
    # silent about the most obvious-looking locator on the page. Say it was seen and refused.
    if probe.dom_id and probe.dom_id_stability == "generated":
        rejected.append(
            Rejection(
                kind="dom_id",
                value=probe.dom_id,
                reason="dom_id_stability: generated — a new id is issued on every request",
            )
        )

    return kept, rejected
