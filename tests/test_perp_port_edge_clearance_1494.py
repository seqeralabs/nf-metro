"""A port keeps clearance from the box edges it is not anchored to.

Every port is pinned to one edge of its section's bounding box: a LEFT/RIGHT
port to a vertical edge, a TOP/BOTTOM port to a horizontal one.  Along its
*other* axis the port is free, and landing it flush on a second edge draws the
inbound run along the box border, where the route and the border read as one
stroke.  It also starves the section header: with a route on the top edge the
above-left position is never clear, so the placement chain falls through to
``nudge`` and the number badge slides right, away from the box it labels.

``_compact_row_content_to_bbox_top`` (Stage 5.4) is what pulls a perpendicular
port toward the top edge, and it reserves ``perp_port_edge_clearance()`` so the
port stops short of it.  The rotated case -- a horizontal flow's TOP/BOTTOM port
approaching a vertical edge -- is held by ``_reserve_perp_port_edge_inset``
(Stage 3.5), which grows the edge instead.

Every clearance here is measured from the port's outermost *drawn* lane, not from
the port station: the bundle crossing a port is staggered off it along exactly
the free axis these clearances are measured on, so a station well clear of the
border can still have its outer lane riding it.

Covers:

* Corpus: no shipped fixture seats a port's drawn bundle flush on an unanchored
  box edge, and every perpendicular port keeps the full designed inset from the
  flow-axis edges it faces, on either axis.
* Meaningfulness: a hand-planted flush port is caught, and so is one whose
  station is clear while its bundle is not, so neither corpus check is vacuous.
* Regression: ``tb_exit_terminal_on_carrier``'s carrier row keeps entry-port
  headroom and anchors its header at the box's top-left corner.
* Rotation: both axes read their perpendicular pair off the flow axis, and a
  pair on either axis ends up equidistant from the edges it faces.  The bundle
  sits on the same side of a LEFT/RIGHT port under a TB flow and its BT mirror,
  since the run carrying it is horizontal either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    PERP_PORT_EDGE_INSET,
    SAME_COORD_TOLERANCE,
    SECTION_Y_PADDING,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import AxisFrame, perpendicular_port_sides
from nf_metro.layout.pass_metrics import perp_port_edge_clearance
from nf_metro.layout.phases._common import port_bundle_edge_reach
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_ports_clear_unanchored_box_edges,
)
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide
from nf_metro.render.constants import SECTION_NUM_CIRCLE_R_LARGE
from nf_metro.render.section_header import resolve_all_section_headers

REPO_ROOT = Path(__file__).resolve().parent.parent
CARRIER_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tb_exit_terminal_on_carrier.mmd"
FOUR_LANE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tb_right_exit_feeder_slots.mmd"
"""A TB section whose perpendicular exit carries four lines, so its bundle reaches
further off the port than the guard floor -- the only shape in which a station can
be clear of an edge its outermost lane rides."""

EPSILON = SAME_COORD_TOLERANCE
"""Sub-pixel slack, shared with the runtime guard so the two agree on the floor."""


def _gather_fixtures() -> list[Path]:
    paths: list[Path] = []
    for rel in (
        "tests/fixtures/topologies",
        "tests/fixtures",
        "examples",
        "examples/topologies",
    ):
        paths.extend(sorted((REPO_ROOT / rel).glob("*.mmd")))
    return paths


def _unanchored_edge_clearances(graph) -> list[tuple[str, str, float]]:
    """``(section_id, port_id, clearance)`` for every port, measured from its
    outermost drawn lane to the nearer of the two box edges it is *not* anchored
    to."""
    offsets = compute_station_offsets(graph)
    out: list[tuple[str, str, float]] = []
    for sec in graph.sections.values():
        if sec.bbox_w <= 0 or sec.bbox_h <= 0:
            continue
        for pid in (*sec.entry_ports, *sec.exit_ports):
            port = graph.ports.get(pid)
            station = graph.stations.get(pid)
            if port is None or station is None:
                continue
            axis = "y" if port.side in (PortSide.LEFT, PortSide.RIGHT) else "x"
            low, high = port_bundle_edge_reach(graph, pid, offsets, axis)
            if axis == "y":
                coord, box_low, box_high = (
                    station.y,
                    sec.bbox_y,
                    sec.bbox_y + sec.bbox_h,
                )
            else:
                coord, box_low, box_high = (
                    station.x,
                    sec.bbox_x,
                    sec.bbox_x + sec.bbox_w,
                )
            out.append(
                (
                    sec.id,
                    pid,
                    min(coord - low - box_low, box_high - coord - high),
                )
            )
    return out


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_ports_clear_unanchored_box_edges(path: Path) -> None:
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    flush = [
        (sid, pid, clear)
        for sid, pid, clear in _unanchored_edge_clearances(graph)
        if clear < perp_port_edge_clearance() - EPSILON
    ]
    assert not flush, (
        "ports sit within "
        f"{perp_port_edge_clearance()}px of a box edge they are not anchored to: "
        + ", ".join(f"{sid}/{pid} clearance={clear:.1f}" for sid, pid, clear in flush)
    )


def test_guard_catches_planted_flush_port() -> None:
    """The runtime guard is not vacuous: seating a port on the top edge trips it."""
    graph = parse_metro_mermaid(CARRIER_FIXTURE.read_text())
    compute_layout(graph)
    section = graph.sections["quantification"]
    pid = next(iter(section.entry_ports))
    graph.stations[pid].y = section.bbox_y
    graph.ports[pid].y = section.bbox_y

    with pytest.raises(PhaseInvariantError, match="clearance"):
        _guard_ports_clear_unanchored_box_edges(graph, "planted")


def test_guard_catches_a_port_whose_bundle_alone_rides_the_edge() -> None:
    """The bundle arm of the guard is not vacuous.

    Seats the exit port so its *station* keeps more than the floor from the box
    bottom while its outermost lane sits on the border.  Measured at the station
    the layout reads as clean, so only a bundle-aware guard can fail it.
    """
    graph = parse_metro_mermaid(FOUR_LANE_FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    section = graph.sections["sec"]
    pid = next(iter(section.exit_ports))
    reach = port_bundle_edge_reach(graph, pid, offsets, "y")[1]
    assert reach > perp_port_edge_clearance() + EPSILON, (
        f"{pid} carries only a {reach:.1f}px bundle reach; the plant needs one "
        "wider than the guard floor to hide a violation behind the station"
    )
    planted = section.bbox_y + section.bbox_h - reach
    graph.stations[pid].y = planted
    graph.ports[pid].y = planted

    with pytest.raises(PhaseInvariantError, match="bundle"):
        _guard_ports_clear_unanchored_box_edges(graph, "planted")


def test_carrier_row_entry_port_keeps_headroom() -> None:
    graph = parse_metro_mermaid(CARRIER_FIXTURE.read_text())
    compute_layout(graph)
    section = graph.sections["quantification"]
    pid = next(iter(section.entry_ports))
    clearance = graph.stations[pid].y - section.bbox_y
    assert clearance >= perp_port_edge_clearance() - EPSILON, (
        f"{pid} sits {clearance:.1f}px below its box top; the inbound bundle "
        "rides the border"
    )


def test_carrier_row_header_anchors_at_box_corner() -> None:
    """With the entry port clear of the top edge the header needs no nudge."""
    graph = parse_metro_mermaid(CARRIER_FIXTURE.read_text())
    compute_layout(graph)
    polylines = [
        rp.points
        for rp in route_edges_centred(
            graph, station_offsets=compute_station_offsets(graph)
        )
    ]
    placement = resolve_all_section_headers(graph, 14.0, polylines, 24.0)[
        "quantification"
    ]
    section = graph.sections["quantification"]
    assert placement.mode == "above"
    assert placement.badge_cx == pytest.approx(
        section.bbox_x + SECTION_NUM_CIRCLE_R_LARGE, abs=EPSILON
    )


def test_perpendicular_port_sides_follow_the_flow_axis() -> None:
    """The perpendicular pair is read off the lane axis, for every rotation.

    The corpus has no LR/RL section carrying both a TOP and a BOTTOM port and no
    BT section at all, so the X-axis and reversed-flow arms of the rule are
    pinned here rather than by a fixture.
    """
    assert perpendicular_port_sides("TB") == (PortSide.LEFT, PortSide.RIGHT)
    assert perpendicular_port_sides("BT") == (PortSide.LEFT, PortSide.RIGHT)
    assert perpendicular_port_sides("LR") == (PortSide.TOP, PortSide.BOTTOM)
    assert perpendicular_port_sides("RL") == (PortSide.TOP, PortSide.BOTTOM)


def test_carrier_row_ports_sit_symmetrically_in_their_box() -> None:
    """The two perpendicular ports' drawn lanes end equidistant from their edges.

    Measured off the outermost lane rather than the port station: the exit's
    bundle is staggered toward the bottom edge and the entry's away from the top
    one, so equal *station* clearances would put the two drawn ends unlike.
    """
    graph = parse_metro_mermaid(CARRIER_FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    section = graph.sections["quantification"]
    entry_id = next(iter(section.entry_ports))
    exit_id = next(iter(section.exit_ports))
    entry = graph.stations[entry_id]
    exit_ = graph.stations[exit_id]
    top_clearance = (
        entry.y
        - port_bundle_edge_reach(graph, entry_id, offsets, "y")[0]
        - section.bbox_y
    )
    bottom_clearance = (
        section.bbox_y
        + section.bbox_h
        - exit_.y
        - port_bundle_edge_reach(graph, exit_id, offsets, "y")[1]
    )
    assert top_clearance == pytest.approx(bottom_clearance, abs=EPSILON), (
        f"the entry's top lane sits {top_clearance:.1f}px below the box top but "
        f"the exit's bottom lane sits {bottom_clearance:.1f}px above its bottom"
    )


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_horizontal_perp_ports_keep_the_designed_inset(path: Path) -> None:
    """Every LR/RL perpendicular port keeps the full inset from both side edges.

    A corpus ratchet, not a runtime guard.  A render only aborts below the much
    smaller ``perp_port_edge_clearance()``, which leaves a novel map free to land
    a port short of the designed inset; the shipped corpus is held to it here.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    tight: list[str] = []
    for section in graph.sections.values():
        direction = section.direction or "LR"
        if section.bbox_w <= 0 or AxisFrame.axes_for_direction(direction)[0] != "x":
            continue
        for pid in (*section.entry_ports, *section.exit_ports):
            port = graph.ports.get(pid)
            station = graph.stations.get(pid)
            if (
                port is None
                or station is None
                or port.side not in perpendicular_port_sides(direction)
            ):
                continue
            near = min(
                station.x - section.bbox_x,
                section.bbox_x + section.bbox_w - station.x,
            )
            if near < PERP_PORT_EDGE_INSET - EPSILON:
                tight.append(f"{section.id}/{pid} clearance={near:.1f}")
    assert not tight, (
        f"perpendicular ports sit within {PERP_PORT_EDGE_INSET}px of a vertical "
        "box edge: " + ", ".join(tight)
    )


def _row_group_slacks(graph, section) -> list[float]:
    """Above-content slack for each of ``section``'s contiguous row-mates.

    Mirrors what ``_compact_row_content_to_bbox_top`` computes per section before
    taking the group minimum: how far content could rise before it would breach
    ``SECTION_Y_PADDING`` under the box top.
    """
    if AxisFrame.axes_for_direction(section.direction or "LR")[0] != "y":
        return []
    slacks = []
    for mate in graph.sections.values():
        if (
            mate.bbox_h <= 0
            or mate.grid_row != section.grid_row
            or abs(mate.grid_col - section.grid_col) > 1
        ):
            continue
        content = [
            graph.stations[sid].y
            for sid in mate.station_ids
            if sid in graph.stations and not graph.stations[sid].is_port
        ]
        if content:
            slacks.append(min(content) - mate.bbox_y - SECTION_Y_PADDING)
    return slacks


def _perp_pair_clearances(graph, section, axis, offsets):
    """``(low, high)`` clearance of the outermost perpendicular lanes, or None.

    Read off each port's outermost drawn lane, so a bundle staggered toward one
    edge is judged on what the viewer sees rather than on the port station.
    """
    sides = perpendicular_port_sides(section.direction or "LR")
    lanes = [
        (
            (graph.stations[pid].y if axis == "y" else graph.stations[pid].x),
            port_bundle_edge_reach(graph, pid, offsets, axis),
        )
        for pid in (*section.entry_ports, *section.exit_ports)
        if (port := graph.ports.get(pid)) is not None
        and port.side in sides
        and pid in graph.stations
    ]
    if len(lanes) < 2:
        return None
    low = section.bbox_y if axis == "y" else section.bbox_x
    high = low + (section.bbox_h if axis == "y" else section.bbox_w)
    return (
        min(coord - reach[0] for coord, reach in lanes) - low,
        high - max(coord + reach[1] for coord, reach in lanes),
    )


def _iter_perp_pairs(graph, want_axis):
    offsets = compute_station_offsets(graph)
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.bbox_w <= 0:
            continue
        axis = AxisFrame.axes_for_direction(section.direction or "LR")[0]
        if axis != want_axis:
            continue
        pair = _perp_pair_clearances(graph, section, axis, offsets)
        if pair is not None:
            yield section, pair


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_vertical_perp_port_pair_is_balanced_or_its_row_is_blocked(path: Path) -> None:
    """A vertical flow's perpendicular pair is equidistant unless its row is stuck.

    Compaction shifts a whole grid row by one uniform delta so the row's shared
    trunk Y survives it, so the delta is the minimum slack across the row.  A
    row-mate with no slack pins it at zero and the reserve has nothing to act on;
    balancing one section alone would drag its trunk off the row.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    for section, (low, high) in _iter_perp_pairs(graph, "y"):
        if abs(low - high) < EPSILON:
            continue
        slacks = _row_group_slacks(graph, section)
        assert slacks and min(slacks) <= EPSILON, (
            f"{section.id!r} perpendicular ports are asymmetric "
            f"({low:.1f} vs {high:.1f}) yet its row group has slack "
            f"{min(slacks) if slacks else None}; the reserve should have "
            "balanced them"
        )


@pytest.mark.parametrize(
    "path", _gather_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_perp_port_bundle_keeps_the_full_inset(path: Path) -> None:
    """A perpendicular port's outermost lane keeps ``PERP_PORT_EDGE_INSET``.

    The inset is what the bbox phases reserve beyond a perpendicular port toward
    the flow-axis edges -- ``port_edge_inset`` on Y,
    ``_reserve_perp_port_edge_inset`` on X -- and it is room the drawn lane owes
    the border, not room the port station owes it.  Both rotations are checked
    here, since either reservation could lose the reach independently.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    short: list[str] = []
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.bbox_w <= 0:
            continue
        direction = section.direction or "LR"
        axis = AxisFrame.axes_for_direction(direction)[0]
        for pid in (*section.entry_ports, *section.exit_ports):
            port = graph.ports.get(pid)
            station = graph.stations.get(pid)
            if port is None or station is None:
                continue
            if port.side not in perpendicular_port_sides(direction):
                continue
            low, high = port_bundle_edge_reach(graph, pid, offsets, axis)
            if axis == "y":
                coord, box_low, box_high = (
                    station.y,
                    section.bbox_y,
                    section.bbox_y + section.bbox_h,
                )
            else:
                coord, box_low, box_high = (
                    station.x,
                    section.bbox_x,
                    section.bbox_x + section.bbox_w,
                )
            clearance = min(coord - low - box_low, box_high - coord - high)
            if clearance < PERP_PORT_EDGE_INSET - EPSILON:
                short.append(f"{section.id}/{pid} {axis}-clearance={clearance:.1f}")
    assert not short, (
        "perpendicular ports whose outermost drawn lane sits within "
        f"{PERP_PORT_EDGE_INSET}px of the flow-axis box edge it faces: "
        + ", ".join(short)
    )


@pytest.mark.parametrize(
    ("path", "section_id", "direction"),
    (
        (REPO_ROOT / "tests/fixtures/tb_right_exit_feeder_slots.mmd", "sec", "TB"),
        (
            REPO_ROOT / "examples/topologies/bt_perp_left_entry_right_exit.mmd",
            "align",
            "BT",
        ),
    ),
    ids=("TB", "BT"),
)
def test_perp_port_bundle_sits_below_its_port_in_either_vertical_flow(
    path: Path, section_id: str, direction: str
) -> None:
    """A TB flow and its BT mirror stagger a perpendicular port's lanes alike.

    A vertical flow fans its *own* stations' lanes to one side under TB and the
    other under BT, so the mirror is where a sign error hides.  A perpendicular
    port's lanes are not on that axis: the run crossing its edge is horizontal,
    and every horizontal flow stacks lanes on +Y, so both mirrors put the bundle
    below the port and leave nothing above it.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    section = graph.sections[section_id]
    assert section.direction == direction
    reaches = {
        pid: port_bundle_edge_reach(graph, pid, offsets, "y")
        for pid in (*section.entry_ports, *section.exit_ports)
        if (port := graph.ports.get(pid)) is not None
        and port.side in perpendicular_port_sides(direction)
    }
    assert reaches, f"{section_id} carries no perpendicular port to measure"
    assert any(high > 0.0 for _low, high in reaches.values()), (
        f"no perpendicular port in {section_id} carries a multi-line bundle, so "
        "the fan side is unobservable here"
    )
    assert all(low == 0.0 for low, _high in reaches.values()), (
        f"a perpendicular port in {section_id} reaches above its station: {reaches}"
    )


_LR_PERP_PAIR_MMD = """\
%%metro title: LR perpendicular pair (top entry, bottom exit)
%%metro line: dna | DNA | #e6007e
%%metro grid: intake | 0,0
%%metro grid: mid | 0,1
%%metro grid: report | 0,2

graph LR
    subgraph intake [Intake]
        %%metro exit: bottom | dna
        samplesheet[Samplesheet]
        fastqc[FastQC]
        samplesheet -->|dna| fastqc
    end

    subgraph mid [Alignment]
        %%metro entry: top | dna
        %%metro exit: bottom | dna
        bwa[BWA-MEM2]
        sort[Sort BAM]
        bwa -->|dna| sort
    end

    subgraph report [Reporting]
        %%metro entry: top | dna
        multiqc[MultiQC]
    end

    fastqc -->|dna| bwa
    sort -->|dna| multiqc
"""
"""An LR section carrying both perpendicular ports, so both are free along X.

Held inline rather than as a corpus fixture: the shape also trips
``test_no_line_folds_back_over_its_track`` and the placement-purity suites, so
shipping it under ``examples/topologies/`` would red CI on defects unrelated to
the clearance this module is about.
"""


def test_horizontal_perp_port_pair_is_balanced() -> None:
    """An LR/RL TOP+BOTTOM pair sits alike on whichever edges the ports set.

    Balance is only owed where the ports are what hold the edges out.  An edge
    pushed further by content or by a routing band is not the pair's doing, and
    mirroring it onto the opposite edge buys symmetry with dead space --
    ``tb_bottom_exit_fork_diamond``'s ``mid`` grew 39px past its column mates
    that way.  So each port owes the inset, and the two owe each other equality
    only when both edges sit at the inset.
    """
    graph = parse_metro_mermaid(_LR_PERP_PAIR_MMD)
    compute_layout(graph)
    pairs = list(_iter_perp_pairs(graph, "x"))
    assert pairs, "the inline map no longer produces an X-axis perpendicular pair"
    for section, (low, high) in pairs:
        assert low >= PERP_PORT_EDGE_INSET - EPSILON, (
            f"{section.id}: low-edge clearance {low:.1f} below the inset"
        )
        assert high >= PERP_PORT_EDGE_INSET - EPSILON, (
            f"{section.id}: high-edge clearance {high:.1f} below the inset"
        )
        port_driven = (
            abs(low - PERP_PORT_EDGE_INSET) < EPSILON
            and abs(high - PERP_PORT_EDGE_INSET) < EPSILON
        )
        if port_driven:
            assert low == pytest.approx(high, abs=EPSILON), (
                f"{section.id}: both edges are port-driven yet unequal "
                f"({low:.1f} vs {high:.1f})"
            )
