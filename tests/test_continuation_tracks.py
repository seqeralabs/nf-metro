from pathlib import Path

import networkx as nx
import pytest

from nf_metro.api import prepare_graph, render_string
from nf_metro.layout import compute_layout
from nf_metro.layout.constants import SAME_COORD_TOLERANCE, graph_offset_step
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.layout.ordering import _place_single_node
from nf_metro.layout.phases import _common as phase_common
from nf_metro.layout.phases._common import (
    continuation_track_is_realizable,
    continuation_track_predecessors,
    section_lane_axis,
)
from nf_metro.layout.phases.fan_bundles import _carry_full_bundle_continuations
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_post_convergence_trunk_continues,
)
from nf_metro.layout.routing import compute_station_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import PortSide

ROOT = Path(__file__).parents[1]
SEED_72 = ROOT / "tests" / "fixtures" / "hash_seed_determinism" / "seed_72.mmd"


def _transition_graph(
    direction: str, declarations: str = "pred\n        node\n        tail"
):
    return prepare_graph(
        f"""
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
%%metro grid: feeder | 0,0
%%metro grid: target | 1,1
%%metro grid: sink_section | 2,2
graph LR
    subgraph feeder [Feeder]
        source
    end
    subgraph target [Target]
        %%metro direction: {direction}
        {declarations}
        pred -->|second| node
        node -->|second| tail
    end
    subgraph sink_section [Sink]
        sink
    end
    source -->|first,second| pred
    tail -->|second| sink
"""
    )


def _lane_columns(graph, station_ids: tuple[str, ...]) -> list[float]:
    """The settled lane-axis coordinate of each station in one section."""
    section_id = graph.stations[station_ids[0]].section_id
    frame = AxisFrame.for_direction(graph.sections[section_id].direction, 1.0, 1.0)
    return [frame.secondary.get(graph.stations[sid]) for sid in station_ids]


@pytest.mark.parametrize("direction", ("LR", "RL"))
def test_transition_seed_and_closure_are_mirror_stable(direction: str) -> None:
    forward = _transition_graph(direction)
    reversed_declarations = _transition_graph(
        direction, "tail\n        node\n        pred"
    )

    assert continuation_track_predecessors(forward) == {
        "node": "pred",
        "tail": "node",
    }
    assert continuation_track_predecessors(reversed_declarations) == {
        "node": "pred",
        "tail": "node",
    }


def test_guard_checks_equal_line_closure_members() -> None:
    graph = _transition_graph("LR")
    compute_layout(graph)
    graph.stations["tail"].y += 20.0

    with pytest.raises(PhaseInvariantError, match="continuation station 'tail'"):
        _guard_post_convergence_trunk_continues(graph, "mutation")


@pytest.mark.parametrize("reverse", (False, True))
def test_unrelated_layer_occupant_blocks_continuation_track_carry(
    reverse: bool,
) -> None:
    declarations = "pred\n        node\n        tail\n        occupant"
    if reverse:
        declarations = "occupant\n        tail\n        node\n        pred"
    graph = _transition_graph("LR", declarations)
    section_id = graph.stations["node"].section_id
    assert section_id is not None
    graph.stations["pred"].layer = 0
    graph.stations["pred"].y = 100.0
    graph.stations["node"].layer = 1
    graph.stations["node"].y = 140.0
    graph.stations["occupant"].section_id = section_id
    graph.stations["occupant"].layer = 1
    graph.stations["occupant"].y = 100.0

    inherited = continuation_track_predecessors(graph)

    assert inherited["node"] == "pred"
    assert not continuation_track_is_realizable(graph, "node", "pred")
    _carry_full_bundle_continuations(graph)
    assert graph.stations["node"].y == 140.0
    _guard_post_convergence_trunk_continues(graph, "test")


def test_unrelated_layer_occupant_blocks_initial_continuation_track() -> None:
    graph = parse_metro_mermaid(
        """
%%metro line: route | Route | #3779b1
graph LR
    subgraph target [Target]
        pred -->|route| node
        occupant
    end
"""
    )
    dependency_graph: nx.DiGraph[str] = nx.DiGraph()
    for edge in graph.edges:
        dependency_graph.add_edge(edge.source, edge.target)

    track = _place_single_node(
        "node",
        40.0,
        40.0,
        dependency_graph,
        {"pred": 0.0, "occupant": 0.0},
        graph,
        {"pred": 0, "node": 1, "occupant": 1},
        layer_occupancy={1: {"occupant": 0.0}},
        continuation_predecessors={"node": "pred"},
    )

    assert track == 40.0


def test_hidden_merge_is_not_a_continuation_seed() -> None:
    path = ROOT / "examples" / "hlatyping.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    assert "fastqc" not in continuation_track_predecessors(graph)


def test_visible_merge_is_not_a_continuation_seed() -> None:
    graph = prepare_graph(
        """
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
graph LR
    subgraph target [Target]
        left -->|first| node
        right -->|second| node
        node -->|second| tail
    end
"""
    )

    inherited = continuation_track_predecessors(graph)

    assert "node" not in inherited


def test_file_terminal_occupancy_is_not_a_continuation_seed() -> None:
    graph = prepare_graph(
        """
%%metro line: route | Route | #3779b1
%%metro file: output | TXT
graph LR
    subgraph feeder [Feeder]
        source
    end
    subgraph target [Target]
        pred -->|route| node
        node -->|route| output
    end
    source -->|route| pred
"""
    )

    assert "node" not in continuation_track_predecessors(graph)


def test_external_exit_fanout_is_not_a_continuation_seed() -> None:
    graph = prepare_graph(
        """
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
graph LR
    subgraph source [Source]
        pred -->|second| node
    end
    subgraph sink [Sink]
        done
    end
    pred -->|first| done
"""
    )

    assert "node" not in continuation_track_predecessors(graph)


_REOCCUPATION_AROUND_THE_BOX = """
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
graph LR
    subgraph feeder [Feeder]
        source
    end
    subgraph target [Target]
        %%metro direction: {direction}
        pred -->|second| node
        node -->|second| tail
    end
    subgraph bypass_section [Bypass]
        bypass
    end
    subgraph sink_section [Sink]
        sink
    end
    source -->|first,second| pred
    source -->|first| bypass
    bypass -->|first| sink
    tail -->|second| sink
"""


def test_downstream_line_reoccupation_rejects_transition_seed() -> None:
    graph = prepare_graph(_REOCCUPATION_AROUND_THE_BOX.format(direction="LR"))

    assert "node" not in continuation_track_predecessors(graph)


@pytest.mark.parametrize("direction", ("TB", "BT"))
def test_vertical_reoccupation_around_the_box_seeds_inheritance(
    direction: str,
) -> None:
    """A dropped line that reaches the sink around a vertical section needs no lane.

    ``first`` stops at ``pred`` and reaches ``sink`` again through another
    section, so its other route never runs beside ``pred -> node`` in the
    column.
    """
    graph = prepare_graph(_REOCCUPATION_AROUND_THE_BOX.format(direction=direction))

    assert continuation_track_predecessors(graph)["node"] == "pred"


@pytest.mark.parametrize("direction", ("TB", "BT"))
def test_vertical_bypass_inside_the_box_rejects_transition_seed(
    direction: str,
) -> None:
    """A dropped line that rejoins inside the section keeps the chain apart.

    ``first`` stops at ``pred`` but reaches ``tail`` through the section's own
    entry, so it runs past ``node`` inside the column.
    """
    graph = prepare_graph(
        f"""
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
graph LR
    subgraph feeder [Feeder]
        source
    end
    subgraph target [Target]
        %%metro direction: {direction}
        pred -->|second| node
        node -->|second| tail
    end
    subgraph sink_section [Sink]
        sink
    end
    source -->|first,second| pred
    source -->|first| tail
    tail -->|first,second| sink
"""
    )

    assert "node" not in continuation_track_predecessors(graph)


def test_seed72_layout_keeps_only_proven_linear_inheritance() -> None:
    graph = prepare_graph(
        (ROOT / "examples" / "topologies" / "seed72_cross_family_fan.mmd").read_text()
    )

    assert continuation_track_predecessors(graph) == {
        "blocked_out": "blocked_in",
        "exempt_done": "exempt_in",
        "split": "prepare",
    }
    compute_layout(graph)


def test_added_line_continuing_through_flow_axis_exit_seeds_inheritance() -> None:
    path = ROOT / "examples" / "topologies" / "recompacted_fanout_exit.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    inherited = continuation_track_predecessors(graph)

    assert inherited["paired_step"] == "paired_in"
    assert inherited["paired_out"] == "paired_step"

    compute_layout(graph)

    assert {
        station_id: graph.stations[station_id].y
        for station_id in ("paired_in", "paired_step", "paired_out")
    } == {
        "paired_in": 120.0,
        "paired_step": 120.0,
        "paired_out": 120.0,
    }


# (fixture, section, predecessor, node) for a chain whose node both drops one
# line and picks up another that leaves the section through a flow-side exit
# port.  ``recompacted_fanout_exit`` hands that line to the mirror-side entry of
# the next section along the flow; ``same_destination_vertical_convergence``
# hands it to a same-side entry, which reverses.  The hand-off proves the node
# continues the chain either way, so the chain must hold one row in both.
_ADDED_LINE_EXIT_CONTINUATIONS = [
    ("topologies/recompacted_fanout_exit.mmd", "paired", "paired_in", "paired_step"),
    ("topologies/recompacted_fanout_exit.mmd", "paired", "paired_step", "paired_out"),
    ("topologies/same_destination_vertical_convergence.mmd", "s5", "n5_0", "n5_1"),
    ("topologies/same_destination_vertical_convergence.mmd", "s5", "n5_1", "n5_2"),
]


@pytest.mark.parametrize(
    "fixture,section_id,predecessor,node", _ADDED_LINE_EXIT_CONTINUATIONS
)
def test_added_line_leaving_through_an_exit_seeds_inheritance_either_way(
    fixture: str, section_id: str, predecessor: str, node: str
) -> None:
    """A flow-side exit hand-off seeds inheritance whichever side it lands on."""
    path = ROOT / "examples" / fixture
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))

    assert continuation_track_predecessors(graph).get(node) == predecessor

    compute_layout(graph)
    section = graph.sections[section_id]
    rows = {
        station_id: graph.stations[station_id].y
        for station_id in section.station_ids
        if not graph.stations[station_id].is_port
    }
    assert len(set(rows.values())) == 1, (
        f"{fixture}: section {section_id} stations straddle rows {rows}; the "
        "chain steps off its own trunk"
    )


def test_seed72_real_pipeline_proves_every_horizontal_chain() -> None:
    graph = prepare_graph(SEED_72.read_text(), source_dir=str(SEED_72.parent))

    assert continuation_track_predecessors(graph) == {
        "n1_1": "n1_0",
        "n1_2": "n1_1",
        "n2_1": "n2_0",
        "n2_2": "n2_1",
        "n3_1": "n3_0",
        "n3_2": "n3_1",
        "n4_1": "n4_0",
        "n5_1": "n5_0",
        "n5_2": "n5_1",
        "n6_1": "n6_0",
        "n7_1": "n7_0",
        "n7_2": "n7_1",
        "n7_3": "n7_2",
        "n8_1": "n8_0",
    }


def test_long_linear_pipeline_needs_no_bypass_reachability_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    station_count = 3000
    edges = "\n".join(
        f"        n{index} -->|route| n{index + 1}"
        for index in range(station_count - 1)
    )
    graph = parse_metro_mermaid(
        f"""
%%metro line: route | Route | #3779b1
graph LR
    subgraph linear [Linear]
{edges}
    end
"""
    )
    query_count = 0
    real_query = phase_common._line_bypasses_boundary

    def counted_query(*args, **kwargs):
        nonlocal query_count
        query_count += 1
        return real_query(*args, **kwargs)

    monkeypatch.setattr(phase_common, "_line_bypasses_boundary", counted_query)

    inherited = continuation_track_predecessors(graph)

    assert len(graph.stations) == station_count
    assert len(graph.edges) == station_count - 1
    assert len(inherited) == station_count - 1
    assert inherited["n2999"] == "n2998"
    assert query_count == 0


@pytest.mark.parametrize("direction", ("TB", "BT"))
def test_vertical_flow_names_its_continuation_and_keeps_one_lane(
    direction: str,
) -> None:
    """A vertical section's chain is named by the relation and holds one lane."""
    graph = _transition_graph(direction)
    assert lanes_run_along_x(graph.sections["target"].direction)

    assert continuation_track_predecessors(graph) == {"node": "pred", "tail": "node"}

    compute_layout(graph)
    lanes = _lane_columns(graph, ("pred", "node", "tail"))

    assert max(lanes) - min(lanes) <= SAME_COORD_TOLERANCE, lanes


def test_tb_passthrough_fixture_names_its_merge_tail_continuation() -> None:
    """The TB passthrough corpus fixture's merge->tail chain shares one column.

    ``merge`` is ``tail``'s only predecessor and ``tail`` its only target, so the
    chain is a sole continuation by the section's own edges and the relation
    names it.
    """
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    assert lanes_run_along_x(graph.sections["work"].direction)

    assert continuation_track_predecessors(graph) == {"tail": "merge"}
    assert [edge.source for edge in graph.edges_to("tail")] == ["merge"]
    assert [edge.target for edge in graph.edges_from("merge")] == ["tail"]

    compute_layout(graph)
    merge_lane, tail_lane = _lane_columns(graph, ("merge", "tail"))

    assert abs(tail_lane - merge_lane) <= SAME_COORD_TOLERANCE


_DROPPED_LINE_BYPASS_CHAIN = """
%%metro line: m0 | L0 | #e6194b
%%metro line: m1 | L1 | #3cb44b
%%metro line: m3 | L3 | #f58231
graph LR
    subgraph a [A]
        a0[A0]
    end
    subgraph b [B]
        %%metro direction: {direction}
        b0[B0]
        b1[B1]
        b2[B2]
        b0 -->|m3| b1
        b1 -->|m0| b2
    end
    subgraph c [C]
        c0[C0]
    end
    a0 -->|m0,m1,m3| b0
    a0 -->|m1| c0
    b2 -->|m0| c0
"""


@pytest.mark.parametrize("direction", ("TB", "BT", "LR"))
def test_chain_holds_its_lane_past_a_line_rerouted_around_the_box(
    direction: str,
) -> None:
    """A chain keeps one lane when a line it drops reaches the sink another way.

    ``m1`` stops at ``B0`` and also runs from ``A0`` straight to ``C``, outside
    section ``B``, so it takes no lane beside the chain.
    """
    graph = parse_metro_mermaid(_DROPPED_LINE_BYPASS_CHAIN.format(direction=direction))
    compute_layout(graph)
    lane = section_lane_axis(graph, "b")
    lanes = [lane.get(graph.stations[station_id]) for station_id in ("b0", "b1", "b2")]

    assert max(lanes) - min(lanes) <= SAME_COORD_TOLERANCE, lanes


TB_LINE_CHANGE_CHAIN = ROOT / "examples" / "topologies" / "tb_line_change_chain.mmd"
LR_LINE_HANDOFF = ROOT / "examples" / "topologies" / "lr_line_handoff_fanning_exit.mmd"
LR_HANDOFF_PEEL_ORDER = (
    ROOT / "examples" / "topologies" / "lr_handoff_fanning_exit_peel_order.mmd"
)


@pytest.mark.parametrize("direction", ("TB", "BT"))
def test_vertical_chain_holds_its_lane_column_across_a_line_change(
    direction: str,
) -> None:
    """A vertical chain that swaps lines mid-way stays in one lane column."""
    text = TB_LINE_CHANGE_CHAIN.read_text().replace(
        "%%metro direction: TB", f"%%metro direction: {direction}"
    )
    graph = parse_metro_mermaid(text)
    chain = ("t0", "t1", "t2", "t3")

    assert continuation_track_predecessors(graph) == {
        "a1": "a0",
        "t1": "t0",
        "t2": "t1",
        "t3": "t2",
    }

    compute_layout(graph, validate=True)
    lanes = _lane_columns(graph, chain)

    assert max(lanes) - min(lanes) <= SAME_COORD_TOLERANCE, dict(zip(chain, lanes))


@pytest.mark.parametrize("direction", ("LR", "TB"))
def test_line_handed_to_a_fanning_exit_seeds_inheritance(direction: str) -> None:
    """A line leaving through an exit that fans out via a junction continues.

    The exit port feeds a hidden junction that splits to two downstream entry
    ports, so the entry that proves the hand-off sits two hops past the exit.
    """
    text = LR_LINE_HANDOFF.read_text().replace(
        "    subgraph b [B]\n",
        f"    subgraph b [B]\n        %%metro direction: {direction}\n",
    )
    graph = parse_metro_mermaid(text)
    assert graph.sections["b"].direction == direction
    exit_targets = {
        edge.target
        for port_id in graph.sections["b"].exit_ports
        for edge in graph.edges_from(port_id)
    }
    assert exit_targets <= graph.junction_ids
    chain = ("b0", "b1", "b2")

    assert continuation_track_predecessors(graph) == {"b1": "b0", "b2": "b1"}

    compute_layout(graph, validate=True)
    lanes = _lane_columns(graph, chain)

    assert max(lanes) - min(lanes) <= SAME_COORD_TOLERANCE, dict(zip(chain, lanes))


@pytest.mark.parametrize("direction", ("TB", "BT"))
def test_vertical_line_change_chain_packs_its_lanes(direction: str) -> None:
    """Every station of a vertical line-change chain holds its lines on
    adjacent lanes, and each line keeps one lane down the column.

    ``T0`` carries Line 1 and Line 3 but never Line 2, so Line 2's lane
    between them is dead there; closing it has to move Line 3 down the whole
    column rather than at ``T0`` alone, or Line 3 jogs on its way to ``T1``.
    """
    text = TB_LINE_CHANGE_CHAIN.read_text().replace(
        "%%metro direction: TB", f"%%metro direction: {direction}"
    )
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    step = graph_offset_step(graph)
    chain = ("t0", "t1", "t2", "t3")

    for station_id in chain:
        lanes = sorted(
            offsets[(station_id, lid)] for lid in graph.station_lines(station_id)
        )
        assert lanes[-1] - lanes[0] == pytest.approx(step * (len(lanes) - 1)), (
            station_id,
            lanes,
        )
    for line_id in {lid for sid in chain for lid in graph.station_lines(sid)}:
        lanes = {
            offsets[(sid, line_id)]
            for sid in chain
            if line_id in graph.station_lines(sid)
        }
        assert len(lanes) == 1, (line_id, lanes)


def test_gap_compaction_keeps_a_straightened_handoff_in_peel_order() -> None:
    """Closing a feeder's lane gap does not flip the bundle it hands off.

    ``a1`` carries ``l0`` and ``l2`` with ``l1``'s slot empty between them, so
    compaction lifts ``l2`` a slot, and ``a2`` -- on ``a1``'s row -- follows it
    to keep the run level.  ``a2``'s other line, ``l1``, also runs on to ``b``
    on this row while ``l2`` only drops to ``c``, so ``l1`` has to stay above
    ``l2`` out through the exit, or ``l2``'s drop crosses ``l1``'s level run
    where the fan peels.
    """
    graph = parse_metro_mermaid(LR_HANDOFF_PEEL_ORDER.read_text())
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    (exit_port,) = graph.sections["a"].exit_ports

    assert graph.stations["a1"].y == graph.stations["a2"].y
    assert offsets["a1", "l2"] == offsets["a2", "l2"]
    for station_id in ("a2", exit_port):
        assert offsets[station_id, "l1"] < offsets[station_id, "l2"], station_id


_COMPACTION_LINES = "\n".join(
    f"%%metro line: m{index} | L{index} | {colour}"
    for index, colour in enumerate(
        ("#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4")
    )
)

_PERPENDICULAR_ENTRY_SWAP = """
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x0 -->|m0,m1,m2| q0x1
    end
    subgraph z1 [Zone 1]
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x0 -->|m1| q1x1
    end
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x0 -->|m2| q2x1
        q2x1 -->|m0| q2x2
    end
    q0x1 -->|m0,m1| q1x0
    q1x1 -->|m0,m1,m2| q2x0
    q0x1 -->|m0,m1| q2x0
    q0x1 -->|m0,m1,m2| q1x0
"""

# Each map closes a lane gap whose propagation lands on a slot another line
# holds at the next station, and each needs that line re-seated differently.
COMPACTION_COLLISION_MAPS = {
    # Both lines leave through one exit and enter the downstream section
    # together, which fixes their order there; no fan has a say in it.
    "shared_exit_no_fan": """
graph LR
    subgraph z0 [Zone 0]
        %%metro direction: TB
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x0 -->|m2| q0x1
        q0x1 -->|m2| q0x2
    end
    subgraph z1 [Zone 1]
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x0 -->|m3| q1x1
        q1x1 -->|m0| q1x2
        q1x2 -->|m1| q1x3
    end
    subgraph z2 [Zone 2]
        %%metro direction: BT
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x0 -->|m2| q2x1
    end
    q0x2 -->|m3| q1x0
    q0x2 -->|m4| q2x0
    q1x3 -->|m4| q2x0
    q1x3 -->|m1| q2x1
""",
    # The displaced line's free slot beyond the mover is the lane a bypass
    # rides past the station.
    "slot_beyond_held_by_bypass": """
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x3[Q0X3]
        q0x4[Q0X4]
        q0x0 -->|m2| q0x1
        q0x1 -->|m1| q0x2
        q0x2 -->|m3| q0x3
        q0x3 -->|m1| q0x4
    end
    subgraph z1 [Zone 1]
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x0 -->|m0| q1x1
        q1x1 -->|m2| q1x2
        q1x2 -->|m1| q1x3
    end
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x3[Q2X3]
        q2x4[Q2X4]
        q2x0 -->|m1| q2x1
        q2x1 -->|m0| q2x2
        q2x2 -->|m0| q2x3
        q2x3 -->|m3| q2x4
    end
    subgraph z3 [Zone 3]
        q3x0[Q3X0]
    end
    subgraph z4 [Zone 4]
        q4x0[Q4X0]
    end
    q0x4 -->|m1| q1x0
    q1x0 -->|m0| q2x0
    q2x4 -->|m1| q3x0
    q3x0 -->|m0| q4x0
""",
    # The fan delivers both lines into a vertical section through its
    # bottom entry, whose order the displaced line has to keep.
    "fan_into_bottom_entry": """
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x0 -->|m0| q0x1
        q0x1 -->|m3| q0x2
    end
    subgraph z1 [Zone 1]
        %%metro direction: BT
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x4[Q1X4]
        q1x0 -->|m0| q1x1
        q1x1 -->|m0| q1x2
        q1x2 -->|m3| q1x3
        q1x3 -->|m3| q1x4
    end
    subgraph z2 [Zone 2]
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x0 -->|m0| q2x1
        q2x1 -->|m0| q2x2
    end
    subgraph z3 [Zone 3]
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x2[Q3X2]
        q3x3[Q3X3]
        q3x4[Q3X4]
        q3x0 -->|m1| q3x1
        q3x1 -->|m1| q3x2
        q3x2 -->|m3| q3x3
        q3x3 -->|m1| q3x4
    end
    q0x2 -->|m2| q1x0
    q0x2 -->|m3| q1x0
    q1x4 -->|m3| q2x0
    q2x2 -->|m0| q3x0
    q0x2 -->|m3| q2x0
""",
    # The same through a side entry of a vertical section a row below.
    "fan_into_side_entry_of_vertical_section": """
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x3[Q0X3]
        q0x4[Q0X4]
        q0x0 -->|m0| q0x1
        q0x1 -->|m0| q0x2
        q0x2 -->|m0| q0x3
        q0x3 -->|m3| q0x4
    end
    subgraph z1 [Zone 1]
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x3[Q1X3]
        q1x0 -->|m1| q1x1
        q1x1 -->|m1| q1x2
        q1x2 -->|m1| q1x3
    end
    subgraph z2 [Zone 2]
        %%metro direction: TB
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x3[Q2X3]
        q2x4[Q2X4]
        q2x0 -->|m1| q2x1
        q2x1 -->|m1| q2x2
        q2x2 -->|m1| q2x3
        q2x3 -->|m2| q2x4
    end
    subgraph z3 [Zone 3]
        %%metro direction: TB
        q3x0[Q3X0]
        q3x1[Q3X1]
        q3x0 -->|m3| q3x1
    end
    subgraph z4 [Zone 4]
        q4x0[Q4X0]
        q4x1[Q4X1]
        q4x2[Q4X2]
        q4x3[Q4X3]
        q4x0 -->|m0| q4x1
        q4x1 -->|m0| q4x2
        q4x2 -->|m0| q4x3
    end
    q0x4 -->|m2| q1x0
    q1x3 -->|m2| q2x0
    q0x4 -->|m2| q3x0
    q0x4 -->|m3| q3x0
    q3x1 -->|m0| q4x0
    q3x1 -->|m2| q4x0
""",
    # Closing the lane gap at a vertical section's second station swaps the
    # pair its side entry port feeds into the first station, across the turn
    # the port's lanes take into the column.
    "swap_across_perpendicular_entry": _PERPENDICULAR_ENTRY_SWAP,
    # The displaced line's slot beyond the mover is free, but sliding it there
    # would leave the mover's vacated slot empty inside the station's bundle.
    "slot_beyond_strands_vacated": """
graph LR
    subgraph z0 [Zone 0]
        q0x0[Q0X0]
        q0x1[Q0X1]
        q0x2[Q0X2]
        q0x0 -->|m0,m3| q0x1
        q0x1 -->|m0,m1| q0x2
    end
    subgraph z1 [Zone 1]
        q1x0[Q1X0]
        q1x1[Q1X1]
        q1x2[Q1X2]
        q1x0 -->|m1| q1x1
        q1x1 -->|m1| q1x2
    end
    subgraph z2 [Zone 2]
        %%metro direction: RL
        q2x0[Q2X0]
        q2x1[Q2X1]
        q2x2[Q2X2]
        q2x3[Q2X3]
        q2x0 -->|m0| q2x1
        q2x1 -->|m0| q2x2
        q2x2 -->|m1| q2x3
    end
    q0x2 -->|m3| q1x0
    q0x2 -->|m1| q1x0
    q0x2 -->|m2| q2x0
    q1x2 -->|m2| q2x3
    q0x2 -->|m1,m3| q1x0
    q0x2 -->|m1,m3| q2x0
""",
}


@pytest.mark.parametrize("name", sorted(COMPACTION_COLLISION_MAPS))
def test_gap_compaction_collision_renders(name: str) -> None:
    """A line displaced by gap compaction lands where the map still renders."""
    render_string(f"{_COMPACTION_LINES}\n{COMPACTION_COLLISION_MAPS[name]}")


def test_gap_compaction_keeps_a_side_entry_bundle_in_its_order() -> None:
    """A vertical section's first station holds its side entry's lane order.

    ``q2x1`` carries ``m0`` and ``m2`` with ``m1``'s lane empty between them;
    closing it along the column would swap ``m1`` and ``m2`` at ``q2x0``, whose
    side entry port turns them into the column in the opposite order.
    """
    graph = parse_metro_mermaid(f"{_COMPACTION_LINES}\n{_PERPENDICULAR_ENTRY_SWAP}")
    compute_layout(graph, validate=True)
    offsets = compute_station_offsets(graph)
    (entry_port,) = graph.sections["z2"].entry_ports

    assert graph.ports[entry_port].side in (PortSide.LEFT, PortSide.RIGHT)
    for station_id in (entry_port, "q2x0"):
        assert offsets[station_id, "m1"] < offsets[station_id, "m2"], station_id


CURVE_REPROS = ROOT / "tests" / "fixtures" / "curve_invariant_repros"
SIDE_EXIT_FED_FROM_TWO_STATIONS = (
    CURVE_REPROS / "tb_side_exit_fed_from_two_stations.mmd"
)
TOP_EXIT_GAP_INTO_MERGE_ENTRY = CURVE_REPROS / "bt_top_exit_gap_into_merge_entry.mmd"
BYPASS_INTO_SIDE_EXIT = CURVE_REPROS / "tb_bypass_into_side_exit.mmd"


@pytest.mark.parametrize(
    "path",
    [
        SIDE_EXIT_FED_FROM_TWO_STATIONS,
        TOP_EXIT_GAP_INTO_MERGE_ENTRY,
        BYPASS_INTO_SIDE_EXIT,
    ],
    ids=lambda path: path.stem,
)
def test_vertical_gap_compaction_repro_renders(path: Path) -> None:
    """Closing a vertical section's lane gaps leaves a map that renders."""
    render_string(path.read_text())


def _layout_offsets(path: Path):
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    return graph, compute_station_offsets(graph)


def test_side_exit_pair_is_judged_where_the_column_lines_meet() -> None:
    """A side exit fed from two stations of a column keeps the entry's pair order.

    ``l3`` leaves the column for the side exit at ``a1`` and ``l2`` at ``a2``,
    so the pair first rides together at the exit, whose order ``b``'s entry
    settles.  Closing ``a1``'s dead lane down the column would lift ``l3``
    past ``l2`` at ``a2`` and cross the pair in the exit's turn.
    """
    graph, offsets = _layout_offsets(SIDE_EXIT_FED_FROM_TWO_STATIONS)
    (exit_port,) = graph.sections["a"].exit_ports
    (entry_port,) = graph.sections["b"].entry_ports

    for station_id in ("a2", exit_port, entry_port):
        assert offsets[station_id, "l2"] < offsets[station_id, "l3"], station_id


def test_vertical_exit_gap_hands_its_order_to_a_merge_entry() -> None:
    """A merge entry fed by a vertical section's gapped top exit keeps its order.

    The exit carries ``l1`` and ``l3`` with ``l2``'s lane empty between them,
    and closing that gap down the column would reorder the column against its
    bottom entry.  Left alone, the merge entry ``c`` re-slots ``l3`` above
    ``l1`` as a line descending from above, which crosses the bundle turning in
    from the exit, where ``l3`` rides on ``l1``'s far side.
    """
    graph, offsets = _layout_offsets(TOP_EXIT_GAP_INTO_MERGE_ENTRY)
    (exit_port,) = graph.sections["b"].exit_ports
    (entry_port,) = graph.sections["c"].entry_ports

    for station_id in (exit_port, entry_port):
        assert offsets[station_id, "l1"] < offsets[station_id, "l3"], station_id
