from pathlib import Path

import networkx as nx
import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout import compute_layout
from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x
from nf_metro.layout.ordering import _place_single_node
from nf_metro.layout.phases import _common as phase_common
from nf_metro.layout.phases._common import (
    continuation_track_is_realizable,
    continuation_track_predecessors,
)
from nf_metro.layout.phases.fan_bundles import _carry_full_bundle_continuations
from nf_metro.layout.phases.guards import (
    PhaseInvariantError,
    _guard_post_convergence_trunk_continues,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

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


def test_downstream_line_reoccupation_rejects_transition_seed() -> None:
    graph = prepare_graph(
        """
%%metro line: first | First | #3779b1
%%metro line: second | Second | #6ef362
graph LR
    subgraph feeder [Feeder]
        source
    end
    subgraph target [Target]
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
def test_vertical_flow_contributes_no_relation_yet_keeps_one_lane(
    direction: str,
) -> None:
    """A vertical section reports no continuation, and needs none.

    Horizontal-only is the relation's contract, so the empty answer here is the
    specified one rather than an accident: a change that started emitting
    vertical relations would red this. The same chain still settles with every
    node on its predecessor's lane column, which is asserted against the settled
    geometry so the empty relation costs no guarantee.
    """
    graph = _transition_graph(direction)
    assert lanes_run_along_x(graph.sections["target"].direction)

    assert continuation_track_predecessors(graph) == {}

    compute_layout(graph)
    lanes = _lane_columns(graph, ("pred", "node", "tail"))

    assert max(lanes) - min(lanes) <= SAME_COORD_TOLERANCE, lanes


def test_tb_passthrough_fixture_keeps_its_column_without_a_relation() -> None:
    """The TB passthrough corpus fixture's merge->tail chain shares one column.

    ``merge`` is ``tail``'s only predecessor and ``tail`` its only target, so the
    chain is a sole continuation by the section's own edges; the horizontal-only
    relation declines to name it, and the column it would have pulled ``tail``
    onto is where the vertical layout puts it anyway.
    """
    path = ROOT / "examples" / "topologies" / "tb_passthrough_continuation.mmd"
    graph = prepare_graph(path.read_text(), source_dir=str(path.parent))
    assert lanes_run_along_x(graph.sections["work"].direction)

    assert continuation_track_predecessors(graph) == {}
    assert [edge.source for edge in graph.edges_to("tail")] == ["merge"]
    assert [edge.target for edge in graph.edges_from("merge")] == ["tail"]

    compute_layout(graph)
    merge_lane, tail_lane = _lane_columns(graph, ("merge", "tail"))

    assert abs(tail_lane - merge_lane) <= SAME_COORD_TOLERANCE
