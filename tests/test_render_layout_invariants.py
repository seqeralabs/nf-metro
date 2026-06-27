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
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
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


@pytest.mark.parametrize("guard_name", BACKTRACK_RENDER_GUARDS)
def test_backtrack_guards_on_render_path(guard_name: str) -> None:
    """The backtrack/wrap guards are part of the always-on render chokepoint set
    (Tier A), not the validate-only set."""
    names = {spec.name for spec in render_layout_invariant_specs()}
    assert guard_name in names


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
