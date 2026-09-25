"""
Reading the artifact's declared outputs off the finished screen.

`OutputSpec.extract` is a required field, and the reason is in its own comment: *a declared output with no
way to extract it is a promise the artifact cannot keep*. This is where the promise is kept. A replay that
returned `Success(outputs={})` while the contract declares four fields would be claiming success at
something it never did.

The region comes from `extract.scope`, which is the same `#review-container table.review-table` the
`verify-outcome` checkpoint now asserts inside. That is deliberate rather than a coincidence: the values a
caller receives and the values replay verified should be read from one place, or a run can pass its
checkpoint and return something else.

Transforms are named in the spec and resolved here. A name with no implementation **raises** — see
`apply_transform`.
"""

from __future__ import annotations

import re
from typing import Any

from src.domain.artifact import Contract


class ExtractionError(Exception):
    """The screen does not carry what the contract says it will."""


def mask_member_id(value: str) -> str:
    """`23456` -> `***56`. Enough to confirm which member, not enough to be the member id.

    The output is declared `sensitive: true`, so this is the difference between returning a member
    identifier to a caller and returning a reference to one.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) <= 2:
        return "*" * len(digits)
    return "*" * (len(digits) - 2) + digits[-2:]


def strip_currency(value: str) -> str:
    """`$50.00` -> `50.00`. The page chose the presentation; a caller wants the number.

    Thousands separators go too, so `$1,250.00` parses. The result is left as a string because the
    contract says `decimal` and floating point is not what a caller of a banking API wants.
    """
    cleaned = re.sub(r"[^\d.\-]", "", value)
    if not cleaned:
        raise ExtractionError(f"no numeric value in {value!r}")
    return cleaned


# Named in `src/discovery/specs.py`, resolved here. Adding a transform to a spec without adding it here is
# caught by `apply_transform` rather than silently ignored.
TRANSFORMS: dict[str, Any] = {
    "mask_member_id": mask_member_id,
    "strip_currency": strip_currency,
}


def apply_transform(name: str | None, value: str) -> str:
    """Apply a named transform, or refuse.

    Raising on an unknown name rather than passing the raw value through, because the transforms that
    exist here are `mask_member_id` — on a field declared `sensitive: true` — and `strip_currency`. A
    quietly unapplied mask returns a real member id to a caller while the artifact says it is masked. The
    failure has to be loud, and it is a contract error rather than a screen-reading error.
    """
    if name is None:
        return value
    if name not in TRANSFORMS:
        raise ExtractionError(
            f"output transform {name!r} is named in the contract but not implemented "
            f"(have: {', '.join(sorted(TRANSFORMS))})"
        )
    return TRANSFORMS[name](value)


async def extract_outputs(
    surface: Any, frame_path: list[str] | None, contract: Contract
) -> dict[str, Any]:
    """Every declared output, read from the screen.

    Row-labelled rather than positional: the extractor finds the cell whose text is the `row_label` and
    takes the next cell along. A positional path would encode the table's row order, which is exactly what
    a redesign changes — the same argument that puts `css` last on the locator ladder.
    """
    results: dict[str, Any] = {}

    for name, spec in contract.outputs.items():
        selector = getattr(spec.extract.scope, "selector", None)
        if not selector:
            raise ExtractionError(
                f"output {name!r} has a {spec.extract.scope.kind} scope with no selector; "
                f"extraction needs a region it can query"
            )

        region = surface.frame(frame_path).locator(selector)
        if not await region.count():
            raise ExtractionError(f"output {name!r}: nothing matched {selector!r} on this screen")

        fields: dict[str, Any] = {}
        for field_name, field in spec.extract.fields.items():
            raw = await _row_value(region.first, field.row_label)
            if raw is None:
                raise ExtractionError(
                    f"output {name}.{field_name}: no row labelled {field.row_label!r} in {selector!r}"
                )
            fields[field_name] = apply_transform(field.transform, raw)

        # A scalar output declares no properties; an object returns the field map.
        results[name] = fields if spec.properties is not None else next(iter(fields.values()), None)

    return results


async def _row_value(region: Any, row_label: str) -> str | None:
    """The cell next to the one holding `row_label`.

    `normalize-space` on both sides because the template indents its cells, and the label is matched with
    its colon exactly as the spec writes it (`"Member:"`) — a label is a fixed string on this screen, not
    a value someone parameterised.
    """
    quoted = row_label.replace("'", "\\'")
    cell = region.locator(
        f"xpath=.//td[normalize-space(.)='{quoted}']/following-sibling::td[1]"
    )
    if not await cell.count():
        return None
    return " ".join((await cell.first.inner_text()).split())
