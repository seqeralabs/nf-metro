"""A partial row-mate seats its direct handover lane flat (#1844).

When a section carries only part of a row's through-bundle and hands one of
those lines straight to an adjacent carrier -- port to port, no junction and no
cell-mate forcing a bypass -- its trunk must sit at that line's own lane on the
shared boundary, one lane below the carrier trunk, so the connector runs level
instead of jogging diagonally into the shared port.

The descent only fires when the offset step is stable: a step read mid-layout
before a carrier's exit port settles onto its lines' level can be a transient
that collapses once the port settles, and seating a descent on that phantom step
corrupts the carrier's exit geometry.  The restore that re-applies the descent
after the grid snap is carrier-relative, so it lands right whether the snap
collapsed the sub-grid offset (shared row grid) or preserved it (explicit-grid
solo section) and cannot double-apply.
"""

from __future__ import annotations

from pathlib import Path

from nf_metro.layout import engine
from nf_metro.layout.phases._common import _section_trunk_y
from nf_metro.layout.phases.grid_snap import _restore_partial_trunk_descents
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render import build_render_plan, emit_render_plan
from nf_metro.render.validate import parse_route_polylines
from nf_metro.themes import THEMES

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "examples" / "variantbenchmarking_auto.mmd"

CARRIER = "benchmarking"
PARTIAL = "ensembl_truth"
PARTIAL_EXIT = "ensembl_truth__exit_left_4"

# A carrier whose exit port hands its side-branch lines to a partial one column
# away reads an inflated offset step mid-Stage-4.8 (the exit port has not yet
# settled onto the level its lines live at), but that step collapses to zero once
# the port settles: no real kink, so no descent may fire.
SIDE_BRANCH_FIXTURE = (
    REPO / "examples" / "topologies" / "side_branch_ascent_label_strike.mmd"
)
SIDE_BRANCH_PARTIAL = "down"

# An explicit-grid single-row chain where the partial descent survives but its
# section lands in an empty row grid, so the grid snap does not collapse the
# descent onto the carrier slot.  A blind re-add would double-apply it.
EXPLICIT_GRID_MMD = """\
%%metro title: Adjacent Direct Partial Descent
%%metro style: dark
%%metro compact_offsets: true
%%metro line: main | Main | #4CAF50
%%metro line: side | Side | #ff9800
%%metro grid: up | 0,0
%%metro grid: partial | 1,0
%%metro grid: carrier | 2,0
%%metro grid: down | 3,0

graph LR
    subgraph up [Upstream]
        %%metro exit: right | main, side
        u_node[Source]
    end

    subgraph partial [Partial]
        %%metro entry: left | side
        %%metro exit: right | side
        p_node[Side Step]
    end

    subgraph carrier [Carrier]
        %%metro entry: left | main, side
        %%metro exit: right | main, side
        c_node[Carrier]
    end

    subgraph down [Report]
        %%metro entry: left | main, side
        d_node[Report]
    end

    u_node -->|side| p_node
    u_node -->|main| c_node
    p_node -->|side| c_node
    c_node -->|main,side| d_node
"""


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    engine.compute_layout(graph, validate=True)
    return graph


def test_partial_trunk_descends_below_carrier():
    """The partial's trunk seats one lane below the carrier trunk, not on it."""
    graph = _layout()
    carrier_trunk = _section_trunk_y(graph, graph.sections[CARRIER])
    partial_trunk = _section_trunk_y(graph, graph.sections[PARTIAL])
    assert carrier_trunk is not None and partial_trunk is not None
    descent = partial_trunk - carrier_trunk
    assert descent > 1.0, (
        f"partial trunk {partial_trunk} should sit below carrier trunk "
        f"{carrier_trunk}; a coincident Y is the diagonal-jog bug"
    )


def test_partial_exit_port_rides_its_own_trunk():
    """The handover exit port sits on the partial's trunk, so its leg is flat."""
    graph = _layout()
    partial_trunk = _section_trunk_y(graph, graph.sections[PARTIAL])
    exit_st = graph.stations[PARTIAL_EXIT]
    assert abs(exit_st.y - partial_trunk) < 1.0, (
        f"exit port {PARTIAL_EXIT} at {exit_st.y} must ride the partial trunk "
        f"{partial_trunk} for a flat handover"
    )


def test_partial_handover_connector_is_flat():
    """The rendered truth line runs level out of the partial's exit port."""
    graph = _layout()
    plan = build_render_plan(graph, THEMES["nfcore"])
    svg = emit_render_plan(plan)
    exit_x = round(graph.stations[PARTIAL_EXIT].x, 1)
    exit_y = round(graph.stations[PARTIAL_EXIT].y, 1)
    flat_run = None
    for line_id, subpaths in parse_route_polylines(svg):
        if line_id != "truth":
            continue
        for sub in subpaths:
            pts = [(round(x, 1), round(y, 1)) for x, y in sub]
            if any(abs(px - exit_x) < 1.0 and abs(py - exit_y) < 1.0 for px, py in pts):
                flat_run = pts
    assert flat_run is not None, "no truth subpath starts at the partial exit port"
    ys = {y for _, y in flat_run}
    assert len(ys) == 1, (
        f"truth handover connector jogs across Ys {sorted(ys)}; expected one flat lane"
    )


def test_phantom_step_descent_is_suppressed():
    """A step that collapses when the carrier's exit port settles fires no descent.

    Seating a descent on the mid-layout step corrupts the carrier's exit port and
    aborts the render, so the section must stay on the carrier trunk.
    """
    graph = parse_metro_mermaid(SIDE_BRANCH_FIXTURE.read_text())
    engine.compute_layout(graph, validate=True)
    assert SIDE_BRANCH_PARTIAL not in graph._partial_trunk_descents, (
        f"section {SIDE_BRANCH_PARTIAL!r} descended on a phantom offset step; its "
        "carrier's exit port had not settled when the step was sampled"
    )


def test_restore_is_idempotent_on_explicit_grid():
    """The grid-snap restore seats an explicit-grid descent once, not twice.

    On a section that lands in an empty row grid the snap leaves the descent in
    place, so the carrier-relative restore must find zero gap and add nothing --
    a second restore must not move the section again.
    """
    graph = parse_metro_mermaid(EXPLICIT_GRID_MMD)
    engine.compute_layout(graph, validate=False)
    record = graph._partial_trunk_descents.get("partial")
    assert record is not None, "explicit-grid partial should record a descent"
    partial_st = graph.stations[record.partial_port]
    carrier_st = graph.stations[record.carrier_port]
    expected = carrier_st.y + record.descent
    assert abs(partial_st.y - expected) < 0.5, (
        f"handover port {record.partial_port} at {partial_st.y} should sit "
        f"descent={record.descent} below carrier {record.carrier_port} "
        f"(expected {expected}); a double-apply lands it 2x below"
    )
    seated_y = partial_st.y
    _restore_partial_trunk_descents(graph)
    assert partial_st.y == seated_y, (
        f"a second restore moved {record.partial_port} from {seated_y} to "
        f"{partial_st.y}; the restore is not idempotent"
    )
