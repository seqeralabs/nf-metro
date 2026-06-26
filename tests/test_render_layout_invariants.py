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
from nf_metro.layout.phases.guards import (
    LayoutInvariantError,
    PhaseInvariantError,
    assert_render_layout_invariants,
    render_layout_invariant_specs,
)
from nf_metro.layout.routing import compute_station_offsets, route_edges
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

# Forced-grid fixtures placing a consumer section so its producer's exit side
# faces away from it: the inter-section bundle wraps and claws back through a
# section interior (the #1078 / #1074 shape reduced to a stable repro).
WRAP_DEFECT_FIXTURES = [
    "away_exit_wrap_interior_left.mmd",
    "away_exit_wrap_interior_right.mmd",
]

# Kept under ``regressions/`` (not the auto-discovered corpus root): a defect
# fixture raises under ``validate=True``, so the corpus-wide idempotence and
# manifest tests must not pick it up.
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


@pytest.mark.parametrize("name", WRAP_DEFECT_FIXTURES)
def test_interior_wrap_warns_by_default(name: str) -> None:
    """A bundle that wraps back through its own section interior warns on the
    default render path (it would render a silently-broken map otherwise)."""
    with pytest.warns(UserWarning, match="Tier-A invariants"):
        _render_fixture(name)


@pytest.mark.parametrize("name", WRAP_DEFECT_FIXTURES)
def test_interior_wrap_raises_under_strict(name: str) -> None:
    """The same wrap raises ``LayoutInvariantError`` under ``--strict``."""
    with pytest.raises(LayoutInvariantError, match="clears_own_section_interior"):
        _render_fixture(name, strict=True)


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
