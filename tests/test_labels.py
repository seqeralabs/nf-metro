"""Tests for label placement helpers."""

import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nf_metro.layout.constants import (
    DESCENDER_CLEARANCE,
    LABEL_OFFSET,
    LABEL_OVERLAP_TOL,
)
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.geometry import segment_intersects_bbox as _segment_intersects_bbox
from nf_metro.layout.labels import (
    LabelPlacement,
    _avoid_diagonal_routes,
    _build_label_ctx,
    _compute_port_label_preference,
    _find_clear_reflow_candidate,
    _intrusion,
    _label_bbox,
    _lift_wrapped_labels_off_foreign_trunks,
    _make_obstacle_placements,
    _places_label_beside_pill,
    _station_marker_boxes,
    _wrap_text_to_chars,
    find_wrapped_label_trunk_strikes,
    label_glyph_ink_bbox,
    place_labels,
)
from nf_metro.layout.phases.spacing import (
    _reflowed_label_station_ids,
    _residual_label_overlaps,
)
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.layout.routing.common import OffsetRegime
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import (
    Edge,
    LayoutGeometryWarning,
    MetroGraph,
    Port,
    PortSide,
    Station,
)
from nf_metro.render.svg import _compute_icon_obstacles, build_render_plan
from nf_metro.themes import THEMES, resolve_theme

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    offset_regime: OffsetRegime = OffsetRegime.BAKED


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


class TestWrappedLabelTrunkLift:
    """The wrapped-label lift pulls a two-line name off a foreign trunk.

    A wrapped label pushed out toward a neighbouring track can grow its ink
    block across a metro line the station does not serve.
    :func:`_lift_wrapped_labels_off_foreign_trunks` restores it to its
    un-pushed anchor beside its own pill, clearing the line off the name. This
    exercises both halves of that guarantee on constructed geometry,
    independent of any parsed fixture's settled coordinates.
    """

    def _struck_wrapped_setup(self):
        """A two-line 'sort' label pushed above its pill onto a foreign 'qc'
        trunk, plus the strike's trunk Y taken from the pushed ink block."""
        graph = MetroGraph()
        graph.add_station(Station(id="sort", label="Samtools sort", x=100, y=200))
        graph.add_station(Station(id="star", label="STAR", x=40, y=200))
        graph.add_edge(Edge(source="star", target="sort", line_id="align"))

        base_y = 200 - LABEL_OFFSET - DESCENDER_CLEARANCE
        placement = LabelPlacement(
            station_id="sort", text="Samtools\nsort", x=100, y=base_y - 40, above=True
        )
        x0, ytop, x1, ybot = label_glyph_ink_bbox(placement)
        trunk_y = (ytop + ybot) / 2
        route = _FakeRoute(
            line_id="qc", points=[(x0 - 20, trunk_y), (x1 + 20, trunk_y)]
        )
        return graph, placement, route, base_y

    def test_pushed_wrapped_label_strikes_foreign_trunk(self):
        graph, placement, route, _ = self._struck_wrapped_setup()
        strikes = find_wrapped_label_trunk_strikes(graph, [placement], [route], None)
        assert [sid for sid, _y, _lid in strikes] == ["sort"]
        assert strikes[0][2] == "qc"

    def test_lift_clears_the_foreign_trunk_strike(self):
        graph, placement, route, base_y = self._struck_wrapped_setup()
        _lift_wrapped_labels_off_foreign_trunks(
            [placement], graph, [route], None, {}, LABEL_OFFSET
        )
        assert placement.y == base_y
        assert not find_wrapped_label_trunk_strikes(graph, [placement], [route], None)


# A wide-label section beside a narrow one, so the narrow section's labels are
# the ones forced to wrap (#1768).
_CROWDED_LABELS_MMD = """%%metro line: main | Main | #e6007e
%%metro grid: wide, narrow | 0,0

graph LR
    subgraph wide [Wide neighbour]
        w1[BEDTools genomecov]
        w2[UMI-tools deduplicate]
        w3[Infer strandedness]

        w1 -->|main| w2
        w2 -->|main| w3
    end

    subgraph narrow [Narrow]
        a[plastid P-site]
        b[plastid wiggle]
        c[Quantify ORF P-sites]

        a -->|main| b
        b -->|main| c
    end

    w3 -->|main| a
"""


def _drawn_label_texts(graph: MetroGraph) -> dict[str, str]:
    """Station id -> the name-label text the renderer will draw, wraps included."""
    plan = build_render_plan(graph, THEMES["nfcore"])
    return {p.station_id: p.text for p in plan.labels if p.station_id}


def _assert_tokens_intact(graph: MetroGraph, context: str) -> None:
    """Every drawn label must be its node label with only whitespace re-flowed."""
    mangled = {
        sid: text
        for sid, text in _drawn_label_texts(graph).items()
        if (station := graph.stations.get(sid)) is not None
        and text.split() != station.label.split()
    }
    assert not mangled, (
        f"{context}: wrapping mutated label tokens (mid-word hyphenation): "
        + ", ".join(f"{sid}={text!r}" for sid, text in sorted(mangled.items()))
    )


class TestWrapTextToChars:
    """Wrapping re-flows whitespace; it never breaks inside a token."""

    @pytest.mark.parametrize(
        "label",
        [
            "plastid wiggle",
            "gffcompare",
            "RNA-SeQC",
            "sambamba markdup",
            "Quantify ORF P-sites",
            "UMI-tools deduplicate",
            "Infer strandedness",
        ],
    )
    @pytest.mark.parametrize("budget", [1, 2, 4, 6, 8, 12, 40])
    def test_tokens_survive_every_budget(self, label: str, budget: int) -> None:
        assert _wrap_text_to_chars(label, budget).split() == label.split()

    def test_breaks_on_whitespace_before_widening(self) -> None:
        assert _wrap_text_to_chars("plastid wiggle", 7) == "plastid\nwiggle"

    def test_single_token_overflows_rather_than_splitting(self) -> None:
        assert _wrap_text_to_chars("gffcompare", 4) == "gffcompare"

    def test_oversized_token_does_not_strand_its_neighbours(self) -> None:
        assert (
            _wrap_text_to_chars("Quantify ORF P-sites", 4) == "Quantify\nORF\nP-sites"
        )


class TestDrawnLabelsKeepTokensWhole:
    """No station label the renderer draws is hyphenated mid-word (#1768)."""

    @pytest.mark.parametrize("x_spacing", [None, 40.0, 60.0, 70.0])
    def test_crowded_labels_never_hyphenate(self, x_spacing: float | None) -> None:
        graph = parse_metro_mermaid(_CROWDED_LABELS_MMD)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compute_layout(graph, x_spacing=x_spacing)
        _assert_tokens_intact(graph, f"x_spacing={x_spacing}")

    @pytest.mark.parametrize(
        "fixture",
        [
            "examples/topologies/render_labelwrap_row_gap.mmd",
            "examples/topologies/paired_input_fan_branch_tree.mmd",
            "examples/topologies/packed_cell_right_exit_left_entry_wrap.mmd",
            "examples/variantbenchmarking.mmd",
            "tests/fixtures/multiline_labels.mmd",
        ],
    )
    def test_wrapping_corpus_fixture_never_hyphenates(self, fixture: str) -> None:
        graph = parse_metro_mermaid((REPO_ROOT / fixture).read_text())
        compute_layout(graph)
        _assert_tokens_intact(graph, fixture)


class TestPinnedXSpacingWarning:
    """A pinned column pitch too narrow for its labels says so out loud.

    Wrapping stops at the longest word, so a pitch the caller pinned below what
    the content needs ships an honest overlap.  That is preferable to a name
    broken mid-word, but only if it is reported rather than shipped silently.
    """

    def test_pinned_pitch_below_content_warns(self) -> None:
        graph = parse_metro_mermaid(_CROWDED_LABELS_MMD)
        with pytest.warns(
            LayoutGeometryWarning, match=r"x_spacing=60\.0 is too narrow"
        ):
            compute_layout(graph, x_spacing=60.0)

    @pytest.mark.parametrize("x_spacing", [None, 100.0])
    def test_a_pitch_the_labels_fit_is_silent(self, x_spacing: float | None) -> None:
        graph = parse_metro_mermaid(_CROWDED_LABELS_MMD)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compute_layout(graph, x_spacing=x_spacing)
        assert not [w for w in caught if "x_spacing" in str(w.message)]


def _laid_out(fixture: str, x_spacing: float | None = None) -> MetroGraph:
    """Parse and lay out a corpus fixture, optionally pinning the column pitch."""
    graph = parse_metro_mermaid((REPO_ROOT / fixture).read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph, x_spacing=x_spacing)
    return graph


def _reflowed_labels_beside_a_free_side(graph: MetroGraph) -> list[str]:
    """Re-flowed labels that overlap although a side of their own pill is free.

    Re-flowing a label onto several lines changes its footprint, so the side
    picked for the flat single line has to be re-offered to the block.  Asks
    :func:`_find_clear_reflow_candidate` -- the production decision itself, so
    the two cannot drift -- of the labels the layout shipped: a station
    reported here ships an overlap with a clear side going spare, which means
    the engine pays for the collision by widening spacing instead.
    """
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    obstacles = _compute_icon_obstacles(graph, resolve_theme(None, graph), offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        routes=routes,
        icon_obstacles=obstacles,
        label_angle=graph.label_angle or 0.0,
    )
    ctx, _ = _build_label_ctx(
        graph, LABEL_OFFSET, offsets, obstacles, graph.label_angle or 0.0
    )
    markers = _station_marker_boxes(graph, offsets)
    obstacle_placements = _make_obstacle_placements(obstacles)

    stranded: list[str] = []
    for placement in placements:
        station = graph.stations.get(placement.station_id)
        if station is None or placement.text == station.label:
            continue
        others = [p for p in placements if p is not placement] + obstacle_placements
        if _find_clear_reflow_candidate(ctx, placement, others, markers) is not None:
            stranded.append(placement.station_id)
    return stranded


class TestReflowedLabelsAreRePlaced:
    """A re-flowed label is re-sided for the block it becomes (#1768)."""

    @pytest.mark.parametrize(
        ("fixture", "x_spacing"),
        [
            ("examples/centered_tracks.mmd", 50.0),
            ("examples/live/pipeline.mmd", 50.0),
            ("examples/topologies/fold_stacked_branch.mmd", 50.0),
            ("examples/topologies/reconverge_reversed_fold.mmd", 50.0),
            ("examples/topologies/render_labelwrap_row_gap.mmd", None),
            ("examples/topologies/render_labelwrap_row_gap.mmd", 50.0),
            ("examples/topologies/render_labelwrap_row_gap.mmd", 60.0),
            ("examples/topologies/straddling_fanout_junction.mmd", None),
            ("examples/topologies/straddling_fanout_junction.mmd", 50.0),
            ("examples/topologies/straddling_fanout_junction.mmd", 60.0),
            ("examples/topologies/wrapped_label_trunk.mmd", 50.0),
        ],
    )
    def test_no_reflowed_label_strands_beside_a_free_side(
        self, fixture: str, x_spacing: float | None
    ) -> None:
        graph = _laid_out(fixture, x_spacing)
        stranded = _reflowed_labels_beside_a_free_side(graph)
        assert not stranded, (
            f"{fixture} @ x_spacing={x_spacing}: re-flowed label(s) "
            f"{sorted(stranded)} overlap although a side of their own pill is free"
        )


class TestReflowedBesidePillLabelsKeepTheirPill:
    """A vertical-flow label re-flowed off a marker stays beside its pill (#1768).

    A label in a vertical-flow section hangs off a pill edge, anchored from
    that edge (``text_anchor`` start/end) and centred on the station's own Y.
    Narrowing one to relieve a marker intrusion is worthwhile, so such a label
    does reach the re-flow re-siding pass, which offers only the anchors
    centred above and below the station -- straddling the trunk, and measured
    for a centred anchor the placement does not use.  Adopting one would draw
    the name across its own pill, so the pass has to leave these alone.
    """

    @pytest.mark.parametrize(
        ("fixture", "x_spacing"),
        [
            ("examples/topologies/fold_left_exit_right_entry.mmd", None),
            ("examples/topologies/fold_left_exit_right_entry.mmd", 40.0),
            ("examples/topologies/tb_fork_lane_transpose.mmd", None),
        ],
    )
    def test_reflowed_vertical_flow_label_hangs_off_its_pill_edge(
        self, fixture: str, x_spacing: float | None
    ) -> None:
        graph = _laid_out(fixture, x_spacing)
        plan = build_render_plan(graph, THEMES["nfcore"])
        markers = _station_marker_boxes(graph, plan.station_offsets)
        checked: list[str] = []
        for placement in plan.labels:
            station = graph.stations.get(placement.station_id)
            if station is None or not _places_label_beside_pill(graph, station):
                continue
            if placement.text == station.label:
                continue
            checked.append(placement.station_id)
            assert placement.y == pytest.approx(station.y), (
                f"{placement.station_id}: re-flowed block left its pill's baseline"
            )
            assert placement.dominant_baseline == "central"
            assert placement.text_anchor in ("start", "end")
            own = markers.get(placement.station_id)
            assert own is not None
            ox, oy = _intrusion(_label_bbox(placement), own)
            assert not (ox > LABEL_OVERLAP_TOL and oy > LABEL_OVERLAP_TOL), (
                f"{placement.station_id}: re-flowed block runs across its own "
                f"pill by {ox:.1f}x{oy:.1f}px"
            )
        assert checked, (
            f"{fixture} @ x_spacing={x_spacing} no longer re-flows a "
            "vertical-flow label; the case this locks is unexercised"
        )


class TestReflowSparesTheColumnWidening:
    """Wrapping that resolves its own collision costs no extra pitch (#1768).

    ``markdup``'s name is the widest label in a crowded QC section.  It fits on
    two lines beside its neighbours, so the layout needs neither a mid-word
    break nor a wider column pitch to place it.
    """

    FIXTURE = "examples/topologies/render_labelwrap_row_gap.mmd"
    BASE_PITCH = 60.0

    def test_wide_label_reflows_onto_two_whole_words(self) -> None:
        graph = _laid_out(self.FIXTURE)
        assert _drawn_label_texts(graph)["markdup"] == "sambamba\nmarkdup"

    def test_auto_pitch_does_not_widen_past_the_base(self) -> None:
        auto = _laid_out(self.FIXTURE)
        pinned = _laid_out(self.FIXTURE, self.BASE_PITCH)
        assert {sid: (s.x, s.y) for sid, s in auto.stations.items()} == {
            sid: (s.x, s.y) for sid, s in pinned.stations.items()
        }

    def test_base_pitch_needs_no_geometry_warning(self) -> None:
        graph = parse_metro_mermaid((REPO_ROOT / self.FIXTURE).read_text())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compute_layout(graph, x_spacing=self.BASE_PITCH, validate=True)
        assert not [w for w in caught if w.category is LayoutGeometryWarning]


def _laid_out_at_row_pitch(fixture: str, y_spacing: float) -> MetroGraph:
    """Parse and lay out a corpus fixture on a pinned row pitch."""
    graph = parse_metro_mermaid((REPO_ROOT / fixture).read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compute_layout(graph, y_spacing=y_spacing)
    return graph


class TestReflowEarnsItsRowPitch:
    """No layout both widens for label crowding and ships a cheap re-flow (#1768).

    Wrapping is offered before the spacing search widens anything, so a graph
    can end up paying on both sides of the same trade: a pitch above the base
    content pitch *and* a label broken onto two lines that a touch more of that
    pitch would have kept on one.  The invariant binds exactly where the search
    widened: one ``LABEL_OVERLAP_TOL`` of extra row pitch -- the intrusion below
    which the engine does not count label geometry as conflicting -- must not
    buy a smaller re-flowed set with no residual overlap left over.

    A wrap that clears everything at the base content pitch is outside the
    invariant and stays: it costs no pitch, so there is nothing to buy back and
    widening past the content minimum would only spend room nothing asked for.
    """

    WRAPPING_FIXTURES = (
        "tests/data/reflow_widened_pitch.mmd",
        "examples/live/pipeline.mmd",
        "examples/topologies/fanin_join_diff_length_branches.mmd",
        "examples/topologies/fold_bypass_creep.mmd",
        "examples/topologies/fold_left_exit_right_entry.mmd",
        "examples/topologies/foldback_exit_peeloff.mmd",
        "examples/topologies/manual_rl_row_nonconsumer_bypass.mmd",
        "examples/topologies/near_edge_exit_corner.mmd",
        "examples/topologies/packed_cell_cellmate_bypass.mmd",
        "examples/topologies/render_labelwrap_row_gap.mmd",
        "examples/topologies/same_destination_vertical_convergence.mmd",
        "examples/topologies/straddling_fanout_junction.mmd",
        "examples/topologies/tb_fork_lane_transpose.mmd",
        "examples/topologies/wide_label_fan.mmd",
        "examples/topologies/wrapped_label_trunk.mmd",
        "tests/fixtures/regressions/cross_column_perp_entry_overflow.mmd",
        "tests/fixtures/through_section/riboseq_packed_lr.mmd",
    )

    @pytest.mark.parametrize("fixture", WRAPPING_FIXTURES)
    def test_a_tolerance_of_row_pitch_would_not_have_spared_the_reflow(
        self, fixture: str
    ) -> None:
        settled = _laid_out(fixture)
        reflowed = _reflowed_label_station_ids(settled)
        if not reflowed:
            return
        pitch, base = settled._resolved_y_spacing, settled._base_y_spacing
        assert pitch is not None and base is not None, (
            "auto layout did not record its row pitch"
        )
        if pitch <= base:
            return  # the wrap bought its clearance for free

        wider = _laid_out_at_row_pitch(fixture, pitch + LABEL_OVERLAP_TOL)
        spared = reflowed - _reflowed_label_station_ids(wider)
        assert not spared or _residual_label_overlaps(wider), (
            f"{fixture}: {sorted(spared)} ship re-flowed at row pitch "
            f"{pitch:.1f}, already widened from a base of {base:.1f}, yet "
            f"{pitch + LABEL_OVERLAP_TOL:.1f} keeps them on one line with no "
            f"overlap left over"
        )

    def test_the_locked_case_is_a_widened_pitch(self) -> None:
        """A fixture whose label crowding actually widens the row pitch.

        Anchors the parametrised invariant on a case that reaches the widened
        branch: a thick multi-line symmetric diamond whose interior branch
        labels crowd, so the spacing search widens the row pitch to clear them.
        The widen buys back every wrap (no label ships re-flowed), which is the
        outcome the invariant permits.
        """
        graph = _laid_out("tests/data/reflow_widened_pitch.mmd")
        assert graph._resolved_y_spacing > graph._base_y_spacing

    def test_short_two_word_names_stay_on_one_line(self) -> None:
        drawn = _drawn_label_texts(_laid_out("examples/centered_tracks.mmd"))
        assert [drawn[sid] for sid in ("cnv", "splice", "fusion")] == [
            "CNV",
            "Splice call",
            "Fusion call",
        ]

    def test_a_reflow_that_spares_a_wider_pitch_is_kept(self) -> None:
        fixture = "examples/topologies/render_labelwrap_row_gap.mmd"
        assert _drawn_label_texts(_laid_out(fixture))["markdup"] == (
            "sambamba\nmarkdup"
        )
