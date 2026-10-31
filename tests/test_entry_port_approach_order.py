"""A multi-feeder side entry deals its lanes in the order its feeders approach.

Each fixture is a generated map whose side entry port is fed by two or more
stations whose lines interleave in the port's lane order.  Left in that order,
the feeders' risers can cross on their way in, and on the first three maps the
corridor-cohort compile then has no feasible seating, so the render aborts.
The rest pin the cases where re-dealing a port's lanes would do harm.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

import cohort_shadow_oracle
import pytest

from nf_metro.api import render_string
from nf_metro.layout.geometry import AxisFrame, flow_entry_side
from nf_metro.layout.routing import offsets as offsets_module
from nf_metro.layout.routing.context import _section_lane_frame
from nf_metro.layout.routing.inter_section_handlers import ArrivalLeg, arrival_leg_of
from nf_metro.parser.model import Edge, MetroGraph, Port, Section, Station
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


# Risers tied on one leg keep their incoming relative order.  A port keeps its
# lanes where a feeder's own lines would swap, a feeder is drawn off the lane
# its offset names, or carrying the re-deal on would cross lines between the
# stations it reaches or sit a line between the lanes of a station it skips.
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
    pytest.param(
        "approach_order_carry_crossing.mmd",
        "sec3__entry_left_5",
        ("l0", "l1", "l2"),
        id="carry-would-cross-downstream",
    ),
    pytest.param(
        "approach_order_carry_sandwich.mmd",
        "sec2__entry_left_3",
        ("l0", "l1", "l2"),
        id="carry-would-sandwich-a-skipping-line",
    ),
]


@pytest.mark.parametrize(
    ("fixture", "port_id", "order"), _APPROACH_ORDERED + _KEPT_ORDER
)
def test_side_entry_lane_order(
    monkeypatch: pytest.MonkeyPatch,
    fixture: str,
    port_id: str,
    order: tuple[str, ...],
) -> None:
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


def test_bottom_entry_of_a_vertical_flow_is_judged_in_its_own_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BOTTOM entry of a BT section runs the same rule, read on its lane
    axis (x); here the re-deal would turn a multi-line feeder's bundle over,
    so the port keeps its lanes."""
    verdicts: list[tuple[str, bool]] = []
    turns_over = offsets_module._turns_a_feeder_over

    def record(ctx: Any, port: Any, *args: Any) -> bool:
        verdict = turns_over(ctx, port, *args)
        verdicts.append((port.id, verdict))
        return verdict

    monkeypatch.setattr(offsets_module, "_turns_a_feeder_over", record)
    _render_offsets(
        monkeypatch, REGRESSIONS / "approach_order_bottom_entry_refused.mmd"
    )

    assert ("sec5__entry_bottom_5", True) in verdicts


_DRAFT = [
    (0.0, 0.0),
    (40.0, 0.0),
    (50.0, 10.0),
    (50.0, 60.0),
    (60.0, 70.0),
    (100.0, 70.0),
]


@pytest.mark.parametrize("transpose", [False, True], ids=["x-flow", "y-flow"])
@pytest.mark.parametrize("flow_sign", [1.0, -1.0], ids=["flow+", "flow-"])
@pytest.mark.parametrize("lane_sign", [1.0, -1.0], ids=["lane+", "lane-"])
def test_arrival_leg_is_read_the_same_in_every_rotated_frame(
    transpose: bool, flow_sign: float, lane_sign: float
) -> None:
    """A draft and each of its seven dihedral images, read in the matching
    section frame, give one leg; flipping the flow axis flips only the
    flow-axis steps, which the port side's inward sign flips with them."""
    radius = 10.0
    base = arrival_leg_of(_DRAFT, AxisFrame.for_direction("LR", 1.0, 1.0), radius)
    assert base == ArrivalLeg(70.0, 40.0, 10.0, 10.0)

    image = [(flow_sign * along, lane_sign * lane) for along, lane in _DRAFT]
    if transpose:
        image = [(lane, along) for along, lane in image]
    frame = replace(
        AxisFrame.for_direction("TB" if transpose else "LR", 1.0, 1.0),
        secondary_sign=lane_sign,
    )

    assert arrival_leg_of(image, frame, radius) == ArrivalLeg(
        base.end_lane,
        flow_sign * base.first_along,
        base.riser_lane,
        flow_sign * base.lead_along,
    )


_Lanes = dict[tuple[str, str], float]

# A carry from entry port "p" in (along, lane) coordinates: each station's
# position, each line's edges, the stations the carry reaches, and the offsets
# held before and after it.
_FLIP_CARRY = {
    "stations": {"p": (0, 0), "m": (100, 0), "n1": (200, 60), "n2": (200, 0)},
    "edges": [
        ("p", "m", "a"),
        ("p", "m", "b"),
        ("m", "n1", "a"),
        ("m", "n2", "b"),
    ],
    "touched": ["p", "m"],
    "cur": {("p", "a"): 10, ("m", "a"): 10},
    "trial": {("p", "b"): 10, ("m", "b"): 10},
    "counts": (1, 0),
}
_SANDWICH_CARRY = {
    "stations": {"p": (0, 0), "u": (100, 0), "v": (200, 0), "w": (300, 0)},
    "edges": [
        ("p", "u", "c"),
        ("u", "w", "c"),
        ("u", "v", "a"),
        ("v", "w", "b"),
    ],
    "touched": ["p", "u"],
    "cur": {("v", "b"): 20, ("p", "c"): 30, ("u", "c"): 30},
    "trial": {("v", "b"): 20, ("p", "c"): 10, ("u", "c"): 10},
    "counts": (0, 1),
}


def _carry_view(
    carry: dict[str, Any], direction: str, positive_fan: set[str]
) -> offsets_module._CarryFrame:
    """The carry drawn in a section of *direction*: its coordinates are the
    canonical (along, lane) pairs laid on that frame's axes and signs."""
    graph = MetroGraph()
    section = Section(id="s", name="s", direction=direction)
    graph.add_section(section)
    frame = _section_lane_frame(graph, section, positive_fan)
    for sid, (along, lane) in carry["stations"].items():
        pos = (frame.primary_sign * along, frame.secondary_sign * lane)
        x, y = pos if frame.primary.name == "x" else pos[::-1]
        if sid == "p":
            graph.add_port(Port("p", "s", flow_entry_side(direction)))
            graph.stations[sid].x, graph.stations[sid].y = x, y
        else:
            graph.register_station(Station(sid, sid, "s", x=x, y=y))
    for source, target, line in carry["edges"]:
        graph.add_edge(Edge(source, target, line))
    return offsets_module._CarryFrame(graph, frame, positive_fan, "p", carry["touched"])


@pytest.mark.parametrize(
    ("direction", "positive_fan"),
    [
        pytest.param("LR", set(), id="LR"),
        pytest.param("RL", set(), id="RL"),
        pytest.param("TB", set(), id="TB"),
        pytest.param("TB", {"s"}, id="TB-positive-fan"),
        pytest.param("BT", set(), id="BT"),
    ],
)
@pytest.mark.parametrize(
    "carry", [_FLIP_CARRY, _SANDWICH_CARRY], ids=["order-flip", "sandwich"]
)
def test_carry_refusals_count_the_same_in_every_frame(
    carry: dict[str, Any], direction: str, positive_fan: set[str]
) -> None:
    """A carry that crosses two lines or sandwiches a skipping line is counted
    once, and its prior offsets none, in every frame a section can have: the
    refusal reads lanes through the frame, not through raw x or y."""
    view = _carry_view(carry, direction, positive_fan)
    cur: _Lanes = carry["cur"]
    trial: _Lanes = carry["trial"]

    def counts(offsets: _Lanes) -> tuple[int, int]:
        return (
            len(offsets_module._carry_order_flips(view, offsets)),
            len(offsets_module._carry_sandwiches(view, offsets)),
        )

    assert counts(cur) == (0, 0)
    assert counts(trial) == carry["counts"]
    assert offsets_module._carry_adds_crossings_or_sandwiches(view, cur, trial)
