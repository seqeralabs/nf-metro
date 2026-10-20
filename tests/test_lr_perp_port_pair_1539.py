"""An LR section carrying both of its perpendicular ports keeps its run in its box
and turns out of the BOTTOM exit through one clean bundle corner.

A ``%%metro entry: top`` plus ``%%metro exit: bottom`` on an LR section leaves it
with no flow-aligned port.  The perpendicular-entry runway shift then moves the
whole run along X, and three things have to follow it: the section's own right
edge, the BOTTOM exit's drop column, and the per-line lane order the exit corner
turns.  Where any one lags, the trailing station leaves its box, the exit leg
doubles back over the run, and the bundle's lanes swap sides through the bend.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from nf_metro.layout.constants import STATION_ELBOW_TOLERANCE
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.invariants import (
    CurveInvariantError,
    check_bundle_order_preserved,
    check_concentric_bundle_corners,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "examples/topologies/lr_perp_top_entry_bottom_exit.mmd"

# The two-line bundle from the issue: a TOP entry fanning to two aligners that
# reconverge on one sorter, whose track leaves through the BOTTOM exit.
BUNDLE_MAP = """\
%%metro title: LR Top-Entry / Bottom-Exit Perpendicular Ports
%%metro line: dna | DNA | #e63946
%%metro line: rna | RNA | #2db572

%%metro grid: qc | 0,0
%%metro grid: rna_prep | 1,0
%%metro grid: align | 0,1
%%metro grid: calling | 0,2

graph LR
    subgraph qc [Quality Control]
        %%metro exit: bottom | dna
        fastqc[FastQC]
        trim[Trim Galore]
        fastqc -->|dna| trim
    end

    subgraph rna_prep [RNA Prep]
        %%metro exit: bottom | rna
        umi[UMI Extract]
    end

    subgraph align [Alignment]
        %%metro entry: top | dna,rna
        %%metro exit: bottom | dna,rna
        bwa[BWA-MEM2]
        star[STAR]
        sort[Sort BAM]
        bwa -->|dna| sort
        star -->|rna| sort
    end

    subgraph calling [Downstream Analysis]
        %%metro entry: top | dna,rna
        variant[GATK Variant Calling]
        quant[Salmon Quant]
    end

    trim -->|dna| bwa
    umi -->|rna| star
    sort -->|dna| variant
    sort -->|rna| quant
"""


def _laid_out(text: str):
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    return graph


def _routed(graph):
    offsets = compute_station_offsets(graph)
    return offsets, route_edges_centred(graph, station_offsets=offsets)


def _perp_exit_ports(graph):
    return [
        (pid, port)
        for pid, port in graph.ports.items()
        if not port.is_entry
        and port.side in (PortSide.TOP, PortSide.BOTTOM)
        and graph.sections[port.section_id].direction in ("LR", "RL")
    ]


def test_fixture_validates() -> None:
    """The fixture lays out with every stage-boundary and final guard armed."""
    compute_layout(parse_metro_mermaid(FIXTURE.read_text()), validate=True)


def test_run_stays_inside_its_own_box() -> None:
    """No station the perpendicular-entry runway shift moved leaves its section."""
    for graph in (_laid_out(FIXTURE.read_text()), _laid_out(BUNDLE_MAP)):
        for sid, station in graph.stations.items():
            if station.is_port or not station.section_id:
                continue
            sec = graph.sections[station.section_id]
            assert sec.bbox_x <= station.x <= sec.bbox_x + sec.bbox_w, (
                f"{sid} at x={station.x} outside {sec.id} "
                f"[{sec.bbox_x}, {sec.bbox_x + sec.bbox_w}]"
            )


def test_perp_exit_seats_past_the_trailing_station() -> None:
    """Every LR/RL perpendicular exit sits downstream of the flow-end station by
    more than the elbow tolerance, so the turn falls after the marker rather than
    doubling the exit leg back along the run.  A trunk shift later in the stage
    can eat into the requested seat, so the bar is the elbow tolerance rather than
    the full requested clearance."""
    clearance = STATION_ELBOW_TOLERANCE
    for graph in (_laid_out(FIXTURE.read_text()), _laid_out(BUNDLE_MAP)):
        for pid, port in _perp_exit_ports(graph):
            sec = graph.sections[port.section_id]
            internal_xs = [
                graph.stations[sid].x
                for sid in sec.station_ids
                if not graph.stations[sid].is_port
            ]
            trailing = min(internal_xs) if sec.direction == "RL" else max(internal_xs)
            gap = (trailing - graph.stations[pid].x) * (
                1 if sec.direction == "RL" else -1
            )
            assert gap >= clearance - 1e-6, f"{pid} only {gap}px past {trailing}"


def test_exit_bundle_turns_as_one_concentric_corner() -> None:
    """The two-line bundle leaving the BOTTOM exit keeps its lane order and one
    shared arc centre through the turn."""
    graph = _laid_out(BUNDLE_MAP)
    offsets, routes = _routed(graph)
    exit_ids = {pid for pid, _ in _perp_exit_ports(graph)}
    order = [
        v
        for v in check_bundle_order_preserved(routes)
        if v.edge_target in exit_ids or v.edge_source in exit_ids
    ]
    concentric = [
        v
        for v in check_concentric_bundle_corners(graph, routes, offsets)
        if v.edge_target in exit_ids or v.edge_source in exit_ids
    ]
    assert not order, "\n".join(v.message() for v in order)
    assert not concentric, "\n".join(v.message() for v in concentric)


@pytest.mark.xfail(
    raises=CurveInvariantError,
    strict=True,
    reason="#2096: the approach fan's two feeders sit in separate corridor "
    "bundles, so dna and rna both descend on the TOP entry's X",
)
def test_bundle_map_renders() -> None:
    """The two-line map renders instead of aborting on defective bundle curves."""
    from nf_metro.api import render_string

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert render_string(BUNDLE_MAP)
