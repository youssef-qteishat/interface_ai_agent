"""
Coordinate scaling and action translation.

Both are pure enough to test without Docker, and both are places where a quiet bug
produces clicks that land *almost* right — the hardest kind of failure to diagnose
from a screenshot. Hence tests that pin the arithmetic and the refusals rather than
trusting that 1280x800 happens to need no scaling today.
"""

from __future__ import annotations

import pytest

from src.domain.actions import (
    CursorPosition,
    DoubleClick,
    Key,
    LeftClick,
    Scroll,
    Type,
    Wait,
)
from src.surfaces.base import UnsupportedAction
from src.surfaces.x11_computer import (
    DEFAULT_MAX_LONG_EDGE,
    DEFAULT_MAX_PIXELS,
    X11ComputerAdapter,
    compute_scale,
    to_display,
    to_model,
)


# --------------------------------------------------------------------------- #
# scaling
# --------------------------------------------------------------------------- #


def test_our_display_needs_no_scaling():
    """1280x800 was chosen so the factor is exactly 1.0 — but the code path still
    runs, so it cannot rot while nobody is looking."""
    assert compute_scale(1280, 800) == 1.0


def test_large_display_is_scaled_under_both_limits():
    width, height = 2560, 1600
    scale = compute_scale(width, height)
    assert scale < 1.0
    scaled_w, scaled_h = round(width * scale), round(height * scale)
    assert max(scaled_w, scaled_h) <= DEFAULT_MAX_LONG_EDGE
    assert scaled_w * scaled_h <= DEFAULT_MAX_PIXELS * 1.01  # rounding headroom


def test_long_thin_display_is_bounded_by_its_long_edge():
    """Few total pixels, but too wide: the long-edge limit has to bite."""
    scale = compute_scale(3000, 400)
    assert round(3000 * scale) <= DEFAULT_MAX_LONG_EDGE


def test_scale_never_upscales_a_small_display():
    assert compute_scale(640, 480) == 1.0


def test_limits_are_parameters_not_constants():
    """Opus 5 permits larger images (2576 px / ~3.75 MP); raising the limit should be
    a decision, not an edit to the function.

    Note 2560x1600 is 4.1 MP, so even the raised ceiling still downscales it — the
    pixel-count limit bites before the long-edge one. A display that fits both passes
    through untouched.
    """
    conservative = compute_scale(2560, 1600)
    opus5 = compute_scale(2560, 1600, max_long_edge=2576, max_pixels=3_750_000)
    assert opus5 > conservative  # less downscaling under the higher ceiling
    assert opus5 < 1.0  # but 4.1 MP still exceeds 3.75 MP

    # 2000x1400 = 2.8 MP, long edge 2000: inside both raised limits.
    assert compute_scale(2000, 1400, max_long_edge=2576, max_pixels=3_750_000) == 1.0


def test_coordinate_round_trips_within_one_pixel():
    scale = compute_scale(2560, 1600)
    for point in [(0, 0), (1, 1), (1279, 799), (2559, 1599), (640, 400)]:
        back = to_display(to_model(point, scale), scale)
        assert abs(back[0] - point[0]) <= 1
        assert abs(back[1] - point[1]) <= 1


def test_identity_scale_is_exact():
    """At scale 1.0 nothing may drift — every discovery run depends on this."""
    for point in [(0, 0), (550, 96), (1279, 799)]:
        assert to_display(point, 1.0) == point
        assert to_model(point, 1.0) == point


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        compute_scale(0, 800)
    with pytest.raises(ValueError):
        to_display((1, 1), 0.0)


# --------------------------------------------------------------------------- #
# translation: clamp bounds, refuse semantics
# --------------------------------------------------------------------------- #


@pytest.fixture
def adapter() -> X11ComputerAdapter:
    """No HTTP happens in these tests — only the pure translation step."""
    return X11ComputerAdapter("http://127.0.0.1:8900")


def test_click_translates_to_display_coordinates(adapter: X11ComputerAdapter):
    payload, clamped = adapter._to_agent_payload(LeftClick(coordinate=(550, 96)))
    assert payload == {"kind": "left_click", "coordinate": [550, 96]}
    assert clamped is None


def test_double_click_keeps_its_kind(adapter: X11ComputerAdapter):
    payload, _ = adapter._to_agent_payload(DoubleClick(coordinate=(10, 20)))
    assert payload["kind"] == "double_click"


def test_long_wait_is_clamped_and_recorded(adapter: X11ComputerAdapter):
    """A shorter wait still means 'wait' — but the trace must not claim 300s ran."""
    payload, clamped = adapter._to_agent_payload(Wait(duration=300))
    assert payload["duration"] == 10.0
    assert clamped == {"duration": {"requested": 300.0, "applied": 10.0}}


def test_short_wait_passes_through_unclamped(adapter: X11ComputerAdapter):
    payload, clamped = adapter._to_agent_payload(Wait(duration=2.5))
    assert payload["duration"] == 2.5
    assert clamped is None


def test_big_scroll_is_clamped_and_recorded(adapter: X11ComputerAdapter):
    payload, clamped = adapter._to_agent_payload(
        Scroll(scroll_direction="down", scroll_amount=50)
    )
    assert payload["scroll_amount"] == 20
    assert clamped["scroll_amount"] == {"requested": 50, "applied": 20}


def test_overlong_text_is_refused_not_truncated(adapter: X11ComputerAdapter):
    """Truncating would type DIFFERENT data into a banking form — the one outcome
    worse than doing nothing."""
    with pytest.raises(UnsupportedAction) as exc:
        adapter._to_agent_payload(Type(text="x" * 600))
    assert exc.value.context["limit"] == 500


def test_text_at_the_limit_is_allowed(adapter: X11ComputerAdapter):
    payload, _ = adapter._to_agent_payload(Type(text="x" * 500))
    assert len(payload["text"]) == 500


def test_modifier_click_is_refused(adapter: X11ComputerAdapter):
    """Dropping the modifier would produce a plain click that looks successful."""
    with pytest.raises(UnsupportedAction) as exc:
        adapter._to_agent_payload(LeftClick(coordinate=(1, 2), text="ctrl"))
    assert exc.value.context["modifiers"] == "ctrl"


def test_malformed_key_is_refused_before_the_round_trip(adapter: X11ComputerAdapter):
    with pytest.raises(UnsupportedAction):
        adapter._to_agent_payload(Key(text="Return; rm -rf /"))


def test_valid_key_combinations_pass(adapter: X11ComputerAdapter):
    for combo in ("Return", "ctrl+a", "Tab", "shift+Tab"):
        payload, _ = adapter._to_agent_payload(Key(text=combo))
        assert payload["text"] == combo


def test_cursor_position_needs_no_arguments(adapter: X11ComputerAdapter):
    payload, clamped = adapter._to_agent_payload(CursorPosition())
    assert payload == {"kind": "cursor_position"}
    assert clamped is None


def test_scaled_click_converts_to_display_space():
    """The direction that moves a real cursor must never be applied twice."""
    adapter = X11ComputerAdapter("http://127.0.0.1:8900")
    adapter.scale = 0.5  # as if the display were 2560x1600
    payload, _ = adapter._to_agent_payload(LeftClick(coordinate=(275, 48)))
    assert payload["coordinate"] == [550, 96]
