"""A multi-feeder side entry deals its lanes in the order its feeders approach.

Each fixture is a generated map whose LEFT entry port is fed by two or more
stations whose lines interleave in the port's lane order.  Left in that order,
the feeders' risers cross on their way in and the corridor-cohort compile has
no feasible seating, so the render aborts.
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
    monkeypatch: pytest.MonkeyPatch, path: Path
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
        render_string(path.read_text(), source_dir=str(path.parent))
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


def test_coincident_corner_cohort_is_drawn_as_planned() -> None:
    """Corners a cohort patch moves are sized with the same coincident-radius
    unification the freeze applies, so the drawn radii are the planned ones."""
    path = REGRESSIONS / "approach_order_coincident_corners.mmd"

    assert cohort_shadow_oracle.shadow_failure(path) is None
