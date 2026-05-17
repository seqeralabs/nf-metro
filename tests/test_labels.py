"""Tests for label placement helpers."""

from dataclasses import dataclass, field

from nf_metro.layout.geometry import segment_intersects_bbox as _segment_intersects_bbox
from nf_metro.layout.labels import (
    LabelPlacement,
    _avoid_diagonal_routes,
    _compute_port_label_preference,
)
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Station


def _make_graph(stations, edges, ports):
    """Build a minimal MetroGraph for label tests."""
    g = MetroGraph()
    for s in stations:
        g.stations[s.id] = s
    g.edges = list(edges)
    for p in ports:
        g.ports[p.id] = p
    return g


class TestComputePortLabelPreference:
    """Tests for _compute_port_label_preference."""

    def test_exit_port_below_prefers_label_above(self):
        """Station with exit port below should prefer label above."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=100),
                Station(id="p", label="", x=120, y=200, is_port=True),
            ],
            edges=[Edge(source="a", target="p", line_id="L1")],
            ports=[Port(id="p", section_id="s", side=PortSide.BOTTOM, is_entry=False)],
        )
        pref = _compute_port_label_preference(g)
        assert pref["a"] is True  # above

    def test_exit_port_above_prefers_label_below(self):
        """Station with exit port above should prefer label below."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=200),
                Station(id="p", label="", x=120, y=100, is_port=True),
            ],
            edges=[Edge(source="a", target="p", line_id="L1")],
            ports=[Port(id="p", section_id="s", side=PortSide.TOP, is_entry=False)],
        )
        pref = _compute_port_label_preference(g)
        assert pref["a"] is False  # below

    def test_entry_port_ignored(self):
        """Entry ports should not produce a label preference."""
        g = _make_graph(
            stations=[
                Station(id="p", label="", x=50, y=200, is_port=True),
                Station(id="a", label="A", x=100, y=100),
            ],
            edges=[Edge(source="p", target="a", line_id="L1")],
            ports=[Port(id="p", section_id="s", side=PortSide.LEFT, is_entry=True)],
        )
        pref = _compute_port_label_preference(g)
        assert "a" not in pref

    def test_same_y_ignored(self):
        """Ports at the same Y as the station should not produce a preference."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=100),
                Station(id="p", label="", x=200, y=100, is_port=True),
            ],
            edges=[Edge(source="a", target="p", line_id="L1")],
            ports=[Port(id="p", section_id="s", side=PortSide.RIGHT, is_entry=False)],
        )
        pref = _compute_port_label_preference(g)
        assert "a" not in pref

    def test_max_dx_filters_distant_ports(self):
        """Ports beyond max_dx should not override label side."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=100),
                Station(id="p", label="", x=300, y=200, is_port=True),
            ],
            edges=[Edge(source="a", target="p", line_id="L1")],
            ports=[Port(id="p", section_id="s", side=PortSide.BOTTOM, is_entry=False)],
        )
        # dx=200 exceeds max_dx=120
        pref = _compute_port_label_preference(g, max_dx=120)
        assert "a" not in pref

        # Without limit, preference is present
        pref_no_limit = _compute_port_label_preference(g, max_dx=0)
        assert pref_no_limit["a"] is True

    def test_conflicting_ports_cancel(self):
        """Ports on both sides should cancel the preference."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=150),
                Station(id="p1", label="", x=120, y=100, is_port=True),
                Station(id="p2", label="", x=120, y=200, is_port=True),
            ],
            edges=[
                Edge(source="a", target="p1", line_id="L1"),
                Edge(source="a", target="p2", line_id="L2"),
            ],
            ports=[
                Port(id="p1", section_id="s", side=PortSide.TOP, is_entry=False),
                Port(id="p2", section_id="s", side=PortSide.BOTTOM, is_entry=False),
            ],
        )
        pref = _compute_port_label_preference(g)
        assert "a" not in pref

    def test_multiple_consistent_ports_keep_preference(self):
        """Multiple exit ports on the same side should reinforce the preference."""
        g = _make_graph(
            stations=[
                Station(id="a", label="A", x=100, y=100),
                Station(id="p1", label="", x=110, y=200, is_port=True),
                Station(id="p2", label="", x=120, y=250, is_port=True),
            ],
            edges=[
                Edge(source="a", target="p1", line_id="L1"),
                Edge(source="a", target="p2", line_id="L2"),
            ],
            ports=[
                Port(id="p1", section_id="s", side=PortSide.BOTTOM, is_entry=False),
                Port(id="p2", section_id="s", side=PortSide.BOTTOM, is_entry=False),
            ],
        )
        pref = _compute_port_label_preference(g)
        assert pref["a"] is True  # both below -> prefer above


@dataclass
class _FakeEdge:
    source: str = ""
    target: str = ""


@dataclass
class _FakeRoute:
    edge: _FakeEdge = field(default_factory=_FakeEdge)
    line_id: str = "L1"
    points: list = field(default_factory=list)
    offsets_applied: bool = True


class TestSegmentIntersectsBbox:
    """Tests for _segment_intersects_bbox."""

    def test_segment_inside_bbox(self):
        assert _segment_intersects_bbox(5, 5, 10, 10, (0, 0, 20, 20))

    def test_segment_crosses_bbox(self):
        assert _segment_intersects_bbox(-5, 10, 25, 10, (0, 0, 20, 20))

    def test_segment_outside_bbox(self):
        assert not _segment_intersects_bbox(100, 100, 200, 200, (0, 0, 20, 20))

    def test_diagonal_clips_corner(self):
        assert _segment_intersects_bbox(0, 30, 30, 0, (10, 10, 20, 20))

    def test_diagonal_misses_bbox(self):
        # Diagonal passes well clear of the bbox.
        assert not _segment_intersects_bbox(0, 0, 5, 5, (50, 50, 60, 60))


class TestAvoidDiagonalRoutes:
    """Tests for _avoid_diagonal_routes."""

    def test_label_flipped_off_diagonal(self):
        g = MetroGraph()
        g.stations["a"] = Station(id="a", label="A", x=100, y=200)
        # Label placed above the station (y_max = 195) right where a
        # diagonal route segment crosses.
        placement = LabelPlacement(station_id="a", text="A", x=100, y=195, above=True)
        # Diagonal segment passes through the label area above.
        route = _FakeRoute(points=[(50, 250), (150, 150)])
        _avoid_diagonal_routes([placement], g, [route], None)
        # Should have flipped to below.
        assert placement.above is False
        assert placement.y > 200

    def test_horizontal_segment_ignored(self):
        g = MetroGraph()
        g.stations["a"] = Station(id="a", label="A", x=100, y=200)
        placement = LabelPlacement(station_id="a", text="A", x=100, y=195, above=True)
        # Pure horizontal segment crossing the label area.
        route = _FakeRoute(points=[(0, 195), (200, 195)])
        _avoid_diagonal_routes([placement], g, [route], None)
        # Should not flip - horizontal trunk routes aren't treated as
        # label obstacles.
        assert placement.above is True
        assert placement.y == 195

    def test_no_route_collision_no_flip(self):
        g = MetroGraph()
        g.stations["a"] = Station(id="a", label="A", x=100, y=200)
        placement = LabelPlacement(station_id="a", text="A", x=100, y=195, above=True)
        # Diagonal far away from the label.
        route = _FakeRoute(points=[(500, 500), (600, 600)])
        _avoid_diagonal_routes([placement], g, [route], None)
        assert placement.above is True
        assert placement.y == 195
