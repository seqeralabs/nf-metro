"""Tests for the passive layout-quality metrics (layout_metrics)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from conftest import parse_and_layout
from layout_metrics import (
    METRIC_KEYS,
    METRICS,
    MetricSpec,
    _bend_scores,
    _turn_angle,
    compute_metrics,
    delta_direction,
    drawn_polylines,
    format_delta,
    format_value,
    measured_geometry,
)

from nf_metro.api import resolve_theme
from nf_metro.layout.constants import STATION_RADIUS_APPROX
from nf_metro.layout.phases import guards
from nf_metro.render.svg import build_render_plan

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_render_diff import _build_metrics_html  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Scores a map need not define: a map on which no line passes a station it does
# not serve has no clearance to report.
OPTIONAL_KEYS = {"marker_clearance"}

# A spread of real fixtures: a minimal map, dense pipelines, and a wide fan.
FIXTURES = [
    EXAMPLES / "simple_pipeline.mmd",
    EXAMPLES / "rnaseq_auto.mmd",
    EXAMPLES / "variantbenchmarking.mmd",
    EXAMPLES / "topologies" / "wide_fan_out.mmd",
    EXAMPLES / "topologies" / "complex_multipath.mmd",
]


def _layout(path: Path):
    return parse_and_layout(path.read_text(), source_dir=str(path.parent))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_compute_metrics_returns_full_schema(path: Path) -> None:
    """Every fixture yields exactly the canonical metric keys, all sane."""
    metrics = compute_metrics(_layout(path))
    assert set(metrics) == set(METRIC_KEYS)
    for spec in METRICS:
        value = metrics[spec.key]
        if value is None:
            assert spec.key in OPTIONAL_KEYS, f"{spec.key} may not be undefined"
            continue
        assert isinstance(value, float)
        assert value >= 0.0
        if spec.kind == "count":
            assert value == float(int(value)), f"{spec.key} count must be integral"
        elif spec.kind == "ratio":
            assert 0.0 <= value <= 1.0


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_compute_metrics_is_deterministic(path: Path) -> None:
    """The same laid-out graph scores identically on repeat computation."""
    graph = _layout(path)
    assert compute_metrics(graph) == compute_metrics(graph)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_canvas_argument_changes_only_wasted_canvas(path: Path) -> None:
    """Supplying the real canvas affects wasted_canvas alone, never the counts."""
    graph = _layout(path)
    estimated = compute_metrics(graph)
    with_canvas = compute_metrics(graph, canvas=(5000.0, 5000.0))
    for key in METRIC_KEYS:
        if key == "wasted_canvas":
            continue
        assert estimated[key] == with_canvas[key]
    # A vastly oversized canvas strands the content, so waste cannot fall.
    assert with_canvas["wasted_canvas"] >= estimated["wasted_canvas"]


def test_simple_pipeline_is_defect_free() -> None:
    """The minimal example carries no crossings, kinks, or strikes."""
    metrics = compute_metrics(_layout(EXAMPLES / "simple_pipeline.mmd"))
    for key in ("crossings", "near_horizontal", "single_diagonals", "label_strikes"):
        assert metrics[key] == 0.0


def test_busy_pipeline_reports_positive_counts() -> None:
    """A dense pipeline scores nonzero crossings and gaps, proving the metric
    keys map onto live validator check names (a rename would silently zero them)."""
    metrics = compute_metrics(_layout(EXAMPLES / "variantbenchmarking.mmd"))
    assert metrics["crossings"] > 0
    assert metrics["excessive_gaps"] > 0


def test_plan_scoring_does_not_inherit_render_settlement_gap() -> None:
    """A render-only section shift must not count as a layout gap error."""
    graph = _layout(EXAMPLES / "topologies" / "paired_input_fan_branch_tree.mmd")
    plan = build_render_plan(graph, resolve_theme(None, graph))

    assert compute_metrics(graph, plan=plan)["excessive_gaps"] == 0.0


def test_label_strike_metric_matches_guard_enumerator() -> None:
    """The strike count equals distinct (line, station) pairs from the shared
    enumerator the guard consumes, so metric and guard cannot drift."""
    graph = _layout(EXAMPLES / "rnaseq_auto.mmd")
    pairs = {(s.line_id, s.station_id) for s in guards.iter_line_label_strikes(graph)}
    assert compute_metrics(graph)["label_strikes"] == float(len(pairs))


def test_guard_and_enumerator_share_strike_definition(monkeypatch) -> None:
    """Forcing every segment to strike makes the enumerator yield and the guard
    raise on the same fixture, proving they read one definition."""
    graph = _layout(EXAMPLES / "rnaseq_auto.mmd")

    monkeypatch.setattr(
        "nf_metro.layout.labels.segment_strikes_label",
        lambda *args, **kwargs: True,
    )
    assert next(guards.iter_line_label_strikes(graph), None) is not None
    with pytest.raises(guards.PhaseInvariantError):
        guards._guard_no_line_strikes_label(graph, "test")


# --- Route-shape scores ---


def test_turn_angle_measures_the_direction_change() -> None:
    right_angle = _turn_angle((0.0, 0.0), (10.0, 0.0), (10.0, 10.0))
    assert right_angle == pytest.approx(math.pi / 2)
    assert _turn_angle((0.0, 0.0), (10.0, 0.0), (20.0, 0.0)) == pytest.approx(0.0)
    assert _turn_angle((0.0, 0.0), (10.0, 0.0), (20.0, 10.0)) == pytest.approx(
        math.pi / 4
    )


def test_bend_scores_count_corners_above_the_floor_only() -> None:
    """Two right angles on one route and a sub-floor wobble on another: three
    waypoints turn, two of them enough to draw a corner."""
    staircase = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (20.0, 10.0)]
    wobble = [(0.0, 50.0), (100.0, 50.0), (200.0, 51.0)]
    corners, per_route, turned = _bend_scores([(None, staircase), (None, wobble)])
    assert corners == 2.0
    assert per_route == pytest.approx(1.0)
    assert turned == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_corner_total_and_per_route_agree(path: Path) -> None:
    """The two bend scores are one measurement at two scales, so the total is
    the per-route mean times the routes it was drawn from."""
    graph = _layout(path)
    metrics = compute_metrics(graph)
    offsets, routes = measured_geometry(graph)
    routed = len(drawn_polylines(routes, offsets))
    expected = metrics["bends_per_route"] * routed
    assert metrics["corners_total"] == pytest.approx(expected)


def test_single_line_map_has_no_clearance_to_report() -> None:
    """Every station serves the only line, so no line passes a marker it does
    not serve and the clearance is undefined rather than zero or huge."""
    graph = parse_and_layout(
        """%%metro line: only | Only | #ff0000
graph LR
    subgraph s [S]
        a[A] -->|only| b[B]
        b -->|only| c[C]
    end
"""
    )
    assert compute_metrics(graph)["marker_clearance"] is None


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_clearance_exceeds_the_marker_it_measures(path: Path) -> None:
    """A line closer to a foreign marker than the marker's own radius would be
    drawn inside it, which the engine's non-consumer guard forbids -- so a
    measured clearance below that means the score has mis-attributed a line."""
    clearance = compute_metrics(_layout(path))["marker_clearance"]
    if clearance is not None:
        assert clearance > STATION_RADIUS_APPROX


# --- Formatting + delta helpers ---

COUNT = MetricSpec("c", "C", "count")
RATIO = MetricSpec("r", "R", "ratio")
DECIMAL = MetricSpec("d", "D", "decimal")
ROOMIER = MetricSpec("g", "G", "decimal", higher_is_better=True)


def test_format_value() -> None:
    assert format_value(COUNT, None) == "n/a"
    assert format_value(COUNT, 3.0) == "3"
    assert format_value(RATIO, 0.125) == "12%"
    assert format_value(DECIMAL, 1.25) == "1.2"


def test_format_delta_sign_and_zero() -> None:
    assert format_delta(COUNT, 5.0, 2.0) == "−3"
    assert format_delta(COUNT, 2.0, 5.0) == "+3"
    assert format_delta(COUNT, 4.0, 4.0) == "0"
    assert format_delta(RATIO, 0.5, 0.39) == "−11%"
    assert format_delta(COUNT, None, 2.0) == ""


def test_delta_direction() -> None:
    assert delta_direction(COUNT, 5.0, 2.0) == -1  # lower is better
    assert delta_direction(COUNT, 2.0, 5.0) == 1
    assert delta_direction(COUNT, 3.0, 3.0) == 0
    assert delta_direction(COUNT, None, 3.0) == 0


def test_delta_direction_follows_the_metric_orientation() -> None:
    """A metric measuring room, not damage, improves as it grows."""
    assert delta_direction(ROOMIER, 12.0, 40.0) == -1
    assert delta_direction(ROOMIER, 40.0, 12.0) == 1
    assert delta_direction(ROOMIER, 12.0, 12.0) == 0


# --- Render-diff table assembly ---


def test_metrics_table_omitted_when_no_scorecards() -> None:
    assert _build_metrics_html([("a.svg", "changed")], {}, {}) == ""


def test_metrics_table_marks_better_and_worse() -> None:
    base = {"a.svg": {k: 0.0 for k in METRIC_KEYS}}
    pr = {"a.svg": {**{k: 0.0 for k in METRIC_KEYS}, "crossings": 4.0}}
    html = _build_metrics_html([("a.svg", "changed")], base, pr)
    assert "m-worse" in html  # crossings rose: worse
    assert "0&rarr;4 (+4)" in html

    html_better = _build_metrics_html([("a.svg", "changed")], pr, base)
    assert "m-better" in html_better
    assert "4&rarr;0 (−4)" in html_better
