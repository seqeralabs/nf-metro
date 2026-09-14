"""A layer that both forks and joins must not reserve its own column gap twice.

``_compute_fork_join_gaps`` charges a fork layer's gap into the column after it
and a join layer's gap into the column before it.  A layer that does both has
its gap charged on each side, so where the layer across each of those boundaries
contributes a gap there too, the one-sided reservation - the fork/join station's
own label half-width, kept for an off-track branch's dip - is booked twice for
room the neighbour already opens.

Two shapes must keep the full gap: a layer with no such neighbour, whose own gap
is all the boundary gets, and a layer carrying the interior-branch loop floor,
which is applied to both sides so an interior branch keeps equal divergence and
reconvergence runs.

The same double-book spans two layers when a hub that both forks and joins sits
on one side of the boundary: the fan across it is that hub's single set of
diagonals, reserved once as the hub's join gap before it and once as the fork gap
after it, so the boundary must reserve the larger of the two, not their sum.
Where the fork and join are distinct single-role layers, the two gaps reserve a
divergence and a separate reconvergence and must both stand, or an interior
branch loses the reconvergence run that keeps it centred; a boundary only one
side owns keeps its whole gap.
"""

from __future__ import annotations

import warnings

import pytest

from nf_metro.layout.constants import EXIT_GAP_MULTIPLIER
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.off_track import (
    _compute_fork_join_gaps,
    _detect_fork_join_layers,
    _flow_axis_label_half,
    _fork_join_adjacency,
    _layer_gap_for,
    _loop_widening_for_label_half,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import Edge, MetroGraph, Station

X_SPACING = 70.0
BASE_GAP = X_SPACING * EXIT_GAP_MULTIPLIER
INTERIOR_LABEL = "DESeq2 deltaTE"

# A boundary may settle up to a couple of pixels off its reserved value; below
# this a deficit cannot be expressed, so an over-book is only real above it.
SETTLEMENT_FLOOR = 2.0

FORK_EDGE = "    anota2seq -->|l1,l2| multiqc\n"

TE_MMD = (
    """%%metro title: Fork-join gap
%%metro x_spacing: 70
%%metro line: l1 | Line one | #e6007e
%%metro line: l2 | Line two | #2db572

graph LR
    subgraph te [Translational efficiency]
        te_prep_gene[Gene count\\nmatrix]
        te_prep_orf[ORF count\\nmatrix]
        anota2seq[anota2seq]
        deltate[DESeq2 deltaTE]
        dotseq[DOTSeq]
        te_out[TE results]
    end

    subgraph reporting [Reporting]
        multiqc[MultiQC]
    end

    te_prep_gene -->|l1,l2| anota2seq
    te_prep_gene -->|l1,l2| deltate
    te_prep_orf -->|l1,l2| anota2seq
    te_prep_orf -->|l1,l2| deltate
    te_prep_orf -->|l1,l2| dotseq
    anota2seq -->|l1,l2| te_out
    deltate -->|l1,l2| te_out
    dotseq -->|l1,l2| te_out
"""
    + FORK_EDGE
)


def _two_line_graph(
    stations: dict[str, str], edges: list[tuple[str, str]]
) -> MetroGraph:
    """A two-line graph over ``{id: label}`` stations; an empty label is a port."""
    graph = MetroGraph()
    for sid, label in stations.items():
        graph.add_station(Station(id=sid, label=label, is_port=not label))
    for source, target in edges:
        for line_id in ("l1", "l2"):
            graph.add_edge(Edge(source, target, line_id))
    return graph


def _gap_at(
    graph: MetroGraph, layers: dict[str, int], tracks: dict[str, float], layer: int
) -> tuple[float, bool, bool]:
    """The column gap reserved at ``layer``, and whether it forks and joins."""
    out_targets, in_sources = _fork_join_adjacency(graph, None, None)
    fork_layers, join_layers = _detect_fork_join_layers(
        out_targets, in_sources, layers, tracks
    )
    gap = _layer_gap_for(
        layer,
        fork_layers,
        join_layers,
        out_targets,
        in_sources,
        layers,
        tracks,
        graph,
        None,
        X_SPACING,
        BASE_GAP,
        0.0,
        "LR",
    )
    return gap, layer in fork_layers, layer in join_layers


def _interior_floor() -> float:
    """The two-sided loop floor an interior branch labelled ``INTERIOR_LABEL``
    imposes on the layer that fans to it."""
    return _loop_widening_for_label_half(
        _flow_axis_label_half(INTERIOR_LABEL, "LR"), X_SPACING
    )


def _te_fan() -> tuple[MetroGraph, dict[str, int], dict[str, float]]:
    """A three-branch diamond whose middle layer also feeds an exit port.

    That middle layer both forks (onward station plus port) and joins (two
    sources on different tracks), and its widest label sets the one-sided dip
    reservation.  Its own fan has a single onward target, so no interior-branch
    loop floor applies to it.
    """
    graph = _two_line_graph(
        {
            "gene": "Gene count\nmatrix",
            "orf": "ORF count\nmatrix",
            "anota": "anota2seq",
            "delta": INTERIOR_LABEL,
            "dot": "DOTSeq",
            "out": "TE results",
            "exit": "",
        },
        [
            ("gene", "anota"),
            ("gene", "delta"),
            ("orf", "anota"),
            ("orf", "delta"),
            ("orf", "dot"),
            ("anota", "out"),
            ("delta", "out"),
            ("dot", "out"),
            ("anota", "exit"),
        ],
    )
    layers = {
        "gene": 0,
        "orf": 0,
        "anota": 1,
        "delta": 1,
        "dot": 1,
        "out": 2,
        "exit": 2,
    }
    tracks = {
        "gene": 0.0,
        "orf": 2.0,
        "anota": 0.0,
        "delta": 1.0,
        "dot": 2.0,
        "out": 1.0,
    }
    return graph, layers, tracks


def _interior_branch_fan() -> tuple[MetroGraph, dict[str, int], dict[str, float]]:
    """A hub joining two feeders and forking to three branches.

    The middle branch is interior (a sibling above and one below) with thick
    multi-line sibling bundles, so the interior loop floor applies to the hub's
    layer.
    """
    graph = _two_line_graph(
        {
            "feed_top": "Feed top",
            "feed_bottom": "Feed bottom",
            "hub": "Hub",
            "top": "Top branch",
            "mid": INTERIOR_LABEL,
            "bottom": "Bottom branch",
        },
        [
            ("feed_top", "hub"),
            ("feed_bottom", "hub"),
            ("hub", "top"),
            ("hub", "mid"),
            ("hub", "bottom"),
        ],
    )
    layers = {
        "feed_top": 0,
        "feed_bottom": 0,
        "hub": 1,
        "top": 2,
        "mid": 2,
        "bottom": 2,
    }
    tracks = {
        "feed_top": 0.0,
        "feed_bottom": 2.0,
        "hub": 1.0,
        "top": 0.0,
        "mid": 1.0,
        "bottom": 2.0,
    }
    return graph, layers, tracks


def _solo_fork_join_fan() -> tuple[MetroGraph, dict[str, int], dict[str, float]]:
    """A hub joining two off-track feeders and forking to two branches.

    Neither neighbouring layer forks or joins, so the hub's own gap is the whole
    reservation each of its two boundaries gets.
    """
    graph = _two_line_graph(
        {
            "feed_top": "Feed top",
            "feed_bottom": "Feed bottom",
            "hub": INTERIOR_LABEL,
            "top": "Top branch",
            "bottom": "Bottom branch",
        },
        [
            ("feed_top", "hub"),
            ("feed_bottom", "hub"),
            ("hub", "top"),
            ("hub", "bottom"),
        ],
    )
    layers = {"feed_top": 0, "feed_bottom": 0, "hub": 1, "top": 2, "bottom": 2}
    tracks = {
        "feed_top": 0.0,
        "feed_bottom": 2.0,
        "hub": 1.0,
        "top": 0.0,
        "bottom": 2.0,
    }
    return graph, layers, tracks


def _fork_only_fan() -> tuple[MetroGraph, dict[str, int], dict[str, float]]:
    """A source forking to three branches that never reconverge.

    The boundary after the fork gets no join gap from the next layer, so the
    fork's own gap is the whole reservation the boundary carries and must
    survive: the fork-meets-join term reduces a doubly-booked boundary, it never
    weakens a fork-only one.
    """
    graph = _two_line_graph(
        {
            "src": INTERIOR_LABEL,
            "top": "Top branch",
            "mid": "Mid branch",
            "bottom": "Bottom branch",
        },
        [
            ("src", "top"),
            ("src", "mid"),
            ("src", "bottom"),
        ],
    )
    layers = {"src": 0, "top": 1, "mid": 1, "bottom": 1}
    tracks = {"src": 1.0, "top": 0.0, "mid": 1.0, "bottom": 2.0}
    return graph, layers, tracks


def test_fork_layer_feeding_a_join_layer_books_one_boundary_gap() -> None:
    """A boundary between a fork layer and the join layer it feeds carries one
    set of diagonals, so it reserves one gap - the larger of the fork and join
    gaps - not their sum.  Charging both double-books room a single divergence
    and reconvergence share, leaving dead flat track before the join hub."""
    graph, layers, tracks = _te_fan()
    out_targets, in_sources = _fork_join_adjacency(graph, None, None)
    fork_layers, join_layers = _detect_fork_join_layers(
        out_targets, in_sources, layers, tracks
    )
    layer_extra = _compute_fork_join_gaps(graph, layers, tracks, X_SPACING)

    boundaries = [
        layer
        for layer in range(max(layers.values()))
        if layer in fork_layers and (layer + 1) in join_layers
    ]
    assert boundaries, "fixture no longer exercises a fork-meets-join boundary"
    for layer in boundaries:
        fork_gap, forks, _ = _gap_at(graph, layers, tracks, layer)
        join_gap, _, joins = _gap_at(graph, layers, tracks, layer + 1)
        assert forks and joins
        booked = layer_extra[layer + 1] - layer_extra[layer]
        assert booked == pytest.approx(max(fork_gap, join_gap), abs=SETTLEMENT_FLOOR), (
            f"boundary {layer}->{layer + 1} books {booked}px, not the "
            f"{max(fork_gap, join_gap)}px the wider of its fork ({fork_gap}px) "
            f"and join ({join_gap}px) gaps needs"
        )


def _disjoint_fork_join_fan() -> tuple[MetroGraph, dict[str, int], dict[str, float]]:
    """Adjacent fork and join layers that are each a single role.

    One source forks to two branches while a second source joins the first
    branch, so layer 0 only forks and layer 1 only joins - neither is a hub that
    both forks and joins.  The two gaps then reserve two separate diagonal sets,
    a divergence and a reconvergence, so the boundary must keep both: collapsing
    it would steal the reconvergence run an interior branch needs to stay
    centred.
    """
    graph = _two_line_graph(
        {
            "src_top": "Source top",
            "src_bottom": "Source bottom",
            "branch_a": INTERIOR_LABEL,
            "branch_b": "Branch B",
            "sink": "Sink",
        },
        [
            ("src_top", "branch_a"),
            ("src_top", "branch_b"),
            ("src_bottom", "branch_a"),
            ("branch_a", "sink"),
            ("branch_b", "sink"),
        ],
    )
    layers = {"src_top": 0, "src_bottom": 0, "branch_a": 1, "branch_b": 1, "sink": 2}
    tracks = {
        "src_top": 0.0,
        "src_bottom": 2.0,
        "branch_a": 0.0,
        "branch_b": 2.0,
        "sink": 1.0,
    }
    return graph, layers, tracks


def test_disjoint_fork_and_join_boundary_keeps_both_gaps() -> None:
    """A fork layer feeding a distinct join layer, with no hub that both forks
    and joins across the boundary, keeps both gaps: the fork spread and the join
    gather are separate diagonal sets, not one double-booked fan."""
    graph, layers, tracks = _disjoint_fork_join_fan()
    out_targets, in_sources = _fork_join_adjacency(graph, None, None)
    fork_layers, join_layers = _detect_fork_join_layers(
        out_targets, in_sources, layers, tracks
    )
    layer_extra = _compute_fork_join_gaps(graph, layers, tracks, X_SPACING)

    assert 0 in fork_layers and 1 in join_layers, (
        "fixture no longer exercises a fork feeding a join"
    )
    assert not (0 in join_layers or 1 in fork_layers), (
        "fixture no longer isolates single-role fork and join layers"
    )
    fork_gap = _gap_at(graph, layers, tracks, 0)[0]
    join_gap = _gap_at(graph, layers, tracks, 1)[0]
    assert fork_gap != pytest.approx(join_gap), (
        "fixture no longer distinguishes collapsing from keeping both gaps"
    )
    booked = layer_extra[1] - layer_extra[0]
    assert booked == pytest.approx(fork_gap + join_gap), (
        f"disjoint fork/join boundary books {booked}px, not both its "
        f"{fork_gap}px fork and {join_gap}px join gaps"
    )


def test_fork_only_boundary_keeps_its_whole_gap() -> None:
    """A fork with no join across its boundary keeps its full gap: the
    fork-meets-join reduction may not weaken a boundary a single side owns."""
    graph, layers, tracks = _fork_only_fan()
    out_targets, in_sources = _fork_join_adjacency(graph, None, None)
    fork_layers, join_layers = _detect_fork_join_layers(
        out_targets, in_sources, layers, tracks
    )
    layer_extra = _compute_fork_join_gaps(graph, layers, tracks, X_SPACING)

    assert 0 in fork_layers and 1 not in join_layers, (
        "fixture no longer exercises a fork-only boundary"
    )
    fork_gap = _gap_at(graph, layers, tracks, 0)[0]
    booked = layer_extra[1] - layer_extra[0]
    assert booked == pytest.approx(fork_gap), (
        f"fork-only boundary books {booked}px, not its whole {fork_gap}px gap"
    )


def _te_span(source: str) -> float:
    """Flow-axis span of the ``te`` section laid out from ``source``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        graph = parse_metro_mermaid(source)
        compute_layout(graph, validate=True)
    return graph.stations["te_out"].x - graph.stations["te_prep_gene"].x


def test_fork_and_join_layer_reserves_only_the_base_gap() -> None:
    """A layer charged on both sides, with no interior floor of its own, drops
    the one-sided dip reservation and keeps the base gap."""
    gap, forks, joins = _gap_at(*_te_fan(), layer=1)

    assert forks and joins, "fixture no longer exercises a fork-and-join layer"
    assert gap == pytest.approx(BASE_GAP), (
        f"fork-and-join layer reserves {gap}px on each side of its column, "
        f"not the base gap {BASE_GAP}px"
    )


def test_interior_branch_floor_survives_on_a_fork_and_join_layer() -> None:
    """A layer carrying an interior-branch loop floor keeps it on both sides, so
    the interior branch's divergence and reconvergence runs stay equal."""
    gap, forks, joins = _gap_at(*_interior_branch_fan(), layer=1)

    assert forks and joins, "fixture no longer exercises a fork-and-join layer"
    assert _interior_floor() > BASE_GAP, (
        "fixture no longer exercises the interior-branch floor"
    )
    assert gap == pytest.approx(_interior_floor()), (
        f"fork-and-join layer reserves {gap}px, not its interior-branch floor "
        f"{_interior_floor()}px"
    )


def test_solo_fork_and_join_layer_keeps_its_dip_reservation() -> None:
    """A fork-and-join layer with no fork or join on either neighbouring layer
    keeps the dip reservation: it is the only room its boundaries get."""
    gap, forks, joins = _gap_at(*_solo_fork_join_fan(), layer=1)

    dip = _flow_axis_label_half(INTERIOR_LABEL, "LR")
    assert forks and joins, "fixture no longer exercises a fork-and-join layer"
    assert dip > BASE_GAP, "fixture no longer exercises the dip reservation"
    assert gap == pytest.approx(dip), (
        f"solo fork-and-join layer reserves {gap}px, not its {dip}px dip"
    )


def test_extra_fork_off_a_join_layer_adds_only_the_base_gap() -> None:
    """End to end: giving the join layer a second, diverging target widens the
    section by the base gap, not by the layer's whole label reservation over
    again."""
    joins_only = _te_span(TE_MMD.replace(FORK_EDGE, ""))
    forks_and_joins = _te_span(TE_MMD)

    assert forks_and_joins <= joins_only + BASE_GAP, (
        f"the extra fork widens the section from {joins_only}px to "
        f"{forks_and_joins}px, over the {BASE_GAP}px base gap it may claim"
    )
