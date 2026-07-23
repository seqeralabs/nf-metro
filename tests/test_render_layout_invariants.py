"""The render path runs the Tier-A layout guards on the settled geometry (#923).

``render_svg`` computes ``station_offsets`` and ``routes`` and then runs
:func:`assert_render_layout_invariants` on the final geometry, mirroring the
always-on :func:`assert_render_curve_invariants`.  A Tier-A violation is a
warning by default (the map renders best-effort with a visible diagnosis) and a
raise under ``graph.strict`` / ``--strict``.

The chokepoint is observational: the guards only read the geometry the renderer
is about to draw, so it cannot move a pixel.  These tests pin that it fires on
an injected violation, that the clean corpus stays warning-free, and that the
two authoring-error guards are excluded from the warn-by-default path.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases._common import routes_through_own_section_interior
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    PhaseInvariantError,
    _guard_inter_section_route_clears_own_section_interior,
    assert_render_layout_invariants,
    render_layout_invariant_specs,
)
from nf_metro.layout.routing import (
    compute_station_offsets,
    route_edges,
    route_edges_centred,
)
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.invariants import (
    _first_axis_crossing,
    _route_axis_segments,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PermissiveGuardWarning
from nf_metro.render import render_svg
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# A spread of gallery topologies so the invariant is exercised against more
# than one layout shape.
CLEAN_FIXTURES = [
    "rnaseq_sections.mmd",
    "rnaseq_auto.mmd",
    "variant_calling.mmd",
    "sarek_metro.mmd",
]


def _laid_out(name: str) -> MetroGraph:
    graph = parse_metro_mermaid((EXAMPLES / name).read_text())
    compute_layout(graph)
    return graph


def _inject_coincident_stations(graph: MetroGraph) -> tuple[str, str]:
    """Force one non-port station onto another's coordinates.

    Violates two Tier-A invariants at once (coincident coords + marker
    overlap), so the chokepoint has a deterministic, fixture-independent
    failure to report.
    """
    movable = [s for s in graph.stations.values() if not s.is_port]
    anchor, victim = movable[0], movable[1]
    victim.x, victim.y = anchor.x, anchor.y
    return anchor.id, victim.id


@pytest.mark.parametrize("name", CLEAN_FIXTURES)
def test_clean_fixture_emits_no_layout_invariant_warning(name: str) -> None:
    """A clean gallery fixture renders without any Tier-A layout warning."""
    graph = _laid_out(name)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    offenders = [
        str(w.message) for w in caught if "Tier-A invariants" in str(w.message)
    ]
    assert not offenders, f"{name}: clean fixture warned: {offenders}"


@pytest.mark.parametrize("name", CLEAN_FIXTURES)
def test_injected_violation_warns_by_default(name: str) -> None:
    """An injected Tier-A violation warns (not raises) on the default path."""
    graph = _laid_out(name)
    _inject_coincident_stations(graph)
    with pytest.warns(UserWarning, match="Tier-A invariants"):
        render_svg(graph, THEMES["nfcore"])


@pytest.mark.parametrize("name", CLEAN_FIXTURES)
def test_injected_violation_warning_is_a_permissive_guard_warning(name: str) -> None:
    """The default-path warning is categorised so a ``--permissive`` caller can
    tell a guard downgrade apart from an unrelated warning raised mid-render."""
    graph = _laid_out(name)
    _inject_coincident_stations(graph)
    with pytest.warns(PermissiveGuardWarning, match="Tier-A invariants"):
        render_svg(graph, THEMES["nfcore"])


@pytest.mark.parametrize("name", CLEAN_FIXTURES)
def test_injected_violation_raises_under_strict(name: str) -> None:
    """The same violation raises ``LayoutInvariantError`` under ``--strict``."""
    graph = _laid_out(name)
    graph.strict = True
    _inject_coincident_stations(graph)
    with pytest.raises(LayoutInvariantError, match="Tier-A invariants"):
        render_svg(graph, THEMES["nfcore"])


def test_strict_error_is_a_phase_invariant_error() -> None:
    """``LayoutInvariantError`` is a ``PhaseInvariantError`` so the CLI's layout
    handler surfaces it as a clean message, not a traceback."""
    assert issubclass(LayoutInvariantError, PhaseInvariantError)


@pytest.mark.parametrize("name", CLEAN_FIXTURES)
def test_chokepoint_is_observational(name: str) -> None:
    """Running the chokepoint must not move a single coordinate."""
    graph = _laid_out(name)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    before = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert_render_layout_invariants(graph, routes, offsets, strict=False)
    after = {sid: (s.x, s.y) for sid, s in graph.stations.items()}
    assert before == after, f"{name}: chokepoint perturbed station geometry"


def test_authoring_guards_are_excluded_from_chokepoint() -> None:
    """The two authoring-error guards (``ValueError`` on un-renderable input,
    already always-on in the engine) are not part of the warn-by-default
    chokepoint -- warning then rendering an un-renderable map would be wrong."""
    names = {spec.name for spec in render_layout_invariant_specs()}
    assert "_guard_no_same_row_backward_feed" not in names
    assert "_guard_no_mixed_entry_directions" not in names


def test_chokepoint_runs_the_migrated_bbox_guard() -> None:
    """``_guard_stations_within_bbox`` (migrated out of ``engine.py``) is part of
    the render chokepoint set."""
    names = {spec.name for spec in render_layout_invariant_specs()}
    assert "_guard_stations_within_bbox" in names


# The inter-section backtrack/wrap guards run on the always-on render path so a
# wrapped/backtracking bundle is a visible warning, not a silently-broken map.
# The last is the detector for the away-facing wrap the first three exempt as a
# legitimate reverse-flow route.
BACKTRACK_RENDER_GUARDS = [
    "_guard_inter_section_route_no_backtrack",
    "_guard_inter_section_route_no_full_width_backtrack",
    "_guard_serpentine_no_backtrack",
    "_guard_inter_section_route_clears_own_section_interior",
]

# Kept under ``regressions/`` (not the auto-discovered corpus root): some
# fixtures here raise under ``validate=True``, so the corpus-wide idempotence
# and manifest tests must not pick them up.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "regressions"
TOP_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize("guard_name", BACKTRACK_RENDER_GUARDS)
def test_backtrack_guards_on_render_path(guard_name: str) -> None:
    """The backtrack/wrap guards are part of the always-on render chokepoint set
    (Tier A), not the validate-only set."""
    names = {spec.name for spec in render_layout_invariant_specs()}
    assert guard_name in names


# A line that breezes through a section it never connects to is at least as
# visibly broken as the backtrack/wrap guards above, so it gets the same
# always-on treatment: warn by default, raise under --strict.
BREEZE_THROUGH_RENDER_GUARDS = [
    "_guard_no_route_through_section",
    "_guard_no_line_crosses_non_consumer",
]


@pytest.mark.parametrize("guard_name", BREEZE_THROUGH_RENDER_GUARDS)
def test_breeze_through_guards_on_render_path(guard_name: str) -> None:
    """The non-consumer-section breeze-through guards are part of the
    always-on render chokepoint set (Tier A), not the validate-only set."""
    names = {spec.name for spec in render_layout_invariant_specs()}
    assert guard_name in names


def test_render_header_clearance_guard_fires_under_strict() -> None:
    """``assert_render_header_clearance`` raises under strict when a section's
    bbox has grown far enough that the row below's header would overlap it, and
    warns otherwise."""
    from nf_metro.layout.constants import SECTION_HEADER_PROTRUSION
    from nf_metro.layout.phases.guards import assert_render_header_clearance

    graph = _laid_out("topologies/render_labelwrap_row_gap.mmd")
    graph.sections["qc_sec"].bbox_h += SECTION_HEADER_PROTRUSION + 40.0

    with pytest.raises(LayoutInvariantError, match="section header over the box"):
        assert_render_header_clearance(graph, strict=True)
    with pytest.warns(PermissiveGuardWarning, match="section header over the box"):
        assert_render_header_clearance(graph, strict=False)


# Non-rail fixtures whose sections stack vertically; the first forces late
# label wrapping (a two-word ``sambamba markdup`` and an ``RNA-SeQC`` dir-icon
# branch squeezed together) in a section sitting directly above another
# (#1461). Rail-mode graphs are exempt (see test_render_does_not_reflow_...).
ROW_GAP_FIXTURES = [
    "topologies/render_labelwrap_row_gap.mmd",
    "variant_calling.mmd",
    "rnaseq_auto.mmd",
]

_SECTION_RECT_RE = re.compile(
    r'<rect\b[^>]*\bclass="[^"]*nf-metro-section-box[^"]*"[^>]*'
    r'\bdata-section-id="(?P<sid>[^"]+)"'
)
_ATTR_RE = {a: re.compile(rf'\b{a}="([0-9.]+)"') for a in ("x", "y", "width", "height")}


def _rendered_section_rects(svg: str) -> dict[str, tuple[float, float, float, float]]:
    """Map each section id to its drawn ``(x, y, w, h)`` box in the final SVG.

    The rendered box reflects render-time label-wrap growth the pre-render
    layout bboxes never saw, so it is the geometry the clearance invariant must
    hold against.
    """
    rects: dict[str, tuple[float, float, float, float]] = {}
    for m in _SECTION_RECT_RE.finditer(svg):
        tag = svg[m.start() : svg.index(">", m.start())]
        vals = {a: float(_ATTR_RE[a].search(tag).group(1)) for a in _ATTR_RE}
        rects[m.group("sid")] = (vals["x"], vals["y"], vals["width"], vals["height"])
    return rects


def _min_rendered_row_gap(graph: MetroGraph, rects: dict) -> float | None:
    """Smallest gap between a column-overlapping adjacent-row section pair in
    the rendered rects, or ``None`` if no such pair exists."""
    smallest: float | None = None
    for usid, us in graph.sections.items():
        if usid not in rects:
            continue
        ux, uy, uw, uh = rects[usid]
        next_row = us.grid_row + us.grid_row_span
        for lsid, ls in graph.sections.items():
            if ls.grid_row != next_row or lsid not in rects:
                continue
            lx, ly, _, _ = rects[lsid]
            if ux >= lx + rects[lsid][2] or lx >= ux + uw:  # no horizontal overlap
                continue
            gap = ly - (uy + uh)
            smallest = gap if smallest is None else min(smallest, gap)
    return smallest


@pytest.mark.parametrize("name", ROW_GAP_FIXTURES)
def test_rendered_sections_clear_header_after_label_wrap(name: str) -> None:
    """No lower section's header badge overlaps the box above it in the
    *rendered* SVG, after label wrapping has grown any bbox (#1461)."""
    from nf_metro.layout.constants import (
        SAME_COORD_TOLERANCE,
        SECTION_HEADER_PROTRUSION,
    )

    graph = _laid_out(name)
    assert not graph.has_rail_sections, f"{name}: rail graphs are reflow-exempt"
    theme_name = graph.style if graph.style in THEMES else "nfcore"
    rects = _rendered_section_rects(render_svg(graph, THEMES[theme_name]))

    smallest = _min_rendered_row_gap(graph, rects)
    if smallest is None:
        return
    assert smallest >= SECTION_HEADER_PROTRUSION - SAME_COORD_TOLERANCE, (
        f"{name}: smallest rendered row gap {smallest:.1f}px leaves the lower "
        f"header overlapping (needs >= {SECTION_HEADER_PROTRUSION:.1f}px)"
    )


def test_header_collision_reflow_restores_section_y_gap() -> None:
    """The #1461 fixture's QC labels wrap at render and grow the box into the
    Quantification header; the reflow restores the full ``section_y_gap``."""
    from nf_metro.layout.constants import SAME_COORD_TOLERANCE, SECTION_Y_GAP

    graph = _laid_out("topologies/render_labelwrap_row_gap.mmd")
    rects = _rendered_section_rects(render_svg(graph, THEMES["nfcore"]))
    qx, qy, qw, qh = rects["qc_sec"]
    gap = rects["quant_sec"][1] - (qy + qh)
    assert gap >= SECTION_Y_GAP - SAME_COORD_TOLERANCE, (
        f"qc_sec -> quant_sec gap {gap:.1f}px, expected >= {SECTION_Y_GAP:.1f}px"
    )


def test_render_does_not_reflow_rail_mode_graph() -> None:
    """A rail-mode graph is left un-reflowed at render: its stations keep their
    laid-out Ys, so the rail tracks stay straight (#1461).

    Rail-mode sections anchor their per-line centrelines during
    ``compute_layout``; a render-time row shift cannot re-derive them, so
    pushing a rail row would kink the tracks. ``sarek_metro`` has a rail-mode
    ``calling`` section one row below a section whose labels wrap at render --
    the reflow trigger -- so this pins that the reflow is skipped for it.
    """
    from nf_metro.layout.constants import SAME_COORD_TOLERANCE

    graph = _laid_out("sarek_metro.mmd")
    assert graph.has_rail_sections, "expected sarek_metro to have a rail-mode section"
    pre_y = {sid: st.y for sid, st in graph.stations.items()}

    render_svg(graph, THEMES["nfcore"])

    moved = {
        sid: (pre_y[sid], graph.stations[sid].y)
        for sid in pre_y
        if abs(graph.stations[sid].y - pre_y[sid]) > SAME_COORD_TOLERANCE
    }
    assert not moved, f"render reflowed a rail-mode graph, shifting stations: {moved}"


def _render_fixture(name: str, *, strict: bool = False) -> None:
    graph = parse_metro_mermaid((FIXTURES / name).read_text())
    graph.strict = strict
    compute_layout(graph)
    render_svg(graph, THEMES["nfcore"])


# An inter-section route whose exit side faces away from its consumer must leave
# the box and route *around* it, never claw back through a section interior
# (#1083).  The repros exercise both away-facing-exit mechanisms:
#   - the away_exit pair: forced-grid TB BOTTOM exits feeding a TOP entry that
#     sits above them (the #1078 / #1074 shape reduced to a stable repro);
#   - variant_calling_tuned at fold 8: a real pipeline where the compact fold
#     makes ``variant_calling`` a tall TB bridge whose LEFT exit feeds the
#     ``reporting`` convergence sink one row below and one column left.
# ``(fixture_dir, name, fold)`` -- ``fold`` is the parse-time column budget
# (``None`` keeps the fixture's own threshold).
ROUTE_AROUND_REPROS = [
    ("regressions", "away_exit_wrap_interior_left.mmd", None),
    ("regressions", "away_exit_wrap_interior_right.mmd", None),
    ("examples", "variant_calling_tuned.mmd", 8),
]


def _laid_out_repro(fixture_dir: str, name: str, fold: int | None) -> MetroGraph:
    base = FIXTURES if fixture_dir == "regressions" else EXAMPLES
    graph = parse_metro_mermaid((base / name).read_text(), max_station_columns=fold)
    compute_layout(graph)
    return graph


@pytest.mark.parametrize("fixture_dir,name,fold", ROUTE_AROUND_REPROS)
def test_away_facing_exit_routes_around_own_section(
    fixture_dir: str, name: str, fold: int | None
) -> None:
    """No inter-section route runs back through its own source/target interior:
    the away-facing exit leaves the box and goes around it (#1083)."""
    graph = _laid_out_repro(fixture_dir, name, fold)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    offenders = routes_through_own_section_interior(
        graph, routes=routes, offsets=offsets
    )
    assert not offenders, (
        f"{name}: inter-section route(s) claw back through their own section "
        f"interior instead of routing around it: "
        f"{[(rp.edge.source, rp.edge.target, sid) for rp, sid in offenders]}"
    )


@pytest.mark.parametrize("fixture_dir,name,fold", ROUTE_AROUND_REPROS)
def test_away_facing_exit_renders_without_wrap_warning(
    fixture_dir: str, name: str, fold: int | None
) -> None:
    """The around-the-box route leaves every render-path Tier-A wrap guard
    silent (the route is correct, not merely tolerated)."""
    graph = _laid_out_repro(fixture_dir, name, fold)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    tier_a = [w for w in caught if "Tier-A invariants" in str(w.message)]
    assert not tier_a, (
        f"{name}: unexpected Tier-A wrap warning(s): {[str(w.message) for w in tier_a]}"
    )


def test_interior_wrap_guard_fires_on_an_injected_crossing() -> None:
    """The interior-clearance guard raises when a route *does* run through its
    own target box -- the backstop that proves the around-the-box guarantee."""
    graph = parse_metro_mermaid(
        (FIXTURES / "away_exit_wrap_interior_left.mmd").read_text()
    )
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    inter = next(rp for rp in routes if rp.is_inter_section)
    tgt_sec = graph.sections[graph.section_for_station(inter.edge.target)]
    cx = tgt_sec.bbox_x + tgt_sec.bbox_w / 2
    cy = tgt_sec.bbox_y + tgt_sec.bbox_h / 2
    crossing = RoutedPath(
        edge=inter.edge,
        line_id=inter.line_id,
        points=[(cx - 200, cy), (cx + 200, cy)],
        is_inter_section=True,
    )
    with pytest.raises(PhaseInvariantError, match="instead of routing around it"):
        _guard_inter_section_route_clears_own_section_interior(
            graph, "after final", routes=[crossing], offsets=offsets
        )


# A feeder rising from a row below the target into the target's TOP entry port:
# the port faces away from the feeder, so a straight rise into it would plough
# up through the box interior (#1522).  The leg must instead route around a side
# of the box and approach the port from above.  Both fixtures place
# ``novel_transcripts`` below ``orf_calling``; the second sits several columns
# away so the leg carries the full width before wrapping the box's far side.
TOPOLOGIES = EXAMPLES / "topologies"
ROUTE_AROUND_TOP_ENTRY_FIXTURES = [
    "route_around_to_top_entry.mmd",
    "route_around_far_column_top_entry.mmd",
]


@pytest.mark.parametrize("name", ROUTE_AROUND_TOP_ENTRY_FIXTURES)
def test_below_feeder_routes_around_target_into_top_entry(name: str) -> None:
    """The ``novel_transcripts -> orf_calling`` leg reaches the top entry port by
    routing around the box, with zero crossings of ``orf_calling``'s interior."""
    graph = parse_metro_mermaid((TOPOLOGIES / name).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    leg = next(
        (
            rp
            for rp in routes
            if "novel_transcripts" in rp.edge.source and "orf_calling" in rp.edge.target
        ),
        None,
    )
    assert leg is not None, f"{name}: no novel_transcripts->orf_calling leg routed"
    offenders = routes_through_own_section_interior(
        graph, routes=routes, offsets=offsets
    )
    crossed = [sid for rp, sid in offenders if rp.edge is leg.edge]
    assert not crossed, (
        f"{name}: novel_transcripts->orf_calling leg runs through the interior "
        f"of {crossed}; points={[(round(x), round(y)) for x, y in leg.points]}"
    )


# A side-branch section sharing a topo column with the spine: when the fold
# budget lands on that branch column, one branch member is a sink that defeats
# the straight-drop, so a downstream section is placed behind its producer and
# the inter-section bundle wraps back through a section interior.  Auto-layout
# restricts fold points to spine links that can drop forward (#1080/#1081), so
# the producer leads its consumer and the wrap guard stays silent.
RESOLVED_WRAP_FIXTURES = [
    "rnaseq_branch_fold_wrap.mmd",
]


@pytest.mark.parametrize("name", RESOLVED_WRAP_FIXTURES)
def test_branch_fold_repro_lays_out_forward(name: str) -> None:
    """The producer section is placed ahead of (or level with) its consumer in
    grid columns, so no inter-section bundle reads backward against the flow."""
    graph = parse_metro_mermaid((FIXTURES / name).read_text())
    compute_layout(graph)
    genome = graph.sections["genome_align"]
    post = graph.sections["postprocessing"]
    assert genome.grid_col <= post.grid_col, (
        f"genome_align (col {genome.grid_col}) must not sit ahead of its "
        f"consumer postprocessing (col {post.grid_col})"
    )


@pytest.mark.parametrize("name", RESOLVED_WRAP_FIXTURES)
def test_branch_fold_repro_renders_without_wrap(name: str) -> None:
    """The forward placement leaves the render path's wrap guards silent."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _render_fixture(name)
    tier_a = [w for w in caught if "Tier-A invariants" in str(w.message)]
    assert not tier_a, (
        f"unexpected Tier-A warning(s): {[str(w.message) for w in tier_a]}"
    )


# A flow-side entry port whose target sits on the section trunk while an earlier
# non-consumer station shares that trunk row: the entry runway must keep its flat
# run on the entry (source) row and descend into the target only past the
# bypassed station, rather than running the runway along the target trunk Y
# straight through the non-consumer's marker (#1293).
ENTRY_RUNWAY_BYPASS_FIXTURES = [
    "target_entry_runway_bypass.mmd",
]


@pytest.mark.parametrize("name", ENTRY_RUNWAY_BYPASS_FIXTURES)
def test_entry_runway_clears_trunk_row_non_consumer(name: str) -> None:
    """The entry runway to a trunk-row target does not rake a non-consumer
    station sharing that trunk row (#1293)."""
    graph = parse_metro_mermaid((TOP_FIXTURES / name).read_text())
    compute_layout(graph)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    crossings = [
        str(w.message)
        for w in caught
        if "_guard_no_line_crosses_non_consumer" in str(w.message)
    ]
    assert not crossings, (
        f"{name}: entry route rakes a non-consumer marker: {crossings}"
    )


# The folded fixture feeds a far-side LEFT entry from a LEFT exit in an adjacent
# same-row column, and a RIGHT entry two rows below its source.  A far-side feed
# must wrap around the target box rather than plough its interior, and a descent
# from above must run its traverse in the with-flow band just above the target
# row rather than dive under the whole row counter to that row's flow (#1317).
FAR_SIDE_WRAP_GUARDS = [
    "_guard_routes_enter_sections_at_ports",
    "_guard_entry_approach_from_port_side",
    "_guard_no_artefactual_counter_flow",
    "_guard_no_dogleg_crosses_exempt_trunk",
]


@pytest.mark.parametrize("guard_name", FAR_SIDE_WRAP_GUARDS)
def test_far_side_entry_wraps_without_plough_or_counterflow(guard_name: str) -> None:
    """A far-side entry fed from the opposite side wraps around its target box
    (never ploughing the interior or the wrong band) for the folded riboseq
    fixture (#1317)."""
    from nf_metro.layout.phases import guards as guards_module

    graph = parse_metro_mermaid(
        (TOP_FIXTURES / "target_entry_runway_bypass.mmd").read_text()
    )
    compute_layout(graph)
    routes = route_edges(graph)
    guard = getattr(guards_module, guard_name)
    guard(graph, guard_name, routes=routes)


def test_same_side_culdesac_entry_reanchors_to_leading_edge() -> None:
    """A same-side-I/O cul-de-sac section that also takes an opposite-side
    fold-in feed re-anchors its flow-axis entry to the leading edge, keeping the
    entry beside its own-side consumers rather than raking the section's full
    width against its internal trunk (#1293/#1317; same-side idiom #1182).

    `feeder_l1` in `target_entry_runway_bypass` enters `l1` from the left
    (`al1 -> fl0, fl1`), exits `l1` to the left (`fl2/fl3 -> target`), and folds
    a third feed in from the right (`fb_out -> fl3`).  The right-side fold-in
    must not mask the left entry's fold: the entry belongs beside its own-side
    consumers `fl0/fl1`, which sit at the RL leading (right) edge."""
    from nf_metro.layout.phases.guards import iter_opposing_line_overlaps
    from nf_metro.parser.model import PortSide

    graph = parse_metro_mermaid(
        (TOP_FIXTURES / "target_entry_runway_bypass.mmd").read_text()
    )
    entry_sides = [side for side, lines in graph.sections["feeder_l1"].entry_hints]
    assert entry_sides == [PortSide.RIGHT], (
        "feeder_l1's left entry should re-anchor to the RL leading (right) edge "
        f"beside its own-side consumers, got {entry_sides}"
    )

    compute_layout(graph, validate=True)
    overlaps = [
        ov
        for ov in iter_opposing_line_overlaps(graph)
        if ov.line_id == "l1"
        and "feeder_l1" in (ov.tgt_a + ov.tgt_b + ov.src_a + ov.src_b)
    ]
    assert not overlaps, f"feeder_l1 l1 still folds back over itself: {overlaps}"


# A flow-side entry port that lands ON the target's trunk row while a
# non-consumer station shares that row between the port and the target: the
# straight run would pass through the blocker's marker, so the entry route must
# bow off the trunk, past the blocker, and drop back on before the target
# (#1315).
ENTRY_TRUNK_ROW_BOW_FIXTURES = [
    "entry_trunk_row_bow.mmd",
]


@pytest.mark.parametrize("name", ENTRY_TRUNK_ROW_BOW_FIXTURES)
def test_entry_trunk_row_bow_clears_non_consumer(name: str) -> None:
    """A trunk-row entry run bows over a same-row non-consumer station rather
    than raking its marker (#1315)."""
    graph = parse_metro_mermaid((FIXTURES / name).read_text())
    compute_layout(graph)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    crossings = [
        str(w.message)
        for w in caught
        if "_guard_no_line_crosses_non_consumer" in str(w.message)
    ]
    assert not crossings, (
        f"{name}: trunk-row entry route rakes a non-consumer marker: {crossings}"
    )


# A vertical-flow (TB) section whose bottom-most internal station sits above the
# flow-perpendicular exit port: the late bbox-bottom fit must keep the BOTTOM
# exit port on the section boundary rather than leaving it stranded inside the
# grown box (#1294).
TB_EXIT_BOUNDARY_FIXTURES = [
    "tb_exit_terminal_on_carrier.mmd",
]


@pytest.mark.parametrize("name", TB_EXIT_BOUNDARY_FIXTURES)
def test_tb_exit_port_stays_on_bbox_boundary(name: str) -> None:
    """A vertical-flow section's flow-perpendicular exit port sits on the
    section bbox boundary after the late bbox settling (#1294)."""
    graph = parse_metro_mermaid((TOP_FIXTURES / name).read_text())
    compute_layout(graph)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        render_svg(graph, THEMES["nfcore"])
    off_boundary = [
        str(w.message) for w in caught if "_guard_ports_on_boundaries" in str(w.message)
    ]
    assert not off_boundary, (
        f"{name}: an exit port drifted off its section boundary: {off_boundary}"
    )


def test_tb_exit_terminal_on_carrier_validates_strict() -> None:
    """The fixture lays out clean under ``validate=True``: a BOTTOM exit feeding
    a far-side LEFT entry wraps around the target box and approaches the port
    horizontally from its own outward side (#1317)."""
    graph = parse_metro_mermaid(
        (TOP_FIXTURES / "tb_exit_terminal_on_carrier.mmd").read_text()
    )
    compute_layout(graph, validate=True)


CONVERGENT_ENTRY_FIXTURES = [
    "tb_exit_terminal_on_carrier.mmd",
]


@pytest.mark.parametrize("name", CONVERGENT_ENTRY_FIXTURES)
def test_convergent_entry_feeders_do_not_cross(name: str) -> None:
    """Distinct-line feeders converging on one entry port nest without crossing.

    Feeders forced down the single gap left of a wide row-span target are
    staggered into parallel channels; if the channel order does not account for
    each feeder's turn-down height, the feeder that turns down higher is placed
    to the port side and its long descent is raked by the other's horizontal
    traverse (#1326).  No two feeders sharing a target port may cross before it.
    """
    graph = parse_metro_mermaid((TOP_FIXTURES / name).read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    by_target: dict[str, list[RoutedPath]] = {}
    for rp in routes:
        if rp.is_inter_section:
            by_target.setdefault(rp.edge.target, []).append(rp)
    crossings: list[str] = []
    for target, feeders in by_target.items():
        for i in range(len(feeders)):
            for j in range(i + 1, len(feeders)):
                va, ha = _route_axis_segments(feeders[i])
                vb, hb = _route_axis_segments(feeders[j])
                hit = _first_axis_crossing(va, hb) or _first_axis_crossing(vb, ha)
                if hit is not None:
                    crossings.append(
                        f"{feeders[i].line_id} x {feeders[j].line_id} "
                        f"into {target} at {hit}"
                    )
    assert not crossings, f"{name}: converging feeders cross: {crossings}"
