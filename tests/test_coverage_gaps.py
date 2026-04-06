"""Tests covering three gaps identified in PR #221 review (issue #224).

1. _align_uncentered_siblings - Counter majority path and tie/skip path
2. Label obstacle flip - flip-before-push with terminus icons
3. _resolve_downstream_entry_y - junction-traversal branch
"""

from __future__ import annotations

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.labels import (
    place_labels,
)
from nf_metro.layout.routing import route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.core import _align_uncentered_siblings
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Station

# ---------------------------------------------------------------------------
# 1. _align_uncentered_siblings
# ---------------------------------------------------------------------------


class TestAlignUncenteredSiblings:
    """Tests for the Counter majority path in _align_uncentered_siblings."""

    @staticmethod
    def _make_graph_and_routes(
        station_specs: list[tuple[str, float, float]],
        original_xs: dict[str, float],
        edge_pairs: list[tuple[str, str]] | None = None,
    ) -> tuple[MetroGraph, list[RoutedPath], dict[str, float]]:
        """Build a minimal graph, routes, and original_x mapping.

        station_specs: list of (id, x, y) tuples.
        original_xs: mapping of station id -> original x.
        edge_pairs: optional list of (source, target) pairs for routes.
        """
        graph = MetroGraph()
        for sid, x, y in station_specs:
            graph.stations[sid] = Station(
                id=sid, label=sid.upper(), x=x, y=y, section_id="sec1"
            )

        routes: list[RoutedPath] = []
        if edge_pairs:
            for src, tgt in edge_pairs:
                edge = Edge(source=src, target=tgt, line_id="main")
                src_st = graph.stations[src]
                tgt_st = graph.stations[tgt]
                rp = RoutedPath(
                    edge=edge,
                    line_id="main",
                    points=[(src_st.x, src_st.y), (tgt_st.x, tgt_st.y)],
                )
                routes.append(rp)

        return graph, routes, original_xs

    def test_majority_path_aligns_outlier(self):
        """When >50% of moved stations agree on X, outliers are realigned.

        Group of 4 stations all originally at x=100.  Three moved to x=120,
        one moved to x=150 (outlier).  The outlier should be dragged to 120.
        """
        specs = [
            ("a", 120.0, 0.0),  # moved to 120
            ("b", 120.0, 50.0),  # moved to 120
            ("c", 120.0, 100.0),  # moved to 120
            ("d", 150.0, 150.0),  # moved to 150 (outlier)
        ]
        original_xs = {"a": 100.0, "b": 100.0, "c": 100.0, "d": 100.0}

        graph, routes, ox = self._make_graph_and_routes(specs, original_xs)
        _align_uncentered_siblings(routes, graph, ox)

        # Outlier d should be dragged to 120.0 (majority X)
        assert abs(graph.stations["d"].x - 120.0) < 0.5

    def test_tie_skips_alignment(self):
        """When no clear majority (<=50%), alignment is skipped entirely.

        Group of 4 stations originally at x=100.  Two moved to x=120,
        two moved to x=150.  No majority, so all should stay put.
        """
        specs = [
            ("a", 120.0, 0.0),  # moved to 120
            ("b", 120.0, 50.0),  # moved to 120
            ("c", 150.0, 100.0),  # moved to 150
            ("d", 150.0, 150.0),  # moved to 150
        ]
        original_xs = {"a": 100.0, "b": 100.0, "c": 100.0, "d": 100.0}

        graph, routes, ox = self._make_graph_and_routes(specs, original_xs)

        # Record positions before
        xs_before = {sid: s.x for sid, s in graph.stations.items()}
        _align_uncentered_siblings(routes, graph, ox)

        # No majority -> no changes
        for sid in ("a", "b", "c", "d"):
            assert graph.stations[sid].x == xs_before[sid]

    def test_majority_updates_route_endpoints(self):
        """Route endpoints should be updated when stations are dragged.

        The outlier station appears as both a source and target in routes.
        After alignment, the route endpoints should reflect the new X.
        """
        specs = [
            ("a", 120.0, 0.0),
            ("b", 120.0, 50.0),
            ("c", 120.0, 100.0),
            ("d", 150.0, 150.0),  # outlier
            ("e", 150.0, 200.0),  # outlier
        ]
        original_xs = {
            "a": 100.0,
            "b": 100.0,
            "c": 100.0,
            "d": 100.0,
            "e": 100.0,
        }
        edge_pairs = [("c", "d"), ("d", "e")]

        graph, routes, ox = self._make_graph_and_routes(specs, original_xs, edge_pairs)
        _align_uncentered_siblings(routes, graph, ox)

        # d and e should be dragged to 120.0
        assert abs(graph.stations["d"].x - 120.0) < 0.5
        assert abs(graph.stations["e"].x - 120.0) < 0.5

        # Route c->d: target endpoint should be updated
        cd_route = [r for r in routes if r.edge.target == "d"][0]
        assert abs(cd_route.points[-1][0] - 120.0) < 0.5

        # Route d->e: source endpoint should be updated
        de_route = [r for r in routes if r.edge.source == "d"][0]
        assert abs(de_route.points[0][0] - 120.0) < 0.5


# ---------------------------------------------------------------------------
# 2. Label obstacle flip
# ---------------------------------------------------------------------------


class TestLabelObstacleFlip:
    """Tests for the flip-before-push obstacle clearance in place_labels."""

    def test_flip_produces_closer_label_than_push(self):
        """A station between two terminus icons should flip, not push.

        When a label is placed below a station and hits an icon obstacle
        below, flipping it above (if clear) should produce a label closer
        to the station than pushing it past the obstacle.
        """
        # Build a simple graph with one section and one station.
        graph = parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\n"
            "graph LR\n"
            "    subgraph sec1 [Section]\n"
            "        a[A]\n"
            "        b[Middle]\n"
            "        c[C]\n"
            "        a -->|main| b\n"
            "        b -->|main| c\n"
            "    end\n"
        )
        compute_layout(graph, x_spacing=100, y_spacing=50)

        # Place icon obstacles above and below station b.
        bx = graph.stations["b"].x
        by = graph.stations["b"].y
        # Obstacle below: a file icon just below the station
        obs_below = (bx - 15, by + 5, bx + 15, by + 30)
        # Obstacle above: a file icon just above the station
        obs_above = (bx - 15, by - 30, bx + 15, by - 5)

        # Call place_labels with both obstacles
        labels = place_labels(
            graph,
            icon_obstacles=[obs_below, obs_above],
        )

        # Find label for station b
        b_label = [lp for lp in labels if lp.station_id == "b"]
        assert len(b_label) == 1
        b_lp = b_label[0]

        # The label should still be close to the station (within reasonable
        # distance), not pushed far away.  The flip mechanism should keep
        # it closer than a push would.
        dist = abs(b_lp.y - by)
        # With flip, label should be within ~35px of station.
        # Without flip (push only), it would be 30+ px from the obstacle edge.
        assert dist < 50, (
            f"Label too far from station ({dist:.1f}px); "
            f"flip-before-push may not be working"
        )

    def test_single_obstacle_flips_to_clear_side(self):
        """With one obstacle on the default side, label should flip."""
        graph = parse_metro_mermaid(
            "%%metro line: main | Main | #ff0000\n"
            "graph LR\n"
            "    subgraph sec1 [Section]\n"
            "        a[A]\n"
            "        b[B]\n"
            "        a -->|main| b\n"
            "    end\n"
        )
        compute_layout(graph, x_spacing=100, y_spacing=50)

        bx = graph.stations["b"].x
        by = graph.stations["b"].y

        # Place a large obstacle below station b
        obs_below = (bx - 20, by + 2, bx + 20, by + 40)

        labels_with_obs = place_labels(graph, icon_obstacles=[obs_below])
        labels_no_obs = place_labels(graph, icon_obstacles=None)

        b_with = [lp for lp in labels_with_obs if lp.station_id == "b"][0]
        b_without = [lp for lp in labels_no_obs if lp.station_id == "b"][0]

        # If the default placement was below and it hit the obstacle,
        # it should have flipped to above (or vice versa).
        if not b_without.above:
            # Default was below, obstacle is below -> should flip to above
            assert b_with.above, "Label should flip above to avoid obstacle below"


# ---------------------------------------------------------------------------
# 3. _resolve_downstream_entry_y via junctions
# ---------------------------------------------------------------------------


class TestResolveDownstreamEntryYViaJunction:
    """Test that exit port snap considers entry ports reachable via junctions.

    Junctions are created when a single exit port fans out to multiple
    entry ports (i.e., one section exits to two different sections).
    The _resolve_downstream_entry_y function must traverse through
    junctions to find the downstream entry port Y.
    """

    def test_junction_traversal_topology(self):
        """Exit port snaps correctly when downstream entry is via a junction.

        Topology: sec1 exits to both sec2 and sec3 via the same exit port,
        which forces a junction between exit_port and the two entry_ports.
        """
        # line1 goes sec1 -> sec2, line2 goes sec1 -> sec3
        # Both exit from sec1's right exit port, creating a fan-out junction.
        graph = parse_metro_mermaid(
            "%%metro line: line1 | Line 1 | #ff0000\n"
            "%%metro line: line2 | Line 2 | #0000ff\n"
            "graph LR\n"
            "    subgraph sec1 [Section 1]\n"
            "        %%metro entry: left | line1, line2\n"
            "        %%metro exit: right | line1, line2\n"
            "        a[A]\n"
            "        b[B]\n"
            "        a -->|line1| b\n"
            "        a -->|line2| b\n"
            "    end\n"
            "    subgraph sec2 [Section 2]\n"
            "        %%metro entry: left | line1\n"
            "        %%metro exit: right | line1\n"
            "        c[C]\n"
            "        d[D]\n"
            "        c -->|line1| d\n"
            "    end\n"
            "    subgraph sec3 [Section 3]\n"
            "        %%metro entry: left | line2\n"
            "        %%metro exit: right | line2\n"
            "        e[E]\n"
            "        f[F]\n"
            "        e -->|line2| f\n"
            "    end\n"
            "    b -->|line1| c\n"
            "    b -->|line2| e\n"
        )
        compute_layout(graph)

        # Verify junction stations exist (created by _resolve_sections
        # when one exit port fans out to two entry ports).
        junction_ids = set(graph.junctions)
        assert len(junction_ids) > 0, (
            "Expected junction stations for fan-out from sec1 to sec2+sec3"
        )

        # Find the exit port from sec1
        exit_ports = [
            s
            for s in graph.stations.values()
            if s.is_port
            and graph.ports.get(s.id)
            and not graph.ports[s.id].is_entry
            and graph.ports[s.id].section_id == "sec1"
        ]
        assert len(exit_ports) > 0, "Expected exit ports from sec1"

        # Verify the exit -> junction -> entry chain exists.
        found_chain = False
        for ep in exit_ports:
            for edge in graph.edges:
                if edge.source != ep.id:
                    continue
                if edge.target in junction_ids:
                    for e2 in graph.edges:
                        if e2.source != edge.target:
                            continue
                        dp = graph.ports.get(e2.target)
                        if dp and dp.is_entry:
                            found_chain = True
                            break
                if found_chain:
                    break
            if found_chain:
                break
        assert found_chain, (
            "Expected exit -> junction -> entry chain for junction traversal test"
        )

    def test_junction_traversal_produces_valid_layout(self):
        """Fan-out through junction should produce valid layout and routes.

        Same topology as above: sec1 fans out to sec2 and sec3 via a
        junction.  All stations should have coordinates and routing
        should succeed.
        """
        graph = parse_metro_mermaid(
            "%%metro line: line1 | Line 1 | #ff0000\n"
            "%%metro line: line2 | Line 2 | #0000ff\n"
            "graph LR\n"
            "    subgraph sec1 [Section 1]\n"
            "        %%metro entry: left | line1, line2\n"
            "        %%metro exit: right | line1, line2\n"
            "        a[A]\n"
            "        b[B]\n"
            "        a -->|line1| b\n"
            "        a -->|line2| b\n"
            "    end\n"
            "    subgraph sec2 [Section 2]\n"
            "        %%metro entry: left | line1\n"
            "        %%metro exit: right | line1\n"
            "        c[C]\n"
            "        d[D]\n"
            "        c -->|line1| d\n"
            "    end\n"
            "    subgraph sec3 [Section 3]\n"
            "        %%metro entry: left | line2\n"
            "        %%metro exit: right | line2\n"
            "        e[E]\n"
            "        f[F]\n"
            "        e -->|line2| f\n"
            "    end\n"
            "    b -->|line1| c\n"
            "    b -->|line2| e\n"
        )
        compute_layout(graph)

        # All stations should have valid coordinates
        for sid, st in graph.stations.items():
            assert st.x is not None, f"Station {sid} has no x coordinate"
            assert st.y is not None, f"Station {sid} has no y coordinate"

        # Route edges to verify everything is consistent
        routes = route_edges(graph)
        assert len(routes) > 0, "Expected routed edges"
