"""A multi-feeder side entry deals its lanes in the order its feeders approach.

Each fixture is a generated map whose side entry port is fed by two or more
stations whose lines interleave in the port's lane order.  Left in that order,
the feeders' risers can cross on their way in, and on the first three maps the
corridor-cohort compile then has no feasible seating, so the render aborts.
The rest pin the cases where re-dealing a port's lanes would do harm.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import cohort_shadow_oracle
import pytest

from nf_metro.api import render_string
from nf_metro.render import svg

REGRESSIONS = Path(__file__).parent / "fixtures" / "regressions"

_APPROACH_ORDERED = [
    pytest.param(
        "approach_order_shared_line_feeders.mmd",
        "sec2__entry_left_3",
        ("l0", "l2", "l1"),
        id="junction-and-exit-share-a-line",
    ),
    pytest.param(
        "approach_order_interleaved_feeders.mmd",
        "z3__entry_left_5",
        ("m0", "m2", "m1"),
        id="exit-feeders-interleave",
    ),
    pytest.param(
        "approach_order_coincident_corners.mmd",
        "z3__entry_left_5",
        ("m0", "m2", "m1"),
        id="coincident-corner-cohort",
    ),
]


def _render_offsets(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    layout_options: dict[str, Any] | None = None,
) -> dict[tuple[str, str], float]:
    """Render *path* and return the station offsets the drawing used."""
    seen: dict[str, Any] = {}
    compute = svg.compute_station_offsets

    def capture(*args: Any, **kwargs: Any) -> dict[tuple[str, str], float]:
        seen["offsets"] = offsets = compute(*args, **kwargs)
        return offsets

    monkeypatch.setattr(svg, "compute_station_offsets", capture)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        render_string(
            path.read_text(),
            layout_options=layout_options,
            source_dir=str(path.parent),
        )
    return seen["offsets"]


@pytest.mark.parametrize(("fixture", "port_id", "order"), _APPROACH_ORDERED)
def test_side_entry_lanes_follow_feeder_approach(
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    port_id: str,
    order: tuple[str, ...],
) -> None:
    offsets = _render_offsets(monkeypatch, REGRESSIONS / fixture)

    lanes = [offsets[(port_id, lid)] for lid in order]
    assert lanes == sorted(set(lanes)), dict(zip(order, lanes))


_KEPT_ORDER = [
    pytest.param(
        "approach_order_tied_risers.mmd",
        "sec5__entry_right_8",
        ("l1", "l2", "l0", "l3"),
        id="tied-risers-keep-their-order",
    ),
    pytest.param(
        "approach_order_feeder_keeps_its_order.mmd",
        "z3__entry_right_5",
        ("m0", "m1", "m2"),
        id="multi-line-feeder-keeps-its-order",
    ),
    pytest.param(
        "approach_order_draft_off_lane.mmd",
        "sec2__entry_left_3",
        ("l0", "l1", "l2", "l3"),
        id="draft-lands-off-its-lane",
    ),
]


@pytest.mark.parametrize(("fixture", "port_id", "order"), _KEPT_ORDER)
def test_side_entry_lane_order_where_a_re_deal_would_harm(
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    port_id: str,
    order: tuple[str, ...],
) -> None:
    """Risers tied on one leg keep their incoming relative order; a port where
    a feeder's own lines would swap, or whose feeder is drawn off the lane its
    offset names, keeps its lanes."""
    offsets = _render_offsets(monkeypatch, REGRESSIONS / fixture)

    lanes = [offsets[(port_id, lid)] for lid in order]
    assert lanes == sorted(set(lanes)), dict(zip(order, lanes))


def test_junction_feeder_lanes_are_not_narrowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A junction's corners are sized for the spread it feeds, so packing its
    lines closer at the port would bend them through non-concentric corners."""
    _render_offsets(monkeypatch, REGRESSIONS / "approach_order_junction_gap.mmd")


def test_overridden_port_order_is_not_carried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port order a later phase overrides would leave its carry stranded on
    the stations it reached, here a dead lane between a station's two lines."""
    offsets = _render_offsets(
        monkeypatch,
        REGRESSIONS / "approach_order_overridden_later.mmd",
        {"line_order": "span", "row_align": "top"},
    )

    assert abs(offsets[("s3n1", "l0")] - offsets[("s3n1", "l2")]) == 4.0


def test_coincident_corner_cohort_is_drawn_as_planned() -> None:
    """Corners a cohort patch moves are sized with the same coincident-radius
    unification the freeze applies, so the drawn radii are the planned ones."""
    path = REGRESSIONS / "approach_order_coincident_corners.mmd"

    assert cohort_shadow_oracle.shadow_failure(path) is None
