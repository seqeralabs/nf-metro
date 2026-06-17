"""Cross-section layout invariants for inter-section bundle alignment.

These tests assert that the row trunk Y is consistent across sections in
the same grid row, that symmetric-fan column-mates land at mirrored Ys,
and that off-track inputs sit above their consumer's trunk.  They catch
regressions where one section's trunk drifts from the row's anchor (the
"limma kink" bug) or where fan re-centering leaves stations asymmetric.

The fixtures exercise real pipeline graphs with multi-line bundles,
fan-out columns, and off-track inputs (differentialabundance) plus a
two-section grid with simpler topology (variant calling).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import pytest
from conftest import CONTENT_PLACEMENT_PHASES
from layout_validator import check_station_as_elbow

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    DIAGONAL_SLOPE_RATIO,
    EDGE_TO_BUNDLE_CLEARANCE,
    INTER_ROW_EDGE_CLEARANCE,
    MIN_STATION_FLAT_LENGTH,
    ROW_BAND_SLACK,
    SECTION_HEADER_PROTRUSION,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    X_SPACING,
)
from nf_metro.layout.engine import (
    PhaseInvariantError,
    compute_layout,
    compute_min_y_spacing,
    is_loop_side_branch_station,
)
from nf_metro.layout.geometry import segment_intersects_bbox
from nf_metro.layout.labels import (
    _label_bbox,
    find_wrapped_label_trunk_strikes,
    place_labels,
    segment_strikes_label,
)
from nf_metro.layout.phases._common import _grow_section_bbox_upward
from nf_metro.layout.phases.bbox import (
    _section_band_is_empty,
    _section_content_hug_top,
    _section_fit_top,
)
from nf_metro.layout.phases.off_track import (
    _is_single_trunk_lr_section,
    _off_track_anchor_of,
    _off_track_fit_top,
    _off_track_groups,
    _off_track_lift_step,
    _off_track_output_below,
    _reanchor_off_track_to_consumer,
    _section_distinct_trunk_ys,
)
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import resolve_section
from nf_metro.layout.routing.invariants import (
    check_bundle_order_preserved,
    check_no_collinear_distinct_lines,
)
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide, Section, Station
from nf_metro.render.svg import (
    _compute_icon_obstacles,
    _icon_obstacles_by_station,
    apply_route_offsets,
)
from nf_metro.themes import THEMES

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Tolerance for "same Y" assertions.  The grid pitch defaults to 55px;
# 1px slack absorbs sub-pixel rounding from fan-recenter phases.
_Y_TOL = 1.0


@lru_cache(maxsize=None)
def _resolve_fixture(name: str) -> Path:
    """Resolve a fixture name to a concrete .mmd path.

    Accepts:
      - bare basenames (legacy: ``da_pipeline.mmd``) which resolve under
        ``tests/fixtures/`` for backwards compatibility, then fall back
        to ``examples/`` and its subdirs.
      - paths relative to either ``tests/fixtures/`` or ``examples/``
        (e.g. ``topologies/upward_bypass.mmd``).
      - paths relative to the repo root.
    """
    p = Path(name)
    candidates = [
        FIXTURES / p,
        EXAMPLES / p,
        EXAMPLES / "topologies" / p,
        EXAMPLES / "guide" / p,
        FIXTURES / "topologies" / p,
        REPO_ROOT / p,
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"Could not find fixture {name!r} under tests/fixtures or examples"
    )


# Layout is deterministic, so the corpus-wide invariants share one computed
# graph per (fixture, kwargs) rather than re-running the pipeline once per
# invariant (~36 full-corpus invariants over 92 fixtures).  Each caller gets a
# deep copy, so a test that mutates its graph (routing, label placement) cannot
# leak into another.  A test that monkeypatches a layout phase must pass
# ``_cache=False``: a cached graph would either return pre-patch geometry or, in
# a probe-style test, skip the patched code entirely and pass vacuously.
_LAYOUT_CACHE: dict[tuple[str, tuple], MetroGraph] = {}


def _layout(fixture: str, *, _cache: bool = True, **kwargs) -> MetroGraph:
    """Parse a fixture file and run the full layout pipeline.

    ``fixture`` may be a name under ``tests/fixtures/`` (legacy) or a
    name under ``examples/`` and its subdirs (``topologies/``, ``guide/``).
    Pass ``center_ports=False`` to opt out of the centre-ports default
    that the older fixtures relied on; tests over the full example
    corpus should not override it because example files declare the
    directive directly.
    """
    path = _resolve_fixture(fixture)
    key = (str(path), tuple(sorted(kwargs.items())))
    if _cache and key in _LAYOUT_CACHE:
        return copy.deepcopy(_LAYOUT_CACHE[key])
    text = path.read_text()
    graph = parse_metro_mermaid(text)
    # Legacy fixtures under tests/fixtures/ were authored before the
    # parser parsed center_ports directly; preserve their implicit
    # center_ports=True default.  Examples set the directive in-file.
    if path.is_relative_to(FIXTURES) and "center_ports" not in kwargs:
        graph.center_ports = True
    elif "center_ports" in kwargs:
        graph.center_ports = kwargs.pop("center_ports")
    compute_layout(graph, **kwargs)
    if not _cache:
        return graph
    _LAYOUT_CACHE[key] = graph
    return copy.deepcopy(graph)


def _layout_example(name: str, *, _cache: bool = True, **kwargs) -> MetroGraph:
    """Parse an example file and run layout, honouring its own directives."""
    path = EXAMPLES / name
    key = (str(path), tuple(sorted(kwargs.items())))
    if _cache and key in _LAYOUT_CACHE:
        return copy.deepcopy(_LAYOUT_CACHE[key])
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph, **kwargs)
    if not _cache:
        return graph
    _LAYOUT_CACHE[key] = graph
    return copy.deepcopy(graph)


# ---------------------------------------------------------------------------
# Fixture discovery helpers for full-corpus parametrization
# ---------------------------------------------------------------------------


def _discover_fixtures() -> list[str]:
    """Return all ``%%metro``-format .mmd files under tests/fixtures and
    examples, addressable via :func:`_resolve_fixture`.

    Excludes Nextflow-format flowcharts under ``tests/fixtures/nextflow/``
    (those are parser inputs, not layout inputs), any file lacking a
    ``%%metro`` directive, and any fixture using ``line_spread: rails`` (which
    runs a dedicated layout pipeline with its own geometry contract; see
    ``tests/test_rail_mode.py``).  The substring match catches both the
    graph-wide ``line_spread: rails`` and the per-section
    ``line_spread: rails | <id>`` form: a mixed fixture's rail sections carry
    spanning-pill geometry the corpus invariants don't model, so the whole
    fixture is routed to the dedicated rail tests instead.
    """
    roots = [
        (FIXTURES, ""),
        (FIXTURES / "topologies", "topologies/"),
        (EXAMPLES, ""),
        (EXAMPLES / "topologies", "topologies/"),
        (EXAMPLES / "guide", "guide/"),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for root, prefix in roots:
        if not root.exists():
            continue
        for p in sorted(root.glob("*.mmd")):
            text = p.read_text(errors="ignore")
            if "%%metro" not in text:
                continue
            if "line_spread: rails" in text:
                continue
            # Address all examples paths through the ``examples`` resolver
            # without the leading ``examples/`` so tests can pick up either
            # the legacy fixtures or the examples corpus uniformly.
            rel = prefix + p.name
            if rel in seen:
                continue
            seen.add(rel)
            result.append(rel)
    return result


ALL_FIXTURES = _discover_fixtures()


@lru_cache(maxsize=None)
def _fixture_text(name: str) -> str:
    """Return raw text of a fixture for precondition filtering."""
    return _resolve_fixture(name).read_text()


def _fixtures_with(predicate) -> list[str]:
    """Return the subset of ``ALL_FIXTURES`` for which ``predicate(text)``
    is truthy.  Used to narrow parametrization to fixtures that satisfy
    an invariant's precondition (e.g. fixtures declaring off-track inputs).
    """
    return [f for f in ALL_FIXTURES if predicate(_fixture_text(f))]


_FIXTURES_WITH_OFF_TRACK = _fixtures_with(lambda t: "off_track:" in t)


def _off_track_output_sinks(graph: MetroGraph) -> dict[str, str]:
    """Map each off-track *output* (producer-fed sink) to its producer.

    Built on production's :func:`_off_track_anchor_of`, which resolves an
    input to its consumer and a sink to its producer.  An off-track station
    whose anchor it also feeds (an out-edge to the anchor) is an input; the
    rest are producer-fed sinks.
    """
    sinks: dict[str, str] = {}
    for off_id, anchor_id in _off_track_anchor_of(graph).items():
        if any(e.target == anchor_id for e in graph.edges_from(off_id)):
            continue
        sinks[off_id] = anchor_id
    return sinks


def _off_track_roles(text: str) -> tuple[bool, bool]:
    """Return ``(has_input, has_output)`` for a fixture's off-track stations.

    The ``off_track:`` directive is identical for both roles; only the edge
    direction distinguishes an input (feeds an on-track consumer) from a
    producer-fed sink, so classification needs the parsed graph.
    """
    try:
        g = parse_metro_mermaid(text)
    except Exception:
        return (False, False)
    sinks = _off_track_output_sinks(g)
    anchored = set(_off_track_anchor_of(g))
    return (bool(anchored - sinks.keys()), bool(sinks))


_OFF_TRACK_ROLES = {
    f: _off_track_roles(_fixture_text(f)) for f in _FIXTURES_WITH_OFF_TRACK
}
_FIXTURES_WITH_OFF_TRACK_INPUT = [f for f, r in _OFF_TRACK_ROLES.items() if r[0]]
_FIXTURES_WITH_OFF_TRACK_OUTPUT = [f for f, r in _OFF_TRACK_ROLES.items() if r[1]]
_FIXTURES_MULTI_SECTION = _fixtures_with(lambda t: t.count("subgraph") >= 2)
_FIXTURES_COMPACT = _fixtures_with(lambda t: "compact_offsets: true" in t)

# Multi-section gallery fixtures plus the serpentine-stacked
# regression.  The regression's narrow ``reporting`` column nests under the
# wide ``preprocessing`` row-span (exposing the nested-column dog-leg
# geometry, #425) and its ``variant_calling`` row-span (rows 1-3) separates
# an inter-row wrap's source/target rows by a multi-row placement (#422); the
# multi-section gallery fixtures cover the adjacent-row, non-rowspan wrap
# (via ``topologies/stacked_lr_serpentine.mmd``).
_FIXTURES_MULTI_SECTION_PLUS_STACK = sorted(
    {*_FIXTURES_MULTI_SECTION, "regressions/stacked_collector_fanin.mmd"}
)
_FIXTURES_DOGLEG = _FIXTURES_MULTI_SECTION_PLUS_STACK
_FIXTURES_INTER_ROW_CLEARANCE = _FIXTURES_MULTI_SECTION_PLUS_STACK


def _fixtures_with_bypass() -> list[str]:
    """Return fixtures whose layout produces at least one ``__bypass_``
    hidden virtual station.  Computed by running layout once per fixture
    at import time; cached at module level so the test parametrization
    doesn't repeat the work.
    """
    out: list[str] = []
    for name in ALL_FIXTURES:
        try:
            g = _layout(name)
        except Exception:
            continue
        if any(
            st.is_hidden and sid.startswith("__bypass_")
            for sid, st in g.stations.items()
        ):
            out.append(name)
    return out


_FIXTURES_WITH_BYPASS = _fixtures_with_bypass()


def _fixtures_with_downward_output() -> list[str]:
    """Fixtures whose layout routes at least one off-track output below its
    producer (a downward-branch output; see
    :func:`_off_track_output_below`).
    """
    out: list[str] = []
    for name in _FIXTURES_WITH_OFF_TRACK_OUTPUT:
        try:
            g = _layout(name)
        except Exception:
            continue
        if _off_track_output_below(g):
            out.append(name)
    return out


_FIXTURES_WITH_DOWNWARD_OUTPUT = _fixtures_with_downward_output()


def _fixtures_with_above_output() -> list[str]:
    """Off-track-output fixtures that lift at least one output *above* its
    producer, excluding any whose only outputs route downward.
    """
    out: list[str] = []
    for name in _FIXTURES_WITH_OFF_TRACK_OUTPUT:
        try:
            g = _layout(name)
        except Exception:
            continue
        below = _off_track_output_below(g)
        if any(o not in below for o in _off_track_output_sinks(g)):
            out.append(name)
    return out


_FIXTURES_WITH_ABOVE_OUTPUT = _fixtures_with_above_output()


def _off_track_input_consumer_map(
    graph: MetroGraph, junction_ids: set[str]
) -> dict[str, str]:
    """Map each off-track input to its on-track consumer (first edge wins).

    Re-derived from edges here rather than via production's
    :func:`_off_track_anchor_of`, so an invariant built on this map stays an
    independent oracle of the engine's own consumer resolution.
    """
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)
    return consumer_of


def _off_track_consumer_ids(graph: MetroGraph, junction_ids: set[str]) -> set[str]:
    """On-track stations fed directly by an off-track input."""
    return set(_off_track_input_consumer_map(graph, junction_ids).values())


def _in_section_on_track_successors(
    graph: MetroGraph, station_id: str, junction_ids: set[str]
) -> list[str]:
    """On-track, non-port, same-section successors of ``station_id``."""
    sec = graph.stations[station_id].section_id
    succs: list[str] = []
    for edge in graph.edges_from(station_id):
        tgt = graph.stations.get(edge.target)
        if (
            tgt is None
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
            or tgt.section_id != sec
        ):
            continue
        if tgt.id not in succs:
            succs.append(tgt.id)
    return succs


def _fixtures_with_linear_off_track_consumer() -> list[str]:
    """Off-track-input fixtures with at least one consumer that continues
    straight into the section trunk (exactly one on-track in-section
    successor), the precondition of
    :func:`test_off_track_consumer_on_section_trunk`.
    """
    out: list[str] = []
    for name in _FIXTURES_WITH_OFF_TRACK_INPUT:
        try:
            g = _layout(name)
        except Exception:
            continue
        jids = set(g.junctions)
        consumers = _off_track_consumer_ids(g, jids)
        if any(
            len(_in_section_on_track_successors(g, cid, jids)) == 1 for cid in consumers
        ):
            out.append(name)
    return out


_FIXTURES_WITH_LINEAR_OFF_TRACK_CONSUMER = _fixtures_with_linear_off_track_consumer()


# Pre-existing layout regressions surfaced by parametrizing single-fixture
# invariants over the full corpus.  Each entry pins a fixture/invariant
# pair as ``xfail(strict=False)`` so the bug is documented in code while
# the coverage extension still ships green.  When the underlying bug is
# fixed the entry becomes XPASS and can be removed.
_XFAIL_KEY = "xfail"


def _fp(name: str, fail_reason: str | None = None):
    """Return a ``pytest.param`` for ``name`` with optional xfail marker.

    Xfails are strict: an unexpected pass reds CI, forcing the marker to be
    removed (i.e. the bug is sealed off and won't silently re-open).
    """
    if fail_reason is None:
        return pytest.param(name, id=name)
    return pytest.param(
        name, id=name, marks=pytest.mark.xfail(reason=fail_reason, strict=True)
    )


def _params_with_xfails(fixtures: list[str], xfails: dict[str, str]) -> list:
    """Return a parametrize list mixing plain fixtures and xfail-marked ones."""
    return [_fp(f, xfails.get(f)) for f in fixtures]


# Fixture entries known to fail ``test_row_trunk_marker_cy_consistent``
# because the row-bundle trunk Y drifts between sections in the same row.
# Surfaced by the cross-corpus parametrization; tracked separately from
# this coverage PR.  See nf-metro audit /tmp/invariant-audit.md item 1.
_XFAIL_ROW_TRUNK_CY: dict[str, str] = {}


# Inter-section exit-port cy drifts from the matching entry-port cy in
# the next section.  See nf-metro audit item 1 (the "limma kink"
# regression family).  Limited to multi-section fixtures.
_XFAIL_NO_KINK: dict[str, str] = {}


# Symmetric-fan pairs (two full-bundle stations in the same column) end
# up asymmetric around the row trunk cy.  Audit item 10.
_XFAIL_SYMFAN: dict[str, str] = {}


# Lines cross non-consumer station markers (the "breeze-past" regression
# family).  Audit item 3.  Limited to the guide fixtures where the
# regression manifests; the production maps already route around their
# non-consumer stations.
_XFAIL_BREEZE_PAST: dict[str, str] = {}


# Section bbox bottom doesn't carry the configured section_y_padding
# below the lowest station marker.  Likely linked to off-track input
# placement in differentialabundance_default at default y_spacing.
_XFAIL_BBOX_BOTTOM_PAD: dict[str, str] = {}


def _row_lr_sections(graph: MetroGraph) -> dict[int, list]:
    """Group LR/RL sections by grid_row, skipping row-spanners."""
    rows: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if (
            sec.bbox_h <= 0
            or sec.grid_row < 0
            or sec.direction not in ("LR", "RL")
            or sec.grid_row_span > 1
        ):
            continue
        rows[sec.grid_row].append(sec)
    return rows


def _section_lr_port_ys(graph: MetroGraph, section) -> list[float]:
    """Return Y values of the section's LR (LEFT/RIGHT) ports."""
    ys: list[float] = []
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if (
            port is not None
            and st is not None
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            ys.append(st.y)
    return ys


def _section_trunk_info(
    graph: MetroGraph,
    section,
    offsets: dict[tuple[str, str], float],
) -> tuple[float, float, float] | None:
    """Return ``(cy, y_min, y_max)`` of the trunk station, or ``None``."""
    port_ys = _section_lr_port_ys(graph, section)
    if not port_ys:
        return None
    port_y = port_ys[0]
    bundle = _section_full_bundle(graph, section)
    if not bundle:
        return None
    port_set = set(section.entry_ports) | set(section.exit_ports)
    best: tuple[float, float, float, float] | None = None
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        lines = graph.station_lines(sid)
        if set(lines) != bundle:
            continue
        line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
        if not line_offs:
            continue
        y_min = st.y + min(line_offs)
        y_max = st.y + max(line_offs)
        cy = st.y + (min(line_offs) + max(line_offs)) / 2
        dist = abs(cy - port_y)
        if best is None or dist < best[0]:
            best = (dist, cy, y_min, y_max)
    if best is None:
        return None
    return (best[1], best[2], best[3])


def _section_trunk_marker_cy(
    graph: MetroGraph,
    section,
    offsets: dict[tuple[str, str], float],
) -> float | None:
    info = _section_trunk_info(graph, section, offsets)
    return info[0] if info is not None else None


def _section_full_bundle(graph: MetroGraph, section) -> set[str] | None:
    """The set of line ids that traverse the section's row bundle.

    Defined as the line set carried by the section's LR ports.
    """
    port_lines: set[str] = set()
    has_lr_port = False
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        has_lr_port = True
        port_lines.update(graph.station_lines(pid))
    return port_lines if (has_lr_port and port_lines) else None


# ---------------------------------------------------------------------------
# Row trunk Y consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(_FIXTURES_MULTI_SECTION, _XFAIL_ROW_TRUNK_CY),
)
def test_row_trunk_marker_cy_consistent(fixture):
    """All same-row LR sections must render their trunk marker at the
    same cy.  Inter-section bundles run horizontally between sections
    in the same grid row; a per-section drift in the trunk marker's
    rendered cy produces a visible kink at the section boundary.

    This is the regression test for the "9px limma kink" bug where
    section 2's trunk station sat 9px below sections 1 and 5 because
    of a stray bundle offset shift triggered by a side-branch feeder
    on the section's exit port.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    rows = _row_lr_sections(graph)
    for row, sections in rows.items():
        info: dict[str, tuple[float, float, float, set[str]]] = {}
        for sec in sections:
            trunk = _section_trunk_info(graph, sec, offsets)
            bundle = _section_full_bundle(graph, sec)
            if trunk is None or not bundle:
                continue
            cy, y_min, y_max = trunk
            info[sec.id] = (cy, y_min, y_max, bundle)
        if len(info) < 2:
            continue

        def _same_row(a: str, b: str) -> bool:
            cy_a, lo_a, hi_a, bun_a = info[a]
            cy_b, lo_b, hi_b, bun_b = info[b]
            bands_overlap = min(hi_a, hi_b) - max(lo_a, lo_b) >= -_Y_TOL
            return bands_overlap and bun_a == bun_b

        parent: dict[str, str] = {sid: sid for sid in info}

        def _find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(x: str, y: str) -> None:
            rx, ry = _find(x), _find(y)
            if rx != ry:
                parent[rx] = ry

        ids = list(info)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if _same_row(a, b):
                    _union(a, b)

        groups: dict[str, list[str]] = defaultdict(list)
        for sid in ids:
            groups[_find(sid)].append(sid)

        for members in groups.values():
            if len(members) < 2:
                continue
            anchor = members[0]
            target = info[anchor][0]
            for sid in members[1:]:
                cy = info[sid][0]
                assert abs(cy - target) < _Y_TOL, (
                    f"Row {row}: section {sid} trunk cy={cy} drifts from "
                    f"{anchor} cy={target}"
                )


# ---------------------------------------------------------------------------
# Straight-through bundle line keeps a constant offset
# ---------------------------------------------------------------------------

# (fixture, line_id) where the named line's whole route lies on a single
# base-Y trunk, so every station it touches must carry one per-line offset --
# any variation paints the line as a kink or slant.  These fixtures exercise
# the section-exit / junction-to-entry-port bundle-order paths in offsets.py
# where a straight-through line is reordered off its incoming slot.
_STRAIGHT_THROUGH_LINES = [
    ("topologies/junction_entry_collision.mmd", "alpha"),
    ("topologies/junction_entry_align.mmd", "alpha"),
]


@pytest.mark.parametrize("fixture,line_id", _STRAIGHT_THROUGH_LINES)
def test_straight_through_line_keeps_constant_offset(fixture, line_id):
    """A line confined to one base-Y trunk must carry a single offset.

    The line is purely horizontal, so any per-station offset variation is
    painted as a kink or slant.  Reordering the bundle at a section exit or
    across a junction-to-entry-port boundary breaks this.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    stations = [sid for sid in graph.stations if line_id in graph.station_lines(sid)]
    ys = [graph.stations[sid].y for sid in stations]
    assert max(ys) - min(ys) <= _Y_TOL, (
        f"{fixture}: {line_id} spans rows {min(ys)}..{max(ys)}; "
        "test precondition (single trunk) does not hold"
    )
    offs = {sid: round(offsets.get((sid, line_id), 0.0), 1) for sid in stations}
    distinct = sorted(set(offs.values()))
    assert len(distinct) == 1, (
        f"{fixture}: line {line_id} runs flat on one trunk but its offset "
        f"varies {distinct}; per-station offsets {offs}"
    )


# ---------------------------------------------------------------------------
# Merge-port re-joined line keeps its side on the outgoing run
# ---------------------------------------------------------------------------

# (fixture, line_id, row_y) where the line re-joins a bundle perpendicular at a
# multi-feeder merge port and then continues horizontally along the merge row.
# Once re-slotted to one side of the trunk it must keep that side across the
# whole row, so its offset is constant on every station at row_y; flipping
# sides on the outgoing run paints an avoidable crossover.
_MERGE_PORT_OUTGOING_LINES = [
    ("topologies/merge_port_above_approach.mmd", "bypass", 270.0),
]


@pytest.mark.parametrize("fixture,line_id,row_y", _MERGE_PORT_OUTGOING_LINES)
def test_merge_port_line_keeps_side_on_outgoing_run(fixture, line_id, row_y):
    """A line re-joined at a merge port keeps one offset along the merge row.

    The merge-port pass picks the bundle slot nearest the line's approach
    side; if the downstream consumer keeps the line's old slot it crosses the
    trunk on the outgoing run.  Every station the line touches at ``row_y``
    must therefore share one offset.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    on_row = [
        sid
        for sid in graph.stations
        if line_id in graph.station_lines(sid)
        and abs(graph.stations[sid].y - row_y) <= _Y_TOL
    ]
    offs = {sid: round(offsets.get((sid, line_id), 0.0), 1) for sid in on_row}
    distinct = sorted(set(offs.values()))
    assert len(distinct) == 1, (
        f"{fixture}: line {line_id} flips offset {distinct} along the merge "
        f"row y={row_y}, crossing the trunk; per-station offsets {offs}"
    )


# ---------------------------------------------------------------------------
# Symmetric fan column-mate Y equality
# ---------------------------------------------------------------------------


def _is_symfan_pair(graph: MetroGraph, sids: list[str]) -> bool:
    """Share a predecessor and sit on opposite sides of it in Y."""
    if len(sids) != 2:
        return False
    preds: dict[str, set[str]] = {sids[0]: set(), sids[1]: set()}
    for e in graph.edges:
        if e.target in preds:
            preds[e.target].add(e.source)
    common = preds[sids[0]] & preds[sids[1]]
    if not common:
        return False
    st0 = graph.stations.get(sids[0])
    st1 = graph.stations.get(sids[1])
    if st0 is None or st1 is None:
        return False
    for src_id in common:
        src = graph.stations.get(src_id)
        if src is None:
            continue
        d0 = st0.y - src.y
        d1 = st1.y - src.y
        if abs(d0) > _Y_TOL and abs(d1) > _Y_TOL and (d0 * d1) < 0:
            return True
    return False


def _section_fan_columns(graph: MetroGraph, section) -> dict[float, list[str]]:
    """Group full-bundle internal stations of a section by X column.

    Returns ``{x: [station_id, ...]}`` for columns with >= 2 full-bundle
    stations - the configurations that the symmetric-fan phases target.
    """
    bundle = _section_full_bundle(graph, section)
    if not bundle:
        return {}
    port_set = set(section.entry_ports) | set(section.exit_ports)
    cols: dict[float, list[str]] = defaultdict(list)
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden or st.off_track:
            continue
        if set(graph.station_lines(sid)) != bundle:
            continue
        cols[round(st.x, 1)].append(sid)
    return {x: sids for x, sids in cols.items() if len(sids) >= 2}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_SYMFAN),
)
def test_symfan_pairs_share_y(fixture):
    """When a section has exactly two full-bundle stations in the same
    column (a classic symmetric-fan pair such as Reporting's Shiny app
    + Quarto report, or Functional's GSEA + decoupler), the pair must
    be mirrored around the row's trunk Y so the rendered cys are
    equidistant from the trunk.

    Stronger property than "pair has matching Y": catches asymmetric
    placements like (trunk-55, trunk+0) that leave the bottom-fan slot
    empty.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    for sec in graph.sections.values():
        cols = _section_fan_columns(graph, sec)
        trunk_cy = _section_trunk_marker_cy(graph, sec, offsets)
        if trunk_cy is None:
            continue
        for x, sids in cols.items():
            if len(sids) != 2:
                continue
            if not _is_symfan_pair(graph, sids):
                continue
            cys = []
            for sid in sids:
                st = graph.stations[sid]
                lines = graph.station_lines(sid)
                line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
                cys.append(st.y + (min(line_offs) + max(line_offs)) / 2)
            cys.sort()
            above_gap = trunk_cy - cys[0]
            below_gap = cys[1] - trunk_cy
            assert abs(above_gap - below_gap) < _Y_TOL, (
                f"Section {sec.id} column x={x}: pair cys={cys} not "
                f"mirrored around trunk cy={trunk_cy} "
                f"(above_gap={above_gap}, below_gap={below_gap})"
            )


# ---------------------------------------------------------------------------
# Grid snap keeps same-column stations on distinct slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_grid_snap_keeps_columns_distinct(fixture):
    """Stage 6.4's grid snap must not pull two distinct same-column
    stations onto one slot.

    A fan-out column placed off the row pitch (e.g. a centre-ports fan at a
    40px spread over a 41px row grid) can round two adjacent termini onto
    the same Y, hidden later only by the centre-ports re-fan.  ``validate``
    runs ``_guard_no_station_overlap`` from the Stage 6.4 boundary, so the
    collapse surfaces as a position clash.
    """
    try:
        _layout(fixture, validate=True)
    except PhaseInvariantError as exc:
        # Unrelated pre-existing invariant failures are out of scope for
        # this test; only a station-overlap clash indicates the snap
        # collapsed a column.
        assert "position clash" not in str(exc), str(exc)


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_no_collinear_distinct_lines(fixture):
    """Two different lines must never render exactly on top of each other.

    A bundling/offset defect that collapses co-travelling lines onto one
    channel draws one stroke over the other.  Uses the final,
    offset-applied geometry.
    """
    from nf_metro.layout.routing.invariants import (
        check_no_collinear_distinct_lines,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_no_collinear_distinct_lines(graph, routes, offsets)
    assert not violations, "; ".join(v.message() for v in violations)


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_intra_section_collinear_distinct_lines(fixture):
    """Two different lines must never render on top of each other *within*
    a section either.

    The intra-section counterpart to ``test_no_collinear_distinct_lines``:
    ``check_no_collinear_distinct_lines`` only scans inter-section routes,
    so a defect that collapsed two co-travelling lines onto one channel
    inside a section body would slip through.  Uses the final,
    offset-applied geometry.
    """
    from nf_metro.layout.routing.invariants import (
        check_intra_section_collinear_distinct_lines,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_intra_section_collinear_distinct_lines(graph, routes, offsets)
    assert not violations, "; ".join(v.message() for v in violations)


def test_intra_section_collinear_check_detects_overlay():
    """Meaningfulness guard: the intra-section check fires when two distinct
    lines genuinely coincide on one channel.

    Locks the detector so the corpus test above is not vacuously green: two
    different-line intra-section runs sharing a horizontal channel over more
    than ``_COLLINEAR_MIN_SPAN``, with no shared endpoint, must be flagged.
    """
    from types import SimpleNamespace

    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.layout.routing.invariants import (
        check_intra_section_collinear_distinct_lines,
    )
    from nf_metro.parser.model import Edge

    def _run(src, tgt, line):
        return RoutedPath(
            edge=Edge(source=src, target=tgt, line_id=line),
            line_id=line,
            points=[(0.0, 100.0), (200.0, 100.0)],
            is_inter_section=False,
            offsets_applied=True,
        )

    graph = SimpleNamespace(stations={})  # no endpoints => no convergence excuse
    routes = [_run("a", "b", "red"), _run("c", "d", "blue")]
    violations = check_intra_section_collinear_distinct_lines(graph, routes, {})
    assert violations, "expected a collinear overlay to be detected"
    assert {violations[0].line_a, violations[0].line_b} == {"red", "blue"}


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_stacked_elbow_graze(fixture):
    """Two stacked, non-parallel inter-section risers must not graze.

    Two different lines descending one inter-section gap as risers that merely
    meet at a shared elbow band - a deep descent landing on a port lane that a
    shallow descent then leaves - are separate corridors, not a parallel
    bundle.  Packed within ``BUNDLE_TO_BUNDLE_CLEARANCE`` of each other their
    opposing elbows overlap and the lines graze.  The gap layout must
    distribute them across the gap width instead.
    """
    from nf_metro.layout.routing.invariants import check_stacked_elbow_clearance

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_stacked_elbow_clearance(graph, routes, offsets)
    assert not violations, "; ".join(v.message() for v in violations)


def test_stacked_elbow_check_detects_graze():
    """Meaningfulness guard: the stacked-elbow check fires on a genuine graze.

    Locks the detector so the corpus test above is not vacuously green: two
    different-line vertical risers in one gap, stacked (their spans meet at one
    elbow band rather than overlapping) and within
    ``BUNDLE_TO_BUNDLE_CLEARANCE`` of each other in X, must be flagged.
    """
    from types import SimpleNamespace

    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.layout.routing.invariants import check_stacked_elbow_clearance
    from nf_metro.parser.model import Edge

    def _riser(src, tgt, line, x, y_lo, y_hi):
        return RoutedPath(
            edge=Edge(source=src, target=tgt, line_id=line),
            line_id=line,
            points=[(x, y_lo), (x, y_hi)],
            is_inter_section=True,
            offsets_applied=True,
        )

    # A deep descent landing at y=100 and a shallow descent leaving it, 6px
    # apart in X (< BUNDLE_TO_BUNDLE_CLEARANCE), overlapping only 3px in Y.
    graph = SimpleNamespace(stations={})
    routes = [
        _riser("up_src", "port", "red", 0.0, 0.0, 100.0),
        _riser("hub", "down_dst", "blue", 6.0, 97.0, 200.0),
    ]
    violations = check_stacked_elbow_clearance(graph, routes, {})
    assert violations, "expected a stacked-elbow graze to be detected"
    assert {violations[0].line_a, violations[0].line_b} == {"red", "blue"}


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_perp_entry_feed_not_collinear(fixture):
    """A TOP/BOTTOM entry port never sits at the Y of the station feeding it.

    The station-as-elbow constraint and the boundary snap hold a perpendicular
    entry port off the consumer's internal rows, while its feeders are exit
    ports seated in a vertically-distinct section, so a feed can never be
    collinear with the port; ``_guard_perp_entry_feed_not_collinear`` locks
    that out.
    """
    from nf_metro.layout.phases.guards import _guard_perp_entry_feed_not_collinear

    graph = _layout(fixture)
    _guard_perp_entry_feed_not_collinear(graph, "test")


def test_perp_entry_feed_collinear_guard_detects():
    """Meaningfulness guard: dragging a TOP entry port onto its feeder's Y
    makes ``_guard_perp_entry_feed_not_collinear`` raise.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_perp_entry_feed_not_collinear,
    )

    graph = _layout("topologies/cross_col_top_entry.mmd")
    _guard_perp_entry_feed_not_collinear(graph, "test")

    port_id, feeder_id = next(
        (pid, edge.source)
        for section in graph.sections.values()
        for pid in section.entry_ports
        if (port := graph.ports.get(pid)) is not None
        and port.side in (PortSide.TOP, PortSide.BOTTOM)
        for edge in graph.edges_to(pid)
    )
    graph.stations[port_id].y = graph.stations[feeder_id].y

    with pytest.raises(PhaseInvariantError, match="collinear with its feeder"):
        _guard_perp_entry_feed_not_collinear(graph, "test")


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_distinct_trunk_not_hidden_behind_exempt(fixture):
    """A distinct-line bypass trunk must not sit within a bundle gap of an
    exempt trunk it overlaps in X.

    ``normalize_exempt`` runs are placed by their own handler and the channel
    normaliser never sees them, so a different-line trunk drawn less than one
    ``OFFSET_STEP`` away (the stroke width) is painted over by the exempt run
    and hidden (issue #484: a whole stretch of the SNV-VCF line vanished under
    the BAM line).  ``_dogleg_off_exempt_trunks`` nudges it to a full gap.
    """
    from nf_metro.layout.constants import OFFSET_STEP
    from nf_metro.layout.routing.core import _collect_htrunks

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    movable = _collect_htrunks(routes)
    exempt = [
        t
        for t in _collect_htrunks(routes, include_exempt=True)
        if t.route.normalize_exempt
    ]
    for t in movable:
        for o in exempt:
            if o.route.line_id == t.route.line_id:
                continue
            overlap = min(t.x_hi, o.x_hi) - max(t.x_lo, o.x_lo)
            if overlap <= EDGE_TO_BUNDLE_CLEARANCE:
                continue
            assert abs(o.y - t.y) >= OFFSET_STEP - 0.1, (
                f"{fixture}: '{t.route.line_id}' trunk @{t.y:.1f} hidden behind "
                f"exempt '{o.route.line_id}' @{o.y:.1f} over {overlap:.0f}px"
            )


_PERP_ENTRY_BUNDLE_FIXTURES = [
    "fold_fan_across.mmd",
    "rnaseq_auto.mmd",
]


@pytest.mark.parametrize("fixture", _PERP_ENTRY_BUNDLE_FIXTURES)
def test_perp_entry_bundle_not_overspread(fixture):
    """A multi-line TOP/BOTTOM-port drop already separated by its port-side
    bundle offsets must stay one ``OFFSET_STEP`` apart, not double it.

    When the lines arrive at a shared perpendicular entry port pre-fanned into
    distinct slots, the per-line drop stagger is redundant; applying it anyway
    splays a tight bundle apart on the way into the section (issue #484
    follow-up).  The first vertical leg of each sibling route must therefore be
    spaced exactly one ``OFFSET_STEP`` from the next.
    """
    from nf_metro.layout.constants import OFFSET_STEP

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    by_key: dict[tuple[str, str], list[float]] = {}
    for rp in routes:
        port = graph.ports.get(rp.edge.source)
        if port is None or port.side not in (PortSide.TOP, PortSide.BOTTOM):
            continue
        # The drop channel is the first vertical leg out of the port; the
        # stagger (if any) widens THIS X, not the bare port lead-in point.
        drop_x = next(
            (
                a[0]
                for a, b in zip(rp.points, rp.points[1:])
                if abs(a[0] - b[0]) < 0.1 and abs(a[1] - b[1]) > 0.1
            ),
            None,
        )
        if drop_x is None:
            continue
        by_key.setdefault((rp.edge.source, rp.edge.target), []).append(drop_x)

    checked = False
    for (src, tgt), xs in by_key.items():
        if len(xs) < 2:
            continue
        checked = True
        xs = sorted(xs)
        gaps = [round(b - a, 1) for a, b in zip(xs, xs[1:])]
        assert all(abs(g - OFFSET_STEP) < 0.1 for g in gaps), (
            f"{fixture}: {src}->{tgt} drop Xs {xs} not one OFFSET_STEP apart "
            f"(gaps {gaps})"
        )
    assert checked, f"{fixture}: no multi-line perpendicular-port drop found"


_MIDBAND_BUNDLE_FIXTURES = [
    "longread_variant_calling.mmd",
]

_OPPOSITE_BAND_FIXTURES = [
    "longread_variant_calling.mmd",
    "topologies/convergence_stacked_sink.mmd",
]


@pytest.mark.parametrize("fixture", _MIDBAND_BUNDLE_FIXTURES)
def test_inter_row_trunks_bundle_tightly(fixture):
    """Same-direction trunks co-travelling through one inter-row gap form a
    tight bundle; opposite-direction flows sit on separate, clear bands.

    Several bypass routes dipping into the same inter-row channel used to land
    at a loose smear of distinct Ys (issue #484).  ``_normalize_bypass_trunks``
    now splits the channel by traversal direction (``sign_x``) and lays each
    direction on its own band: SAME-direction co-travellers fan tight
    (``OFFSET_STEP``), while OPPOSITE-direction flows are pushed onto separate
    bands with a clear ``BUNDLE_TO_BUNDLE_CLEARANCE`` gap so they never smoosh
    together (and no distinct line is hidden behind another).  Opposite
    directions are NOT counted as bundle-mates.
    """
    from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN, OFFSET_STEP
    from nf_metro.layout.routing.core import (
        _build_routing_context,
        _collect_htrunks,
        _inter_row_gap_band,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)

    trunks = _collect_htrunks(routes, include_exempt=True)

    # SAME-direction co-travellers this pass bundles (the checked trunk is
    # non-exempt, i.e. movable by the normaliser) must be no more than one
    # slot apart.  A larger gap means a line was left stranded outside the
    # bundle (the loose smear the fix removed).
    budget = OFFSET_STEP + 1.5
    tight_checked = False
    for i, t in enumerate(trunks):
        if t.route.normalize_exempt:
            continue
        gap = _inter_row_gap_band(ctx, t.y)
        if gap is None:
            continue
        nearest = None
        for j, o in enumerate(trunks):
            if i == j or o.route.line_id == t.route.line_id or o.sign_x != t.sign_x:
                continue
            if t.dips_down != o.dips_down or _inter_row_gap_band(ctx, o.y) != gap:
                continue
            if not (t.x_lo < o.x_hi and o.x_lo < t.x_hi):
                continue
            if min(t.x_hi, o.x_hi) - max(t.x_lo, o.x_lo) <= EDGE_TO_BUNDLE_CLEARANCE:
                continue
            d = abs(o.y - t.y)
            nearest = d if nearest is None else min(nearest, d)
        if nearest is None:
            continue
        tight_checked = True
        assert nearest <= budget, (
            f"{fixture}: '{t.route.line_id}' trunk @{t.y:.1f} stranded "
            f"{nearest:.1f}px from its nearest same-direction bundle-mate "
            f"(budget {budget:.1f}px)"
        )
    assert tight_checked, f"{fixture}: no same-direction co-travelling trunks found"


@pytest.mark.parametrize("fixture", _OPPOSITE_BAND_FIXTURES)
def test_opposite_direction_trunks_on_separate_bands(fixture):
    """Opposite-direction flows sharing one inter-row gap must sit on separate,
    clear bands - never smooshed into one tight bundle (issue #484).

    ``_normalize_bypass_trunks`` splits a shared inter-row channel by traversal
    direction (``sign_x``) and lays each direction on its own band with a clear
    ``BUNDLE_TO_BUNDLE_CLEARANCE`` gap.  Two overlapping trunks of OPPOSITE
    direction must therefore never fan to within one ``OFFSET_STEP`` of each
    other; they stay at least a bundle gap apart.
    """
    from nf_metro.layout.constants import (
        BUNDLE_TO_BUNDLE_CLEARANCE,
        CURVE_RADIUS,
        DIAGONAL_RUN,
    )
    from nf_metro.layout.routing.core import (
        _build_routing_context,
        _collect_htrunks,
        _inter_row_gap_band,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)

    trunks = _collect_htrunks(routes, include_exempt=True)
    sep_checked = False
    for i, t in enumerate(trunks):
        gap = _inter_row_gap_band(ctx, t.y)
        if gap is None:
            continue
        for j, o in enumerate(trunks):
            if j <= i or o.sign_x == t.sign_x:
                continue
            if t.dips_down != o.dips_down or _inter_row_gap_band(ctx, o.y) != gap:
                continue
            if not (t.x_lo < o.x_hi and o.x_lo < t.x_hi):
                continue
            if min(t.x_hi, o.x_hi) - max(t.x_lo, o.x_lo) <= EDGE_TO_BUNDLE_CLEARANCE:
                continue
            sep_checked = True
            d = abs(o.y - t.y)
            assert d >= BUNDLE_TO_BUNDLE_CLEARANCE - 0.1, (
                f"{fixture}: opposite-direction trunks '{t.route.line_id}' "
                f"@{t.y:.1f} (sign {t.sign_x}) and '{o.route.line_id}' "
                f"@{o.y:.1f} (sign {o.sign_x}) only {d:.1f}px apart - smooshed"
            )
    assert sep_checked, f"{fixture}: no opposite-direction overlapping trunks found"


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_inter_row_trunk_bands_crossing_optimal(fixture):
    """Sibling corridors sharing one inter-row gap stack in the Y order that
    minimises crossings between their peel-off risers and trunk legs.

    Two corridors dipping into one gap (e.g. from different junction fans) are
    ordered by ``_plan_trunk_band``.  When the realized order lets one slot's
    risers needlessly cross another slot's leg, swapping the two strictly
    reduces crossings; the planner must pick the crossing-minimal order so no
    such avoidable swap remains in the routed output.
    """
    from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN
    from nf_metro.layout.routing.core import (
        _build_routing_context,
        _suboptimal_trunk_bands,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)

    bad = _suboptimal_trunk_bands(routes, ctx)
    assert not bad, (
        f"{fixture}: inter-row trunk band(s) ordered with avoidable crossings "
        + ", ".join(f"y={y:.1f} ({cur}->{best} crossings)" for y, cur, best in bad)
    )


@pytest.mark.parametrize(
    "spans, expected",
    [
        # Two distinct trunks fully sharing a corridor -> one concentric stack.
        ([(0, 100), (0, 100)], [0, 1]),
        # Disjoint left/right pair bridged by a long trunk: the bridge packs
        # adjacent to the left trunk so they bundle tight under the left
        # corridor; the right pair reuses the freed shallow track.
        ([(0, 100), (0, 300), (250, 400), (240, 410)], [0, 1, 2, 3]),
        # A pair that never shares any X reuses track 0 -- no reserved gap.
        ([(0, 100), (200, 300)], [0, 0]),
    ],
)
def test_pack_band_tracks_no_reserved_gaps(spans, expected):
    """Greedy track packing never wedges an empty track between two trunks
    that share a sub-corridor.

    Each trunk takes the shallowest track one deeper than every shallower
    trunk it overlaps in X, so co-travelling trunks land on adjacent tracks
    and a trunk absent from a sub-corridor frees its track for reuse there.
    """
    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.layout.routing.normalize import (
        _HTrunk,
        _pack_band_tracks,
        _slot_span,
    )

    order = [
        [
            _HTrunk(
                route=RoutedPath(edge=None, line_id=f"l{i}", points=[]),
                idx=1,
                y=0.0,
                x_lo=float(lo),
                x_hi=float(hi),
                dips_down=True,
                sign_x=1,
            )
        ]
        for i, (lo, hi) in enumerate(spans)
    ]
    span_of = {id(sg): _slot_span(sg) for sg in order}
    assert _pack_band_tracks(order, span_of) == expected


def test_disjoint_sameline_trunks_bundle_tight():
    """Two distinct lines that dive into one below-row channel together ride a
    tight bundle until a member peels off at its turn column (issue #702).

    In ``disjoint_sameline_trunks`` lines ``a`` and ``c`` both bypass the QC
    section in the channel below the single section row; ``a`` peels up into
    the Align section while ``c`` continues to Call.  Their shared left-hand
    trunk must sit one ``OFFSET_STEP`` apart (one concentric bundle), not be
    split by tracks reserved for trunks that only appear further right.
    """
    from nf_metro.layout.constants import OFFSET_STEP
    from nf_metro.layout.routing.normalize import _collect_htrunks

    graph = _layout("topologies/disjoint_sameline_trunks.mmd")
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    trunks = _collect_htrunks(routes, include_exempt=True)

    a_left = min((t for t in trunks if t.route.line_id == "a"), key=lambda t: t.x_lo)
    c_trunk = next(t for t in trunks if t.route.line_id == "c")

    assert a_left.x_lo < c_trunk.x_hi and c_trunk.x_lo < a_left.x_hi, (
        "fixture precondition: a's left trunk and c's trunk share a corridor"
    )
    assert abs(a_left.y - c_trunk.y) <= OFFSET_STEP + 0.1, (
        f"a's left trunk (y={a_left.y:.1f}) and c's trunk (y={c_trunk.y:.1f}) "
        f"should bundle one OFFSET_STEP apart, not "
        f"{abs(a_left.y - c_trunk.y):.1f}px"
    )


def test_peeloff_riser_keeps_bundle_order():
    """A bypass-trunk bundle peeling up into a shared entry port enters it
    concentrically and keeps that order through the consumer section (#695).

    In ``peeloff_riser_respace`` four lines from two sources ride one shared
    bypass trunk below the destination row and rise into its left entry port.
    The trunk stacking puts the nearer source's lines on top, but the riser
    peel order, the port-slot offsets, and the consumer section's internal
    order are assigned in line-declaration order by independent passes, so the
    lines on the bottom of the trunk rose on the near side and cut across the
    others.  The settled route must order the riser peel-x, the port slots, and
    the section's internal bundle by trunk depth, so:

    1. no two lines make an avoidable crossing, and
    2. the bundle's top-to-bottom order at the port entry matches its order on
       the internal ``d1 -> d2`` run - a reorder between them would be a
       crossing the validator's hub-exclusion hides just inside the boundary.
    """
    from layout_validator import check_route_segment_crossings

    fixture = "topologies/peeloff_riser_respace.mmd"
    graph = _layout(fixture, validate=True)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    crossings = check_route_segment_crossings(graph, (offsets, routes))
    assert not crossings, "; ".join(v.message for v in crossings)

    def order_by_y(selected, at_target):
        rows = [
            (rp.line_id, apply_route_offsets(rp, offsets)[-1 if at_target else 0][1])
            for rp in selected
        ]
        return [lid for lid, _ in sorted(rows, key=lambda r: r[1])]

    port_order = order_by_y(
        [rp for rp in routes if rp.edge.target == "dst__entry_left_2"], at_target=True
    )
    internal_order = order_by_y(
        [rp for rp in routes if rp.edge.source == "d1" and rp.edge.target == "d2"],
        at_target=False,
    )
    assert port_order == internal_order, (
        f"bundle reorders entering the section: port {port_order} "
        f"vs internal {internal_order}"
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_top_entry_lead_corner_concentric(fixture):
    """A multi-line TOP-entry L-shape must turn its lead-in corner
    concentrically: co-routed lines share one bend centre.

    The lines are already separated in Y by the render offset, so an equal
    lead-in radius leaves the arcs non-concentric and the perpendicular gap
    pinches through the bend (issue #484, cross_row turn-down after section 1).
    The fix keeps the outermost line at the base radius and shrinks each inner
    line's lead-in radius by one ``OFFSET_STEP`` so all arcs nest about a
    common centre.  Encoded as: within a shared (source TOP-entry, target)
    bundle, the first-corner radii sorted descending must step down by exactly
    ``OFFSET_STEP``.  Only checks narrow bundles where the fix applies.
    """
    from nf_metro.layout.constants import CURVE_RADIUS, OFFSET_STEP

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    by_key: dict[tuple[str, str], list[float]] = {}
    for rp in routes:
        port = graph.ports.get(rp.edge.target)
        if port is None or not port.is_entry or port.side != PortSide.TOP:
            continue
        if not rp.normalize_exempt or not rp.curve_radii:
            continue
        by_key.setdefault((rp.edge.source, rp.edge.target), []).append(
            rp.curve_radii[0]
        )

    for (src, tgt), radii in by_key.items():
        n = len(radii)
        # Gate matches the handler: only narrow bundles get concentric radii.
        if n < 2 or (n - 1) * OFFSET_STEP > CURVE_RADIUS - OFFSET_STEP:
            continue
        radii = sorted(radii, reverse=True)
        assert abs(radii[0] - CURVE_RADIUS) < 0.1, (
            f"{fixture}: {src}->{tgt} outermost lead radius {radii[0]} "
            f"!= base {CURVE_RADIUS}"
        )
        steps = [round(a - b, 1) for a, b in zip(radii, radii[1:])]
        assert all(abs(s - OFFSET_STEP) < 0.1 for s in steps), (
            f"{fixture}: {src}->{tgt} lead-corner radii {radii} not concentric "
            f"(steps {steps})"
        )


# ---------------------------------------------------------------------------
# Off-track inputs sit above their consumer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK_INPUT)
def test_off_track_inputs_above_consumer(fixture):
    """Off-track input stations (declared via ``%%metro off_track:``)
    must sit at least one ``y_spacing`` slot above their on-track
    consumer.  Catches the regression where ``_lift_off_track_stations``
    leaves an off-track input on the same Y as its consumer (or below).
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    consumer_of = _off_track_input_consumer_map(graph, junction_ids)

    assert consumer_of, f"{fixture}: no off-track edges found"

    for off_id, consumer_id in consumer_of.items():
        off_st = graph.stations[off_id]
        cons_st = graph.stations[consumer_id]
        assert off_st.y < cons_st.y - _Y_TOL, (
            f"Off-track {off_id} y={off_st.y} not above consumer "
            f"{consumer_id} y={cons_st.y}"
        )


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_LINEAR_OFF_TRACK_CONSUMER)
def test_off_track_consumer_on_section_trunk(fixture):
    """An off-track input's consumer that continues straight into the
    section trunk must share that successor's Y.

    When several lines enter a section through an entry port and converge
    on one deep first station (the off-track-input consumer), that station
    is the head of the section trunk. It must sit level with its on-track
    continuation, not be dragged to the section floor -- otherwise the
    onward edge climbs near-vertically and the multi-line bundle merges
    into a single stroke (issue #650).

    Restricted to consumers with exactly one on-track in-section successor
    (a linear trunk continuation): a genuine on-track fork legitimately
    places its branches off the entry station's row.
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    consumers = _off_track_consumer_ids(graph, junction_ids)
    checked = 0
    for cons_id in consumers:
        succs = _in_section_on_track_successors(graph, cons_id, junction_ids)
        if len(succs) != 1:
            continue
        checked += 1
        cons_st = graph.stations[cons_id]
        succ_st = graph.stations[succs[0]]
        assert abs(cons_st.y - succ_st.y) <= _Y_TOL, (
            f"{fixture}: off-track consumer {cons_id} y={cons_st.y} dragged "
            f"off the section trunk; its continuation {succs[0]} sits at "
            f"y={succ_st.y} ({abs(cons_st.y - succ_st.y):.0f}px climb)"
        )
    assert checked, f"{fixture}: no linear off-track consumer to check"


def _single_trunk_off_track_input_lifts(graph: MetroGraph):
    """Yield ``(off_id, consumer_id, gap, n, step)`` for each off-track input
    whose consumer lives in a single-trunk section.

    ``n`` is the number of off-track stations sharing the input's column *and*
    its anchor (its expected stack depth); ``gap`` is its lift above the
    consumer; ``step`` is the section's off-track lift pitch.

    Restricted to single-trunk LR/RL sections (one distinct on-track Y): those
    carry no stacked horizontal line bands, so an off-track input cannot be
    legitimately bumped past its natural slot to clear a foreign feed-line (the
    multi-track ``differentialabundance`` ``functional`` bump).  On a single
    trunk the lift must therefore equal the same-column stack rank exactly.
    """
    junction_ids = set(graph.junctions)
    y_spacing = compute_min_y_spacing(graph)
    anchor_of = _off_track_anchor_of(graph)
    inputs = {
        off_id: anc
        for off_id, anc in anchor_of.items()
        if any(e.target == anc for e in graph.edges_from(off_id))
    }
    col_group: dict[tuple[str | None, float, str], int] = defaultdict(int)
    for off_id, anc in anchor_of.items():
        st = graph.stations[off_id]
        col_group[(st.section_id, round(st.x, 1), anc)] += 1

    for off_id, anc in inputs.items():
        off_st = graph.stations[off_id]
        cons_st = graph.stations[anc]
        section = graph.sections.get(off_st.section_id)
        if section is None or not _is_single_trunk_lr_section(
            graph, section, junction_ids
        ):
            continue
        step = _off_track_lift_step(graph, section, junction_ids, y_spacing)
        n = col_group[(off_st.section_id, round(off_st.x, 1), anc)]
        yield off_id, anc, cons_st.y - off_st.y, n, step


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK_INPUT)
def test_off_track_input_lift_matches_column_stack_depth(fixture):
    """On a single-trunk section, an off-track input hugs its consumer by
    exactly its same-column stack depth, not the whole anchor group's size.

    When one consumer is fed by off-track stations in *different* columns (e.g.
    an input above it and a producer-fed output beside it), the lift step must
    be counted per column.  Counting the whole anchor group instead pushes a
    lone-in-its-column input an extra slot up, stranding it over an empty row
    above an earlier trunk station (issue #651).
    """
    graph = _layout(fixture)
    checked = 0
    for off_id, cons_id, gap, n, step in _single_trunk_off_track_input_lifts(graph):
        checked += 1
        assert gap <= n * step + _Y_TOL, (
            f"{fixture}: off-track input {off_id} lifted {gap:.0f}px above "
            f"consumer {cons_id} ({gap / step:.1f} slots) but only {n} off-track "
            f"station(s) share its column and anchor; expected at most "
            f"{n * step:.0f}px - it is stranded above an empty row"
        )
    if not checked:
        pytest.skip(f"{fixture}: no single-trunk off-track input to check")


def test_off_track_input_column_stack_guard_catches_over_lift():
    """The runtime guard fires when a lone-in-its-column input is over-lifted.

    Locks the guard's teeth: dragging an off-track input one extra slot above
    its consumer on a single-trunk section, where nothing shares its column,
    must make ``_guard_off_track_input_column_stack`` raise rather than pass.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_off_track_input_column_stack,
    )

    graph = _layout("topologies/off_track_input_above_consumer.mmd")
    _guard_off_track_input_column_stack(graph, "test")
    y_spacing = compute_min_y_spacing(graph)
    graph.stations["cpg_bed"].y -= y_spacing

    with pytest.raises(PhaseInvariantError, match="stranded above an empty row"):
        _guard_off_track_input_column_stack(graph, "test")


# ---------------------------------------------------------------------------
# Off-track outputs sit above and adjacent to their producer (#573)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_ABOVE_OUTPUT)
def test_off_track_outputs_above_and_adjacent_to_producer(fixture):
    """Off-track *output* stations (producer-fed sinks, declared via
    ``%%metro off_track:``) must hang clear of the trunk and adjacent to
    their producer: above it (smaller Y) and lifted by only a bounded
    number of ``y_spacing`` slots.

    A correctly-anchored sink sits one pitch above its producer (more if a
    crowded column bumps it clear of a trunk band), so a gap above two
    pitches means it was misanchored to the section's topmost on-track
    station and stranded far from the step that writes it (issue #573).
    Mirror of ``test_off_track_inputs_above_consumer``.
    """
    graph = _layout(fixture)
    y_spacing = compute_min_y_spacing(graph)
    producer_of = _off_track_output_sinks(graph)
    below = _off_track_output_below(graph)

    assert producer_of, f"{fixture}: no off-track output sinks found"

    above_producers = {
        off_id: prod_id
        for off_id, prod_id in producer_of.items()
        if off_id not in below
    }

    for off_id, prod_id in above_producers.items():
        off_st = graph.stations[off_id]
        prod_st = graph.stations[prod_id]
        gap = prod_st.y - off_st.y
        assert gap > _Y_TOL, (
            f"{fixture}: off-track output {off_id} y={off_st.y} not above "
            f"producer {prod_id} y={prod_st.y}"
        )
        assert gap <= 2 * y_spacing + _Y_TOL, (
            f"{fixture}: off-track output {off_id} lifted {gap:.0f}px above "
            f"producer {prod_id} (more than 2 slots) - likely misanchored to "
            f"the section's topmost station instead of its producer"
        )


def test_single_trunk_off_track_step_not_inflated_by_diagonal_band():
    """On a single-trunk diagonal-label section, an off-track output hangs
    one plain off-track step above its producer, not the diagonal-inflated
    pitch (issue #580).

    ``label_angle`` makes the spread loop widen the graph-wide ``y_spacing``
    so hanging angled labels clear the row below.  A section that is a single
    horizontal trunk has no parallel tracks, so that widened pitch is wasted
    vertical room when used as the off-track lift step: it detaches the icon
    far above the trunk.  The lift step for such a section must stay at the
    base content pitch (``compute_min_y_spacing``).
    """
    fixture = "diagonal_single_trunk_off_track.mmd"
    graph = _layout(fixture)
    base_step = compute_min_y_spacing(graph)
    producer_of = _off_track_output_sinks(graph)
    assert producer_of, f"{fixture}: no off-track output sinks found"

    junction_ids = set(graph.junctions)
    for off_id, prod_id in producer_of.items():
        off_st = graph.stations[off_id]
        prod_st = graph.stations[prod_id]
        section = graph.sections[off_st.section_id]
        distinct_trunk_ys = _section_distinct_trunk_ys(graph, section, junction_ids)
        assert len(distinct_trunk_ys) == 1, (
            f"{fixture}: section {section.id} is not single-trunk "
            f"(trunk Ys {sorted(distinct_trunk_ys)})"
        )
        gap = prod_st.y - off_st.y
        assert gap == pytest.approx(base_step, abs=_Y_TOL), (
            f"{fixture}: off-track output {off_id} sits {gap:.0f}px above its "
            f"producer {prod_id} on a single-trunk section, inflated past the "
            f"base off-track step of {base_step:.0f}px by the diagonal-label band"
        )


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK_OUTPUT)
def test_off_track_output_icon_clears_on_track_markers(fixture):
    """An off-track output's icon must not overlap an on-track station's marker.

    An output hangs in a row above or below the trunk, so its icon may share a
    column's X with an on-track station as long as the two sit in different rows
    (their marker boxes don't overlap).  Assert no on-track same-section marker
    overlaps the icon box in both axes.
    """
    theme = THEMES["nfcore"]
    icon_half_w = theme.terminus_width / 2
    icon_half_h = theme.terminus_height / 2
    r = theme.station_radius

    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    producer_of = _off_track_output_sinks(graph)
    assert producer_of, f"{fixture}: no off-track output sinks found"

    for off_id in producer_of:
        off_st = graph.stations[off_id]
        for sid in graph.sections[off_st.section_id].station_ids:
            st = graph.stations.get(sid)
            if (
                st is None
                or st.off_track
                or st.is_port
                or st.is_hidden
                or sid in junction_ids
            ):
                continue
            x_overlap = abs(off_st.x - st.x) < icon_half_w + r
            y_overlap = abs(off_st.y - st.y) < icon_half_h + r
            assert not (x_overlap and y_overlap), (
                f"{fixture}: off-track output {off_id} icon "
                f"({off_st.x:.1f},{off_st.y:.1f}) overlaps on-track marker "
                f"{sid} ({st.x:.1f},{st.y:.1f})"
            )


def test_off_track_placeholder_does_not_displace_on_track_rows():
    """On-track placement is independent of off-track output placeholders.

    Off-track stations carry a placeholder Y until the Stage 5.2 lift; that
    placeholder must not occupy an on-track cell and cascade an on-track
    sibling onto a phantom row.  The lower arm of the
    ``bwa -> {convert, markdup} -> bqsr`` diamond therefore sits exactly one
    grid row below the trunk.
    """
    graph = _layout("off_track_outputs.mmd")
    y_spacing = compute_min_y_spacing(graph)
    trunk_y = graph.stations["convert"].y
    arm_drop = graph.stations["markdup"].y - trunk_y
    assert arm_drop == pytest.approx(y_spacing, abs=_Y_TOL), (
        f"diamond lower arm sits {arm_drop:.0f}px below the trunk; expected "
        f"one {y_spacing:.0f}px row (an off-track placeholder cascaded it onto "
        f"a phantom extra row)"
    )


# ---------------------------------------------------------------------------
# Off-track outputs on a downward branch drop below their producer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_DOWNWARD_OUTPUT)
def test_off_track_outputs_below_downward_branch_producer(fixture):
    """An off-track output whose producer sits on a downward branch must
    drop *below* its producer and stay below the section's top trunk row,
    so it runs straight down instead of crossing back over the trunk.

    The output must sit below the producer (larger Y) and below the section's
    topmost on-track station (the trunk row), and stay adjacent (within a
    bounded number of ``y_spacing`` slots) so it isn't stranded.
    """
    graph = _layout(fixture)
    y_spacing = compute_min_y_spacing(graph)
    junction_ids = set(graph.junctions)
    producer_of = _off_track_output_sinks(graph)
    below = _off_track_output_below(graph)
    downward = {o: p for o, p in producer_of.items() if o in below}

    assert downward, f"{fixture}: no downward off-track outputs found"

    for off_id, prod_id in downward.items():
        off_st = graph.stations[off_id]
        prod_st = graph.stations[prod_id]
        gap = off_st.y - prod_st.y
        assert gap > _Y_TOL, (
            f"{fixture}: downward off-track output {off_id} y={off_st.y} "
            f"not below producer {prod_id} y={prod_st.y}"
        )
        assert gap <= 2 * y_spacing + _Y_TOL, (
            f"{fixture}: downward off-track output {off_id} dropped {gap:.0f}px "
            f"below producer {prod_id} (more than 2 slots) - stranded from "
            f"the step that writes it"
        )

        section = graph.sections[off_st.section_id]
        trunk_ys = [
            graph.stations[sid].y
            for sid in section.station_ids
            if (s := graph.stations.get(sid)) is not None
            and not s.off_track
            and not s.is_port
            and not s.is_hidden
            and sid not in junction_ids
        ]
        top_trunk_y = min(trunk_ys)
        assert off_st.y > top_trunk_y + _Y_TOL, (
            f"{fixture}: downward off-track output {off_id} y={off_st.y} "
            f"did not clear the section's top trunk row y={top_trunk_y}; it "
            f"crosses back over the trunk"
        )


# No known fixture leaves a line striking through a station's name label: fan
# and convergence diagonals are cleared by the column-runway loop and a foreign
# bypass V's climb is seated clear of the bypassed station's label by the router
# (``_clear_bypass_v_label_strikes``).  New strikes are caught both here and by
# the wired ``_guard_no_line_strikes_label`` validate guard.
_LABEL_STRIKE_DIAGONAL_XFAIL: dict[str, str] = {}

_LABEL_STRIKE_FIXTURES = _params_with_xfails(ALL_FIXTURES, _LABEL_STRIKE_DIAGONAL_XFAIL)


def _label_glyph_strikes(fixture: str) -> list[tuple[str, str]]:
    """Return ``(station_id, line_id)`` pairs where a foreign line strikes a label.

    Mirrors the renderer: ``route_edges`` mutates Station.x via diagonal
    centring and the renderer places labels on that mutated geometry, so the
    label glyph-ink boxes are built without restoring X.  A pair is reported
    when a segment of a line the station does not carry (and which is not an
    endpoint of the segment's edge) crosses the label's glyph-ink box.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    theme = THEMES["nfcore"]
    icon_obstacles = _compute_icon_obstacles(graph, theme, offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    strikes: list[tuple[str, str]] = []
    for p in placements:
        station = graph.stations.get(p.station_id)
        if station is None or not station.label.strip():
            continue
        carried = set(graph.station_lines(p.station_id))
        for r in routes:
            if r.edge.source == p.station_id or r.edge.target == p.station_id:
                continue
            if r.line_id in carried:
                continue
            pts = apply_route_offsets(r, offsets)
            if any(
                segment_strikes_label(x1, y1, x2, y2, p)
                for (x1, y1), (x2, y2) in zip(pts, pts[1:])
            ):
                strikes.append((p.station_id, r.line_id))
                break
    return strikes


@pytest.mark.parametrize("fixture", _LABEL_STRIKE_FIXTURES)
def test_no_line_strikes_through_label(fixture):
    """No foreign line crosses a station label's glyph ink.

    A line a station does not carry dipping, fanning, or running across that
    station's name label reads as a strike-through.  The check models the label
    by its glyph ink rather than the full reserved box, so a line clipping only
    the empty reserved margin (an acceptable graze) is not flagged.
    """
    strikes = _label_glyph_strikes(fixture)
    assert not strikes, (
        f"{fixture}: foreign lines strike label glyph ink: "
        + ", ".join(f"{lid!r} over {sid!r}" for sid, lid in strikes)
    )


def _bypass_v_own_label_strikes(fixture: str) -> list[str]:
    """Return ids of stations whose own bypass-V diagonal rakes their label.

    A bypass V diverges from the trunk at the station before the bypassed one
    and re-merges at the station after it.  When the section is too tight for a
    horizontal lead-in, the divergence (or merge) pins to the marker and the
    descending diagonal cuts through that station's name label, which sits
    directly under the marker.  This is the divergence station's *own* line over
    its *own* label, so it is exempt from the foreign-strike checks; it is only
    cured by placing the label on the side clear of the V.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    by_station = {p.station_id: p for p in placements if p.station_id}
    struck: list[str] = []
    for r in routes:
        src_bypass = r.edge.source.startswith("__bypass_")
        tgt_bypass = r.edge.target.startswith("__bypass_")
        if src_bypass == tgt_bypass:
            continue
        endpoint = r.edge.target if src_bypass else r.edge.source
        p = by_station.get(endpoint)
        station = graph.stations.get(endpoint)
        if p is None or p.angle or station is None or not station.label.strip():
            continue
        pts = apply_route_offsets(r, offsets)
        if any(
            segment_strikes_label(x1, y1, x2, y2, p)
            and abs(y2 - y1) >= max(abs(x2 - x1), 1.0) * DIAGONAL_SLOPE_RATIO
            for (x1, y1), (x2, y2) in zip(pts, pts[1:])
        ):
            struck.append(endpoint)
    return struck


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_bypass_v_does_not_strike_diverging_station_label(fixture):
    """A bypass V must not rake the label of the station it diverges from.

    The divergence point may sit on a station's marker only when the label is
    clear of the V's side; otherwise the descending diagonal strikes the name.
    """
    struck = _bypass_v_own_label_strikes(fixture)
    assert not struck, (
        f"{fixture}: bypass-V diagonal rakes its own diverging/merging station "
        "label: " + ", ".join(sorted(struck))
    )


def test_bypass_v_flat_guard_catches_a_collapse():
    """The runtime guard fires when a bypass V's flat run is left collapsed.

    The strike-clearance loop seats ``bypass_v_tight``'s V on a full flat by
    pushing the bypassed node and the merge target each a grid column out, so
    the settled layout passes the guard.  Pulling the exit port back toward the
    bypassed node reinstates the tight merge run, and the guard -- the backstop
    behind the loop -- must raise on the collapsed flat.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_bypass_v_flat_visible,
    )

    graph = _layout("topologies/bypass_v_tight.mmd", _cache=False)
    _guard_bypass_v_flat_visible(graph, "test")

    graph.stations["mid__exit_right_1"].x -= X_SPACING
    with pytest.raises(PhaseInvariantError, match="bypass-V flat"):
        _guard_bypass_v_flat_visible(graph, "test")


# A label wider than its station's flat run pushes a fan-in/fan-out,
# convergence, or station-to-station descent diagonal inside the label's
# x-extent, raking its glyphs.  At a tight column pitch the proportional flat
# run shrinks below the (fixed-width) label and the strike appears; the engine
# clears it by lengthening the run with whole grid columns (section runway for a
# port fan, a per-column gap for an intra-section descent) at a fixed pitch.
# Each case strikes at the pinned pitch on a layout without that growth.
# ``None`` exercises the default (gallery) pitch, where these fixtures ship and
# the strike-clearance loop must keep them clear; the pinned tighter pitches
# stress the levers where the proportional flat run is shortest.
_TIGHT_PITCH_STRIKE_CASES = [
    ("topologies/funcprofiler_upstream.mmd", None),
    ("topologies/funcprofiler_upstream.mmd", 50),
    ("topologies/funcprofiler_upstream.mmd", 45),
    ("topologies/variant_calling.mmd", None),
    ("topologies/variant_calling.mmd", 45),
    ("rnaseq_sections.mmd", None),
    ("topologies/wide_label_fan.mmd", 45),
]


@pytest.mark.parametrize("fixture,x_spacing", _TIGHT_PITCH_STRIKE_CASES)
def test_no_diagonal_strikes_label(fixture, x_spacing):
    """No diagonal may rake a station's name label.

    Uses the engine's own strike definition (the one the clearance loop and the
    runtime guard share): a fan or descent diagonal -- of any line, the station's
    own or foreign -- crossing the drawn glyphs, with flat runs, bypass-V
    crossings, off-track sweeps, and angled labels excluded.  The loop lengthens
    the flat run with whole grid columns until the transition clears.
    """
    from nf_metro.layout.phases.spacing import _struck_stations_and_collinear

    graph = (
        _layout(fixture) if x_spacing is None else _layout(fixture, x_spacing=x_spacing)
    )
    struck, collinear = _struck_stations_and_collinear(graph)
    assert not struck, (
        f"{fixture} @ x_spacing={x_spacing}: diagonals rake label glyph ink: "
        + ", ".join(sorted(graph.stations[s].label for s in struck))
    )
    assert not collinear, f"{fixture} @ x_spacing={x_spacing}: collinear overlay"


def test_label_strike_guard_catches_a_strike():
    """The runtime guard fires on a genuine glyph strike and clears a graze.

    Locks the guard's teeth and its glyph-ink discrimination.  ``guide/06a``'s
    bypass V rakes a wide label's glyphs on both sides until the router seats
    the V clear (``_clear_bypass_v_label_strikes``); emptying the obstacle map
    the router reads reinstates that strike, so the guard -- the backstop behind
    the router -- must raise on it.  A fixture where a line only clips a label's
    empty reserved margin must pass.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_no_line_strikes_label,
    )

    struck = _layout("guide/06a_without_hidden.mmd")
    struck.bypass_label_obstacles = {}
    with pytest.raises(PhaseInvariantError, match="strikes through label"):
        _guard_no_line_strikes_label(struck, "test")

    grazed = _layout("variantbenchmarking.mmd")
    _guard_no_line_strikes_label(grazed, "test")


def test_diagonal_strike_guard_teeth_and_exemptions():
    """The wired guard fires on a strike the clearance loop cannot relocate,
    stays silent once the loop has cleared one, and exempts the bypass-V rake.

    A section the strike-clearance loop grows (``funcprofiler_upstream`` at a
    tight pitch) must pass; a sectionless flat graph (``centered_tracks`` at a
    tight pitch), whose struck stations have no section runway or column gap to
    grow, must raise; and a bypass-V label rake (``guide/06a``) must pass, since
    the guard excludes it.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_no_diagonal_strikes_horizontal_label,
    )

    cleared = _layout("topologies/funcprofiler_upstream.mmd", x_spacing=45)
    _guard_no_diagonal_strikes_horizontal_label(cleared, "test")

    unclearable = _layout("centered_tracks.mmd", x_spacing=45)
    with pytest.raises(PhaseInvariantError, match="strikes horizontal label"):
        _guard_no_diagonal_strikes_horizontal_label(unclearable, "test")

    bypass = _layout("guide/06a_without_hidden.mmd")
    _guard_no_diagonal_strikes_horizontal_label(bypass, "test")


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_DOWNWARD_OUTPUT)
def test_downward_off_track_output_route_clears_producer_label(fixture):
    """A downward off-track output's route clears its producer's name label
    and keeps a normal-slope diagonal.

    The producer's label sits below it, on the side the output drops toward, so
    the descent turns down past the label's far edge.  With the output two
    columns out there is room for the diagonal to keep its standard run rather
    than steepen toward a near-vertical drop, so each diagonal segment's
    horizontal run stays comparable to its vertical drop.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    theme = THEMES["nfcore"]
    icon_obstacles = _compute_icon_obstacles(graph, theme, offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    bbox_of = {p.station_id: _label_bbox(p) for p in placements}

    producer_of = _off_track_output_sinks(graph)
    below = _off_track_output_below(graph)
    downward = {o: p for o, p in producer_of.items() if o in below}
    assert downward, f"{fixture}: no downward off-track outputs found"

    route_by_endpoints = {(r.edge.source, r.edge.target): r for r in routes}

    for off_id, prod_id in downward.items():
        label_bbox = bbox_of.get(prod_id)
        if label_bbox is None:
            continue
        route = route_by_endpoints.get((prod_id, off_id))
        assert route is not None, (
            f"{fixture}: no route for downward output {prod_id} -> {off_id}"
        )
        pts = route.points
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            assert not segment_intersects_bbox(x1, y1, x2, y2, label_bbox), (
                f"{fixture}: downward off-track output route "
                f"{prod_id} -> {off_id} segment ({x1:.1f},{y1:.1f})->"
                f"({x2:.1f},{y2:.1f}) crosses producer {prod_id} label "
                f"bbox {tuple(round(v, 1) for v in label_bbox)}"
            )
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if dx > _Y_TOL and dy > _Y_TOL:
                assert dx >= 0.5 * dy, (
                    f"{fixture}: downward off-track output route "
                    f"{prod_id} -> {off_id} diagonal segment "
                    f"({x1:.1f},{y1:.1f})->({x2:.1f},{y2:.1f}) is near-vertical "
                    f"(dx={dx:.1f}, dy={dy:.1f}); it should keep its slope"
                )


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK_OUTPUT)
def test_off_track_output_routes_share_flat_tail(fixture):
    """Every off-track output in a section hangs off its producer with the
    same flat tail after the diagonal.

    Each output route is a horizontal lead, a standard-slope diagonal, then a
    flat tail into the icon.  The tail length must be identical across a
    section's outputs so the icons read as a consistent set; a variable
    per-output column reservation makes the tail depend on each layer's
    width, stranding some icons far past their diagonal and pulling others
    almost onto it.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    route_by_endpoints = {(r.edge.source, r.edge.target): r for r in routes}

    producer_of = _off_track_output_sinks(graph)
    assert producer_of, f"{fixture}: no off-track output sinks found"

    tails_by_section: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for off_id, prod_id in producer_of.items():
        route = route_by_endpoints.get((prod_id, off_id))
        if route is None or len(route.points) < 2:
            continue
        (x1, _y1), (x2, _y2) = route.points[-2], route.points[-1]
        sec_id = graph.stations[off_id].section_id
        tails_by_section[sec_id].append((off_id, abs(x2 - x1)))

    # The icon offset is uniform, so the nominal tails match; the last segment
    # measured here is post corner-smoothing, which can shave up to a corner
    # radius off an output whose diagonal meets the tail near a tight turn.
    # Allow that, while still catching the old variable-column bug (tens of px).
    tail_tol = CURVE_RADIUS + _Y_TOL
    for sec_id, tails in tails_by_section.items():
        if len(tails) < 2:
            continue
        lengths = [t for _, t in tails]
        spread = max(lengths) - min(lengths)
        rounded = [(o, round(t, 1)) for o, t in tails]
        assert spread <= tail_tol, (
            f"{fixture}/{sec_id}: off-track output flat tails differ by "
            f"{spread:.1f}px (>{tail_tol}); tails {rounded} are not consistent"
        )


# ---------------------------------------------------------------------------
# A metro line must never pass through a file / terminus icon
# ---------------------------------------------------------------------------


def _segments_crossing_icons(
    graph: MetroGraph,
) -> list[tuple[str, str, str, str]]:
    """Return ``(line, src, tgt, icon_station)`` for every routed segment
    that crosses a file icon's drawn bbox other than the segment that
    starts or ends at that icon's own station.
    """
    from nf_metro.layout.geometry import segment_intersects_bbox

    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    boxes = _icon_obstacles_by_station(graph, THEMES["nfcore"], offsets)
    hits: list[tuple[str, str, str, str]] = []
    for r in routes:
        pts = apply_route_offsets(r, offsets)
        src, tgt = r.edge.source, r.edge.target
        for sid, bbox in boxes.items():
            if src == sid or tgt == sid:
                continue
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                if segment_intersects_bbox(p1[0], p1[1], p2[0], p2[1], bbox):
                    hits.append((r.line_id, src, tgt, sid))
                    break
    return hits


def test_leaf_file_icon_crossing_fixture_crosses_without_auto_lift(monkeypatch):
    """The problem-1 fixture genuinely puts a line across the icon when the
    auto-off-track corrective re-run is suppressed.

    Guards against the fixture silently ceasing to exercise the fix: with the
    crossing-sink detector neutralised, the unmarked leaf icon must still be
    raked by a non-terminating line.
    """
    import nf_metro.layout.engine as engine_module

    monkeypatch.setattr(
        engine_module, "_line_crossed_file_icon_sinks", lambda graph: set()
    )
    graph = _layout("leaf_file_icon_on_trunk.mmd", _cache=False)
    assert not graph.stations["bam_out"].off_track
    assert _segments_crossing_icons(graph), (
        "fixture no longer crosses its icon without auto-lift; it can't "
        "exercise the keep-lines-out-of-icons fix"
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_line_crosses_file_icon(fixture):
    """No routed line segment may pass through a file / terminus icon's drawn
    bbox, except the segment that terminates at (or originates from) that
    icon's own station.

    A leaf file-icon sink the layout would otherwise rake a line across is
    auto-lifted off the trunk, so the settled layout is clear by
    construction.
    """
    graph = _layout(fixture)
    hits = _segments_crossing_icons(graph)
    assert not hits, f"{fixture}: line(s) cross a file icon: " + "; ".join(
        f"{ln} on {s}->{t} crosses {icon}" for ln, s, t, icon in hits
    )


# ---------------------------------------------------------------------------
# Sibling off-track output icons must not overlap each other
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK)
def test_off_track_output_icons_do_not_overlap(fixture):
    """Two off-track output icon bboxes in one section never overlap.

    Outputs hanging off nearby producers stack so their drawn boxes -- icon
    plus any under-icon caption -- stay clear of each other.
    """
    graph = _layout(fixture)
    sinks = set(_off_track_output_sinks(graph))
    if len(sinks) < 2:
        pytest.skip(f"{fixture}: fewer than two off-track output sinks")
    offsets = compute_station_offsets(graph)
    boxes = {
        sid: b
        for sid, b in _icon_obstacles_by_station(
            graph, THEMES["nfcore"], offsets
        ).items()
        if sid in sinks
    }
    items = list(boxes.items())
    tol = 0.5
    for i, (s1, (x1, y1, X1, Y1)) in enumerate(items):
        for s2, (x2, y2, X2, Y2) in items[i + 1 :]:
            overlap = (
                x1 < X2 - tol and x2 < X1 - tol and y1 < Y2 - tol and y2 < Y1 - tol
            )
            assert not overlap, (
                f"{fixture}: off-track output icons {s1!r} and {s2!r} overlap: "
                f"({x1:.1f},{y1:.1f},{X1:.1f},{Y1:.1f}) vs "
                f"({x2:.1f},{y2:.1f},{X2:.1f},{Y2:.1f})"
            )


def test_captioned_sibling_outputs_clear_caption_band():
    """Same-column captioned off-track outputs reserve their caption band.

    Two captioned outputs stacked in one column must keep their full drawn
    boxes -- icon plus the under-icon caption that reaches below each icon --
    apart, not merely the bare icon half-heights.
    """
    graph = _layout("captioned_sibling_outputs.mmd")
    sinks = set(_off_track_output_sinks(graph))
    assert len(sinks) >= 2
    offsets = compute_station_offsets(graph)
    boxes = {
        sid: b
        for sid, b in _icon_obstacles_by_station(
            graph, THEMES["nfcore"], offsets
        ).items()
        if sid in sinks
    }
    items = list(boxes.items())
    for i, (s1, (x1, y1, X1, Y1)) in enumerate(items):
        for s2, (x2, y2, X2, Y2) in items[i + 1 :]:
            overlap = x1 < X2 and x2 < X1 and y1 < Y2 and y2 < Y1
            assert not overlap, (
                f"captioned outputs {s1!r}/{s2!r} overlap: "
                f"({x1:.1f},{y1:.1f},{X1:.1f},{Y1:.1f}) vs "
                f"({x2:.1f},{y2:.1f},{X2:.1f},{Y2:.1f})"
            )


# ---------------------------------------------------------------------------
# An off-track output's route must not cross a non-producer trunk marker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK_OUTPUT)
def test_off_track_output_route_clears_non_producer_markers(fixture):
    """An off-track output's S-curve must not cross a trunk station's marker.

    An output icon hangs in the gap just past its own producer.  When the
    output's horizontal extent overruns that gap the up-right diagonal rakes
    across the next on-track station's marker (and adjacent outputs bunch).
    The producer's trunk gap widens to fit the output, so the only marker an
    output route may touch is its own producer's.
    """
    theme = THEMES["nfcore"]
    r = theme.station_radius
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    producer_of = _off_track_output_sinks(graph)
    assert producer_of, f"{fixture}: no off-track output sinks found"

    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    route_by_endpoints = {(rt.edge.source, rt.edge.target): rt for rt in routes}

    for off_id, prod_id in producer_of.items():
        route = route_by_endpoints.get((prod_id, off_id))
        if route is None:
            continue
        pts = apply_route_offsets(route, offsets)
        sec_id = graph.stations[off_id].section_id
        for sid in graph.sections[sec_id].station_ids:
            if sid in (off_id, prod_id) or sid in junction_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.off_track or st.is_port or st.is_hidden:
                continue
            marker_box = (st.x - r, st.y - r, st.x + r, st.y + r)
            for k in range(len(pts) - 1):
                p1, p2 = pts[k], pts[k + 1]
                assert not segment_intersects_bbox(
                    p1[0], p1[1], p2[0], p2[1], marker_box
                ), (
                    f"{fixture}: off-track output {off_id!r} route (from "
                    f"producer {prod_id!r}) crosses non-producer marker {sid!r} "
                    f"at ({st.x:.1f},{st.y:.1f})"
                )


def test_off_track_output_route_guard_catches_a_crossing():
    """The runtime guard fires when an output's route rakes a non-producer marker.

    Locks the guard's teeth: an output dragged past the trunk stations that
    follow its producer has its producer-to-icon route cross their markers, and
    ``_guard_off_track_output_clears_non_producer`` must raise rather than pass.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_off_track_output_clears_non_producer,
    )

    graph = _layout("diagonal_single_trunk_off_track.mmd")
    # Drag the BAM output well past its producer so the route to its icon rakes
    # the intervening trunk markers (merge/markdup/bqsr ...).
    graph.stations["bam_out"].x = graph.stations["applybqsr"].x + 20

    with pytest.raises(PhaseInvariantError, match="non-producer marker"):
        _guard_off_track_output_clears_non_producer(graph, "test")


# ---------------------------------------------------------------------------
# A producer's label clears an off-track output icon above its fork/join bubble
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK)
def test_off_track_output_clears_station_labels(fixture):
    """No on-track station label overlaps an off-track output icon.

    A fork/join bubble carrying an output above it must leave its stations'
    labels clear of the output icon; the column gap widens to fit them.
    """
    graph = _layout(fixture)
    sinks = set(_off_track_output_sinks(graph))
    if not sinks:
        pytest.skip(f"{fixture}: no off-track output sinks")
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    icon_boxes = _icon_obstacles_by_station(graph, THEMES["nfcore"], offsets)
    labels = place_labels(
        graph,
        station_offsets=offsets,
        icon_obstacles=list(icon_boxes.values()),
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    sink_boxes = {sid: b for sid, b in icon_boxes.items() if sid in sinks}
    tol = 0.5
    for p in labels:
        if not p.station_id or p.station_id in sinks:
            continue
        lx0, ly0, lx1, ly1 = _label_bbox(p)
        for oid, (bx0, by0, bx1, by1) in sink_boxes.items():
            overlap = (
                lx0 < bx1 - tol
                and bx0 < lx1 - tol
                and ly0 < by1 - tol
                and by0 < ly1 - tol
            )
            assert not overlap, (
                f"{fixture}: label of {p.station_id!r} overlaps off-track "
                f"output icon {oid!r}: label "
                f"({lx0:.1f},{ly0:.1f},{lx1:.1f},{ly1:.1f}) vs icon "
                f"({bx0:.1f},{by0:.1f},{bx1:.1f},{by1:.1f})"
            )


# ---------------------------------------------------------------------------
# A fork/join bubble's station labels clear the convergence/divergence diagonal
# ---------------------------------------------------------------------------


_FIXTURES_WITH_BUBBLE_OUTPUT = _fixtures_with(
    lambda t: "off_track:" in t and "-->|" in t
)


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_BUBBLE_OUTPUT)
def test_bubble_label_clears_convergence_diagonal(fixture):
    """A fork/join bubble station's label must not overlap a sibling branch's
    convergence (or divergence) diagonal.

    A two-branch bubble (fork ``-> {a, b} ->`` join) carrying an off-track
    output above it can be squeezed so the on-trunk branch's diagonal label
    rakes the off-trunk branch's slanted convergence segment.  The bubble's
    column gap must hold enough room that each branch label clears the other
    branch's diagonal.
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)

    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    labels = place_labels(
        graph,
        station_offsets=offsets,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    label_box = {p.station_id: _label_bbox(p) for p in labels if p.station_id}

    def _diagonal_segments(route):
        pts = apply_route_offsets(route, offsets)
        segs = []
        for k in range(len(pts) - 1):
            (x1, y1), (x2, y2) = pts[k], pts[k + 1]
            if abs(x2 - x1) > 0.5 and abs(y2 - y1) > 0.5:
                segs.append((x1, y1, x2, y2))
        return segs

    tol = 0.5
    checked = False
    for section in graph.sections.values():
        sec_sids = set(section.station_ids)

        def _internal(eid):
            return eid in sec_sids and not graph.stations[eid].is_port

        for jid in section.station_ids:
            join = graph.stations.get(jid)
            if join is None or join.is_port or join.off_track or jid in junction_ids:
                continue
            preds = [
                e.source
                for e in graph.edges_to(jid)
                if _internal(e.source) and not graph.stations[e.source].off_track
            ]
            if len(preds) < 2:
                continue
            # A bubble: the converging branches sit on different tracks.
            branch_ys = {round(graph.stations[p].y, 1) for p in preds}
            if len(branch_ys) < 2:
                continue
            checked = True
            for branch_id in preds:
                route = next(
                    (
                        rt
                        for rt in routes
                        if rt.edge.source == branch_id and rt.edge.target == jid
                    ),
                    None,
                )
                if route is None:
                    continue
                for other_id in preds:
                    if other_id == branch_id:
                        continue
                    box = label_box.get(other_id)
                    if box is None:
                        continue
                    bx0, by0, bx1, by1 = box
                    label_rect = (bx0 - tol, by0 - tol, bx1 + tol, by1 + tol)
                    for x1, y1, x2, y2 in _diagonal_segments(route):
                        assert not segment_intersects_bbox(
                            x1, y1, x2, y2, label_rect
                        ), (
                            f"{fixture}: label of bubble station {other_id!r} "
                            f"overlaps the {branch_id!r}->{jid!r} convergence "
                            f"diagonal"
                        )
    if not checked:
        pytest.skip(f"{fixture}: no multi-branch bubble")


# ---------------------------------------------------------------------------
# Off-track reanchor: explicit precondition + order-independent bbox fit (#463)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK)
def test_reanchor_off_track_requires_snapped_consumers(fixture):
    """``_reanchor_off_track_to_consumer`` must refuse to run before the
    Stage 6.4 grid snap marks consumers final.

    The reanchor re-pins each off-track input at ``consumer.y -
    n*y_spacing``; running it against non-final (pre-snap) consumer Ys
    lands the icon off-grid (issue #463 bug (a)).  The precondition is
    enforced via ``graph._consumers_grid_snapped``, set right after the
    Stage 6.4 snap.  Clearing it models an earlier caller; the reanchor
    must raise ``PhaseInvariantError`` rather than silently mislocate.
    """
    graph = _layout(fixture)
    assert _off_track_groups(graph), f"{fixture}: no off-track sections"
    # Full layout sets the precondition; clear it to model a pre-snap caller.
    graph._consumers_grid_snapped = False
    y_spacing = compute_min_y_spacing(graph)
    with pytest.raises(PhaseInvariantError):
        _reanchor_off_track_to_consumer(graph, y_spacing)


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK)
def test_reanchor_off_track_bbox_fit_is_reversible(fixture):
    """Re-running the reanchor is order-independent: it recomputes the
    section top to fit, growing **or** shrinking.

    ``_grow_section_bbox_upward`` only ever lowers the top, so a stale or
    premature run that grew the box too tall bakes in excess slack that
    is never reclaimed (issue #463 bug (b)).  Bake in a too-tall top as a
    premature grow-only run would, re-run the reanchor, and assert the
    bbox top hugs the off-track band with exactly one ``SECTION_Y_PADDING``
    band and no excess slack.
    """
    graph = _layout(fixture)
    y_spacing = compute_min_y_spacing(graph)
    groups = _off_track_groups(graph)
    below = _off_track_output_below(graph)
    assert groups, f"{fixture}: no off-track sections"

    for sec_id in groups:
        section = graph.sections[sec_id]
        # Bake in a stale, too-tall top as a premature grow-only run would.
        _grow_section_bbox_upward(graph, section, section.bbox_y - 2 * y_spacing)

    _reanchor_off_track_to_consumer(graph, y_spacing)

    for sec_id, (_fallback, by_consumer) in groups.items():
        section = graph.sections[sec_id]
        # The top fit is driven only by the up-direction (above-anchor)
        # band; sections whose off-track all drop downward do not touch the
        # top, so there is nothing to reclaim.
        up_ys = [
            graph.stations[st.id].y
            for stations in by_consumer.values()
            for st in stations
            if st.id not in below
        ]
        if not up_ys:
            continue
        highest = min(up_ys)
        # The fit hugs the off-track band but clamps up to any on-track
        # content (or non-TOP port) sitting higher than the band, so the
        # ideal is the fit-top contract itself, not a bare band-minus-pad.
        # For input fixtures the band is the topmost content and the two
        # coincide; an output sink can sit below higher on-track branches.
        ideal_top = _off_track_fit_top(graph, section, highest, SECTION_Y_PADDING)
        assert section.bbox_y == pytest.approx(ideal_top, abs=_Y_TOL), (
            f"{fixture}/{sec_id}: bbox top {section.bbox_y:.1f} does not hug "
            f"the off-track band (expected {ideal_top:.1f}); grow-only left "
            f"{section.bbox_y - ideal_top:.1f}px of slack"
        )


def test_off_track_fit_top_clamps_to_content_above_band():
    """The reversible fit must not clip on-track content above the band.

    `_off_track_fit_top` seeds at ``highest_off_track - padding`` but
    clamps down to any content station that sits higher, so a shrink
    keeps that station's full padding band instead of cutting it off.
    Exercises the content clamp, which never binds at the byte-identical
    call sites (off-track is the topmost content there).
    """
    graph = _layout("da_pipeline.mmd")
    groups = _off_track_groups(graph)
    sec_id, (_fallback, by_consumer) = next(iter(groups.items()))
    section = graph.sections[sec_id]
    highest = min(
        graph.stations[st.id].y for stations in by_consumer.values() for st in stations
    )
    on_id = next(
        s
        for s in section.station_ids
        if not graph.stations[s].is_port
        and not graph.stations[s].off_track
        and not s.startswith("__bypass_")
    )
    graph.stations[on_id].y = highest - 30.0  # above the off-track band

    top = _off_track_fit_top(graph, section, highest, SECTION_Y_PADDING)

    assert top == pytest.approx(graph.stations[on_id].y - SECTION_Y_PADDING, abs=_Y_TOL)
    # Grew past the off-track-only fit rather than clipping the station.
    assert top < highest - SECTION_Y_PADDING - _Y_TOL


def test_off_track_fit_top_clamps_to_non_top_port():
    """A non-TOP port above the band bounds the fit so it isn't stranded.

    TOP ports follow the bbox edge and impose no bound; a LEFT/RIGHT/
    BOTTOM port sitting above the band top must keep the top from
    shrinking below it.  Exercises the port clamp.
    """
    graph = _layout("da_pipeline.mmd")
    groups = _off_track_groups(graph)
    sec_id, (_fallback, by_consumer) = next(iter(groups.items()))
    section = graph.sections[sec_id]
    highest = min(
        graph.stations[st.id].y for stations in by_consumer.values() for st in stations
    )
    band_top = highest - SECTION_Y_PADDING
    pid = next(
        p
        for p in section.entry_ports + section.exit_ports
        if graph.ports[p].side != PortSide.TOP
    )
    above = band_top - 20.0  # port above the band top
    graph.stations[pid].y = above
    graph.ports[pid].y = above

    top = _off_track_fit_top(graph, section, highest, SECTION_Y_PADDING)

    assert top == pytest.approx(above, abs=_Y_TOL)


# ---------------------------------------------------------------------------
# _section_fit_top clamp coverage
#
# The grow-only call site (Stage 6.15a) only fires when the off-track /
# fan band is the topmost content, so the bypass, port and row-above
# clamps never bind there.  Synthetic graphs isolate each clamp branch so
# the shrink direction is covered before any caller starts hugging
# content downward.
# ---------------------------------------------------------------------------


def _fit_top_section(*, content_ys, ports=(), bypass_ys=(), grid_row=0):
    """Build a one-section graph for exercising ``_section_fit_top``.

    ``content_ys`` are non-port station Ys; ``ports`` are ``is_port``
    station Ys; ``bypass_ys`` are ``__bypass_`` helper station Ys.  The
    section bbox spans x in [0, 100] so a row-above section at the same x
    overlaps in columns.
    """
    graph = MetroGraph()
    section = Section(
        id="s",
        name="S",
        station_ids=[],
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_w=100.0,
        bbox_h=400.0,
        grid_col=0,
        grid_row=grid_row,
        grid_row_span=1,
        grid_col_span=1,
    )
    graph.sections["s"] = section
    for i, y in enumerate(content_ys):
        sid = f"c{i}"
        graph.stations[sid] = Station(id=sid, label="x", section_id="s", y=y)
        section.station_ids.append(sid)
    for i, y in enumerate(ports):
        sid = f"p{i}"
        graph.stations[sid] = Station(
            id=sid, label="", section_id="s", is_port=True, y=y
        )
        section.station_ids.append(sid)
    for i, y in enumerate(bypass_ys):
        sid = f"__bypass_{i}"
        graph.stations[sid] = Station(id=sid, label="", section_id="s", y=y)
        section.station_ids.append(sid)
    return graph, section


def test_section_fit_top_anchors_on_highest_content():
    """The target hugs the topmost content station with a full padding band."""
    graph, section = _fit_top_section(content_ys=[200.0, 260.0])

    top = _section_fit_top(graph, section, SECTION_Y_PADDING, SECTION_Y_GAP)

    assert top == pytest.approx(200.0 - SECTION_Y_PADDING, abs=_Y_TOL)


def test_section_fit_top_clamps_to_port_above_content():
    """A port above the content band bounds the target so it stays inside.

    Mirrors the bottom anchor: ports are hard-contained (no extra
    padding), so a port higher than ``content_top - padding`` pulls the
    target up to the port rather than clipping it outside the box.
    """
    graph, section = _fit_top_section(content_ys=[260.0], ports=[120.0])

    top = _section_fit_top(graph, section, SECTION_Y_PADDING, SECTION_Y_GAP)

    # content-only fit would be 260 - 50 = 210; the port at 120 binds.
    assert top == pytest.approx(120.0, abs=_Y_TOL)


def test_section_fit_top_clamps_to_bypass_curve_clearance():
    """A bypass helper above content bounds the target by curve clearance."""
    v_curve_clearance = CURVE_RADIUS + MIN_STATION_FLAT_LENGTH / 2
    graph, section = _fit_top_section(content_ys=[260.0], bypass_ys=[100.0])

    top = _section_fit_top(graph, section, SECTION_Y_PADDING, SECTION_Y_GAP)

    # content-only fit (210) is overridden by 100 - curve clearance.
    assert top == pytest.approx(100.0 - v_curve_clearance, abs=_Y_TOL)


def test_section_fit_top_bounded_by_row_above():
    """The row-above term is a grow ceiling: it can only lower the top.

    A section in row 1 with a column-overlapping neighbour ending row 0
    cannot hug higher than ``above_bottom + section_y_gap +
    SECTION_HEADER_PROTRUSION``, reserving the header-badge clearance.
    """
    graph, section = _fit_top_section(content_ys=[260.0], grid_row=1)
    above = Section(
        id="above",
        name="Above",
        station_ids=[],
        bbox_x=0.0,
        bbox_y=0.0,
        bbox_w=100.0,
        bbox_h=200.0,
        grid_col=0,
        grid_row=0,
        grid_row_span=1,
        grid_col_span=1,
    )
    graph.sections["above"] = above

    top = _section_fit_top(graph, section, SECTION_Y_PADDING, SECTION_Y_GAP)

    floor = 200.0 + SECTION_Y_GAP + SECTION_HEADER_PROTRUSION
    # content-only fit (210) is below the floor, so the floor wins.
    assert floor > 260.0 - SECTION_Y_PADDING
    assert top == pytest.approx(floor, abs=_Y_TOL)


# ---------------------------------------------------------------------------
# Stacked file-input icons leave room for under-icon captions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    ["differentialabundance.mmd", "differentialabundance_default.mmd"],
)
def test_stacked_file_icons_label_clearance(example):
    """Two vertically-adjacent file-input stations sharing a column must
    sit far enough apart that the upper station's under-icon caption
    doesn't crash into the top edge of the lower icon.

    The default station pitch (``y_spacing`` ~ 40 px) is shorter than
    the captioned icon's vertical extent (~icon_height + caption_gap +
    caption_font_height = 32 + 4 + ~8 = 44 px).  Catches the regression
    where stacked source inputs in DA section 1 (Samples/Contrasts,
    Matrix, GTF, CEL, MaxQuant, GEO ID) have their captions visibly
    overlapping the next icon.
    """
    from nf_metro.layout.constants import (
        ICON_CAPTION_FONT_HEIGHT,
        ICON_CAPTION_GAP,
        ICON_HALF_HEIGHT,
        ICON_STACK_LABEL_CLEARANCE,
    )

    required_pitch = (
        2 * ICON_HALF_HEIGHT
        + ICON_CAPTION_GAP
        + ICON_CAPTION_FONT_HEIGHT
        + ICON_STACK_LABEL_CLEARANCE
    )

    graph = _layout_example(example)
    junction_ids = set(graph.junctions)

    def _has_caption(station) -> bool:
        if not station.is_terminus:
            return False
        return any(bool(n) for n in (station.terminus_names or []))

    # Group captioned terminus stations by section + column.
    by_col: dict[tuple[str, float], list[str]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if (
            st.is_port
            or sid in junction_ids
            or st.section_id is None
            or not _has_caption(st)
        ):
            continue
        by_col[(st.section_id, round(st.x, 1))].append(sid)

    tested = False
    for (sec_id, col_x), sids in by_col.items():
        if len(sids) < 2:
            continue
        tested = True
        sids.sort(key=lambda s: graph.stations[s].y)
        for upper_id, lower_id in zip(sids, sids[1:]):
            upper = graph.stations[upper_id]
            lower = graph.stations[lower_id]
            gap = lower.y - upper.y
            assert gap + _Y_TOL >= required_pitch, (
                f"{example} section {sec_id} col x={col_x}: "
                f"file-icon pair {upper_id} (y={upper.y}) -> "
                f"{lower_id} (y={lower.y}) gap={gap:.2f} px "
                f"< required {required_pitch:.2f} px "
                f"(2*icon_half + caption_gap + caption_font + clearance)"
            )

    assert tested, f"{example}: no captioned file-icon column with two icons"


# ---------------------------------------------------------------------------
# Off-track icons ordered top-to-bottom by their consumer Y
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    ["differentialabundance.mmd", "differentialabundance_default.mmd"],
)
def test_off_track_icons_ordered_by_consumer_y(example):
    """Within a section, the Y order of off-track input icons must
    match the Y order of their on-track consumers.

    When several off-track inputs feed different consumers in the same
    section, the icon for the upper consumer (smaller consumer Y) must
    sit above the icon for the lower consumer.  Catches the regression
    where placement followed mmd declaration order rather than consumer
    position, leaving the network icon above the gene-sets icon even
    though the network's consumer (decoupler) sits below the gene-sets
    consumer (GSEA).
    """
    graph = _layout_example(example)
    junction_ids = set(graph.junctions)

    # Build off_track -> in-section consumer map from edges.
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
            or src.section_id != tgt.section_id
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)

    # Group off-track stations by section.
    by_section: dict[str, list[str]] = defaultdict(list)
    for off_id in consumer_of:
        sid = graph.stations[off_id].section_id
        if sid is not None:
            by_section[sid].append(off_id)

    # Need at least one section with two distinct consumers to test
    # the ordering invariant.
    tested = False
    for sec_id, off_ids in by_section.items():
        distinct_consumers = {consumer_of[o] for o in off_ids}
        if len(distinct_consumers) < 2:
            continue
        tested = True
        # Sort off-track stations by their own Y (top to bottom).
        sorted_offs = sorted(off_ids, key=lambda o: graph.stations[o].y)
        # The consumer Ys, in the same order, must be non-decreasing.
        cons_ys = [graph.stations[consumer_of[o]].y for o in sorted_offs]
        for i in range(len(cons_ys) - 1):
            assert cons_ys[i] <= cons_ys[i + 1] + _Y_TOL, (
                f"{example} section {sec_id}: off-track icon order "
                f"does not match consumer Y order.  Icons (top->bottom): "
                f"{[(o, graph.stations[o].y) for o in sorted_offs]}; "
                f"their consumer Ys: {cons_ys}"
            )

    assert tested, f"{example}: no section with multiple off-track consumers"


# ---------------------------------------------------------------------------
# Bundle offsets must not jump at a section boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(_FIXTURES_MULTI_SECTION, _XFAIL_NO_KINK),
)
def test_no_kink_at_section_boundary(fixture):
    """Adjacent same-row LR sections must agree on the rendered cy
    of the row bundle's pass-through stations.  This catches the
    "limma kink" pattern: matrix_filter (data_prep exit) at cy=110.5
    but limma (differential entry) at cy=119.5, a 9px diagonal line
    visually breaking the horizontal trunk.

    The check pairs each section's exit port with the next section's
    entry port and asserts they share a Y at the rendered (offset-
    adjusted) level.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    rows = _row_lr_sections(graph)
    for row, sections in rows.items():
        sorted_secs = sorted(sections, key=lambda s: s.grid_col)
        for sec, nxt in zip(sorted_secs, sorted_secs[1:]):
            if nxt.grid_col - sec.grid_col != 1:
                continue
            # Exit port of sec
            for pid in sec.exit_ports:
                port = graph.ports.get(pid)
                if port is None or port.side != PortSide.RIGHT:
                    continue
                exit_lines = graph.station_lines(pid)
                if not exit_lines:
                    continue
                exit_offs = [offsets.get((pid, lid), 0.0) for lid in exit_lines]
                exit_cy = graph.stations[pid].y + (min(exit_offs) + max(exit_offs)) / 2
                # Matching entry port of next section
                for npid in nxt.entry_ports:
                    nport = graph.ports.get(npid)
                    if nport is None or nport.side != PortSide.LEFT:
                        continue
                    entry_lines = graph.station_lines(npid)
                    if set(exit_lines) != set(entry_lines):
                        continue
                    entry_offs = [offsets.get((npid, lid), 0.0) for lid in entry_lines]
                    entry_cy = (
                        graph.stations[npid].y + (min(entry_offs) + max(entry_offs)) / 2
                    )
                    assert abs(exit_cy - entry_cy) < _Y_TOL, (
                        f"Row {row}: exit port {pid} cy={exit_cy} != "
                        f"entry port {npid} cy={entry_cy}"
                    )


# ---------------------------------------------------------------------------
# A line must cross a perpendicular entry port at one consistent X
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_perp_entry_boundary_no_lateral_reversal(fixture):
    """A line through a shared TOP/BOTTOM entry port must not reverse its
    lateral direction at the boundary.

    The inter-section approach and the intra-section drop for the same line
    must cross the section boundary at one consistent per-line X.  When they
    disagree the line lands on the port marker, then re-fans off it - a visible
    S-cusp on the boundary (the ``cross_row_gap_wrap`` feed line entering Merge
    through ``merge_pt__entry_top_6``).
    """
    from nf_metro.layout.routing.invariants import (
        check_perp_entry_boundary_consistent,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    violations = check_perp_entry_boundary_consistent(graph, routes)
    assert not violations, "; ".join(v.message() for v in violations)


def test_perp_entry_boundary_check_detects_cusp():
    """Meaningfulness guard: the boundary-consistency check fires when a line's
    approach and drop cross a perp entry port at different X.

    Locks the detector so the corpus test above is not vacuously green: a TOP
    entry port whose single inter-section approach lands at one X while the
    intra drop departs at another (each via a marker jog) must be flagged.
    """
    from types import SimpleNamespace

    from nf_metro.layout.routing.common import RoutedPath
    from nf_metro.layout.routing.invariants import (
        check_perp_entry_boundary_consistent,
    )
    from nf_metro.parser.model import Edge, Port, Station

    port = Station("p", "P", x=100.0, y=50.0, is_port=True)
    g1 = Station("g1", "G1", x=70.0, y=80.0)
    junction = Station("j", "", x=10.0, y=20.0, is_port=True)
    graph = SimpleNamespace(
        ports={"p": Port("p", "sec", PortSide.TOP, is_entry=True)},
        stations={"p": port, "g1": g1, "j": junction},
        station_lines=lambda _pid: ["a"],
    )

    approach = RoutedPath(
        edge=Edge("j", "p", "a"),
        line_id="a",
        points=[(10.0, 20.0), (97.0, 20.0), (97.0, 50.0), (100.0, 50.0)],
    )
    departure = RoutedPath(
        edge=Edge("p", "g1", "a"),
        line_id="a",
        points=[(100.0, 50.0), (98.5, 50.0), (98.5, 80.0), (70.0, 80.0)],
    )
    violations = check_perp_entry_boundary_consistent(graph, [approach, departure])
    assert len(violations) == 1
    assert violations[0].approach_x == 97.0
    assert violations[0].departure_x == 98.5

    straight_dep = RoutedPath(
        edge=Edge("p", "g1", "a"),
        line_id="a",
        points=[(97.0, 50.0), (97.0, 80.0), (70.0, 80.0)],
    )
    assert not check_perp_entry_boundary_consistent(graph, [approach, straight_dep])


# ---------------------------------------------------------------------------
# Fan-out junctions share Y with their feeding LR/RL exit port
# ---------------------------------------------------------------------------


def _fanout_junction_exit_ports(graph: MetroGraph):
    """Yield ``(junction, exit_port)`` for every fan-out junction fed by a
    single LEFT/RIGHT exit port.

    A fan-out junction has exactly one port predecessor (the exit port the
    bundle leaves through) and more than one entry-port successor.  For
    LEFT/RIGHT (LR/RL) exit ports, ``_position_junctions`` anchors the
    junction at the exit port's Y so the bundle runs straight from exit to
    junction; BOTTOM/TOP exit ports are intentionally offset and excluded.
    """
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if junction is None:
            continue
        # One edge per line, so dedupe to distinct port endpoints.
        port_preds = {
            edge.source
            for edge in graph.edges_to(jid)
            if (src := graph.stations.get(edge.source)) and src.is_port
        }
        succ_entry_ports = {
            edge.target
            for edge in graph.edges_from(jid)
            if (tgt := graph.stations.get(edge.target)) and tgt.is_port
        }
        if len(port_preds) != 1 or len(succ_entry_ports) <= 1:
            continue
        exit_port = graph.stations.get(next(iter(port_preds)))
        port_obj = graph.ports.get(exit_port.id)
        if port_obj is None or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        yield junction, exit_port


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_fanout_junction_shares_exit_port_y(fixture):
    """A fan-out junction fed by an LR/RL exit port must sit at the exit
    port's Y.

    ``_position_junctions`` places such a junction at ``exit_port.y`` so the
    bundle runs straight from the exit into the junction.  Late settling
    stages (e.g. Stage 6.14 ``_shift_and_propagate_loop_stations``) can move
    the exit port after junctions were last positioned; if junctions are not
    re-anchored the junction is stranded above/below the port, forcing the
    fanned routes to dip to the stale junction Y and back (#386:
    complex_multipath section 3 -> sections 4/5 S-curve).
    """
    graph = _layout(fixture)
    for junction, exit_port in _fanout_junction_exit_ports(graph):
        assert abs(junction.y - exit_port.y) < _Y_TOL, (
            f"{fixture}: fan-out junction {junction.id} y={junction.y} "
            f"stranded from exit port {exit_port.id} y={exit_port.y}"
        )


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_fanout_junction_resolves_via_upstream(fixture):
    """Every fan-out junction resolves to a section through an incoming edge.

    A fan-out junction (``section_id is None``) is emitted with an
    ``exit_port -> junction`` edge whose source carries the source section's
    id, so ``resolve_section``'s upstream (incoming-edge) scan always resolves
    it; the downstream ``edges_from`` scan and the no-section ``return None``
    are never needed.  Asserting this across every multi-section fixture pins
    the reachability that lets ``resolve_section`` carry only the upstream scan.
    """
    graph = _layout(fixture)
    for jid in graph.junctions:
        junction = graph.stations.get(jid)
        if junction is None or junction.section_id:
            continue
        upstream_sectioned = any(
            (src := graph.stations.get(e.source)) is not None
            and src.section_id
            and graph.sections.get(src.section_id) is not None
            for e in graph.edges_to(jid)
        )
        assert upstream_sectioned, (
            f"{fixture}: fan-out junction {jid} has no sectioned upstream; "
            f"resolve_section would need its downstream fallback"
        )
        for prefer in (True, False):
            assert (
                resolve_section(graph, junction, prefer_upstream=prefer) is not None
            ), f"{fixture}: resolve_section({jid}, prefer_upstream={prefer}) is None"


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_entry_port_fed_only_by_ports(fixture):
    """Every edge into a section entry port originates at a port station.

    ``_resolve_sections`` rewrites inter-section edges into
    ``source -> exit_port -> entry_port -> target`` chains, so an entry
    port's incoming edges come from exit ports or fan-out junctions, all
    ``is_port=True``.  ``_section_line_feeders`` reads the feeder section
    directly off the source's ``section_id``, relying on this; a non-port
    source would mean an internal station feeds an entry port directly.
    """
    graph = _layout(fixture)
    for section in graph.sections.values():
        for pid in section.entry_ports:
            for edge in graph.edges_to(pid):
                src = graph.stations.get(edge.source)
                assert src is None or src.is_port, (
                    f"{fixture}: entry port {pid} fed by non-port station {edge.source}"
                )


@pytest.mark.parametrize("fixture", _FIXTURES_COMPACT)
def test_compact_multiline_entry_ports_pre_separated(fixture):
    """Multi-line entry ports already carry separated offsets before the
    entry-port pass runs.

    ``_apply_compact_section_consistency`` assigns each section's entry lines
    distinct ``i * offset_step`` offsets early in ``compute_station_offsets``,
    so by the time the entry-port pass runs the offsets on a multi-line entry
    port are never all-equal.  This pins the precondition that made the
    compact entry-port separation pass a no-op.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    for section in graph.sections.values():
        entry_lines = {
            lid for pid in section.entry_ports for lid in graph.station_lines(pid)
        }
        if len(entry_lines) < 2:
            continue
        existing = [
            offsets.get((pid, lid), 0.0)
            for pid in section.entry_ports
            for lid in entry_lines
            if lid in graph.station_lines(pid)
        ]
        assert len(set(existing)) >= 2, (
            f"{fixture}: section {section.id} multi-line entry ports share one "
            f"offset {set(existing)}; the separation pass was not redundant"
        )


# ---------------------------------------------------------------------------
# Inter-section routes don't backtrack in X against their net flow direction
# ---------------------------------------------------------------------------


def _route_exit_port_side(graph: MetroGraph, rp) -> PortSide | None:
    """Return the side of the exit port a route leaves through.

    The route source is either the exit port itself or a junction fed by
    one; trace back one step through a junction to reach the port.
    """
    port = graph.ports.get(rp.edge.source)
    if port is not None:
        return port.side
    # Source is a junction (also is_port=True but not a boundary port);
    # trace back one step to the feeding exit port.
    for e in graph.edges_to(rp.edge.source):
        port = graph.ports.get(e.source)
        if port is not None:
            return port.side
    return None


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_inter_section_route_no_x_backtrack(fixture):
    """A forward-flowing inter-section route between two LR columns must be
    X-monotonic: no horizontal segment may reverse against its net
    source->target direction.

    A right-to-left segment on a left-to-right route renders as a visible
    turn-back toward the section just left (#386: the standard/legacy climb
    out of Full Pre-process stepped from the fan-out junction back toward
    section 3 before going up, because the gap channel was centred in a
    wider sibling-row gap that sat left of the junction).

    Scoped to "forward" routes only: both endpoints resolve to LR sections
    in distinct columns AND the exit port faces the target column.  A route
    that exits a port facing AWAY from its target (e.g. a right-side port
    feeding a section to the left) must wrap, so its outward stub legitimately
    reverses; those, fold/serpentine (TB), same-column, and ``normalize_exempt``
    wrap legs are skipped.
    """
    graph = _layout(fixture)
    routes = route_edges(graph)
    for rp in routes:
        if not rp.is_inter_section or rp.normalize_exempt:
            continue
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.direction != "LR" or tgt_sec.direction != "LR":
            continue
        if src_sec.grid_col == tgt_sec.grid_col:
            continue
        rightward = tgt_sec.grid_col > src_sec.grid_col
        exit_side = _route_exit_port_side(graph, rp)
        # Only "forward" routes (exit port faces the target) must be
        # monotonic; an exit facing away legitimately wraps.
        if rightward and exit_side != PortSide.RIGHT:
            continue
        if not rightward and exit_side != PortSide.LEFT:
            continue
        xs = [p[0] for p in rp.points]
        for x1, x2 in zip(xs, xs[1:]):
            if rightward:
                assert x2 >= x1 - _Y_TOL, (
                    f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
                    f"backtracks left x={x1:.1f}->{x2:.1f} on a rightward route"
                )
            else:
                assert x2 <= x1 + _Y_TOL, (
                    f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
                    f"backtracks right x={x1:.1f}->{x2:.1f} on a leftward route"
                )


# ---------------------------------------------------------------------------
# Merge feeders descend the inter-column corridor, not the canvas bottom
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    sorted({*_FIXTURES_MULTI_SECTION, "genomic_pipeline.mmd"}),
)
def test_merge_feeder_does_not_loop_below_target(fixture):
    """A merge-junction feeder reaching a target row *below* its source must
    not dip far below the target section's bottom edge to reach it (#432).

    A multi-row collector fan-in feeds the left-entry ``reporting`` section
    (row 3) from QC sources that exit on the right in rows 0 and 1.  The
    naive around-below route drops the feeder to the very bottom of the
    canvas (below the tall ``variant_calling`` row-span), runs leftward
    there, then climbs back up into the entry - two big loops sweeping the
    canvas bottom.  A clear inter-column corridor exists between the source
    and target columns: drop into the inter-row gap below the source row,
    traverse left in that gap to the inter-column channel, then descend
    that channel straight to the entry.  This invariant pins the corridor:
    a downward cross-row feeder must stay within a bounded margin of the
    target section's bottom rather than looping below everything beneath
    it.

    Scoped to *downward cross-row* feeders.  A same-row fan-in merge
    legitimately U-routes through the inter-row gap *below* the row and
    climbs back into a same-row target (e.g. ``03b_fan_in_merge``,
    ``genomeassembly``); that gap-dip is the intended geometry, not a
    canvas-bottom loop.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    for rp in routes:
        if not (
            rp.edge.source.startswith("__junction")
            and rp.edge.target.startswith("__merge")
        ):
            continue
        src_sec = resolve_section(graph, graph.stations[rp.edge.source])
        tgt_sec = resolve_section(graph, graph.stations[rp.edge.target])
        if src_sec is None or tgt_sec is None:
            continue
        # Only downward cross-row feeders take the corridor; same-row
        # fan-ins legitimately U-route through the gap below the row.
        if tgt_sec.grid_row <= src_sec.grid_row:
            continue
        tgt_bottom = tgt_sec.bbox_y + tgt_sec.bbox_h
        max_y = max(p[1] for p in rp.points)
        # The route may sweep a little below the entry port (its descent
        # curve) but must not loop below the target section's whole box.
        assert max_y <= tgt_bottom + SECTION_Y_GAP + _Y_TOL, (
            f"{fixture}: merge feeder {rp.edge.source}->{rp.edge.target} "
            f"({rp.line_id}) dips to y={max_y:.1f}, far below the target "
            f"section bottom {tgt_bottom:.1f} - it loops below the canvas "
            f"instead of descending the inter-column corridor"
        )


def test_corridor_viability_requires_full_clearance_band():
    """The inter-row-gap corridor is chosen only when the gap fits its band.

    The corridor's leftward traverse runs in a clamp band
    ``[gap_top + INTER_ROW_EDGE_CLEARANCE, gap_bottom - INTER_ROW_HEADER_CLEARANCE]``;
    a bundle of ``n`` lines needs ``(n - 1) * OFFSET_STEP`` of room inside
    it.  When the gap is narrower than
    ``INTER_ROW_EDGE_CLEARANCE + span + INTER_ROW_HEADER_CLEARANCE`` the band
    inverts and every line collapses onto one Y, producing a collinear
    overlay and a route through the source section's bottom edge (#722).
    Below that threshold the feeder must fall back to the around-below
    route, so ``_corridor_is_viable`` must return ``False``.
    """
    from nf_metro.layout.constants import (
        CURVE_RADIUS,
        DIAGONAL_RUN,
        INTER_ROW_EDGE_CLEARANCE,
        INTER_ROW_HEADER_CLEARANCE,
    )
    from nf_metro.layout.routing.common import row_bottom_edge, row_top_edge
    from nf_metro.layout.routing.context import _resolve_section_colrow
    from nf_metro.layout.routing.core import _build_routing_context
    from nf_metro.layout.routing.inter_section_handlers import _corridor_is_viable

    # Narrow gap: a two-line bundle crossing into a left-entry target two
    # rows below, with only the default 50px gap below its source row.
    graph = _layout("topologies/corridor_narrow_gap_fallback.mmd")
    offsets = compute_station_offsets(graph)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    src = graph.stations["source__exit_right_0"]
    ep = graph.stations["target__entry_left_2"]
    src_col, src_row = _resolve_section_colrow(graph, src)
    gap_top = row_bottom_edge(graph, src_row, col=src_col)
    gap_bottom = row_top_edge(graph, src_row + 1, col=src_col, default=gap_top)
    gap = gap_bottom - gap_top
    floor = INTER_ROW_EDGE_CLEARANCE + INTER_ROW_HEADER_CLEARANCE
    assert gap < floor, (
        f"fixture precondition: gap below source row is {gap:.1f}px, "
        f"expected narrower than the {floor:.0f}px floor"
    )
    assert not _corridor_is_viable(ctx, src, ep), (
        f"corridor chosen for a {gap:.1f}px gap that cannot hold its "
        f"{floor:.0f}px+ clearance band"
    )

    # Positive control: genomic_pipeline's 84px corridor gap exactly fits a
    # three-line bundle's band, so viability holds there.
    g2 = _layout("genomic_pipeline.mmd")
    off2 = compute_station_offsets(g2)
    ctx2 = _build_routing_context(g2, DIAGONAL_RUN, CURVE_RADIUS, off2)
    assert any(
        _corridor_is_viable(ctx2, s, ep2)
        for s in g2.stations.values()
        for ep2 in g2.stations.values()
    ), (
        "genomic_pipeline should still route at least one feeder through the "
        "corridor; the viability tightening over-constrained it"
    )


@pytest.mark.parametrize(
    "fixture",
    sorted({*_FIXTURES_MULTI_SECTION, "genomic_pipeline.mmd"}),
)
def test_junction_same_line_fans_coincide_or_separate(fixture):
    """Two routes carrying the SAME line out of a UNIFIED-FAN junction must
    either coincide on their source-side vertical channel or separate
    clearly - never smear by a few px (#437).

    the ``__junction_9`` (Post-processing's right exit) fans the same
    three lines to two destinations: the spine into Annotation (a LEFT-entry
    wrap) and the QC feed down the inter-column corridor to MultiQC.  The
    engine assigns both a shared :func:`_compute_junction_fan_info` position
    so they're MEANT to pivot through one channel; when their first vertical
    channels sit 6-18px apart they read as a single smeared band rather than
    one clean overlay.  This invariant forbids that intermediate spacing:
    for each unified-fan junction and each line fanning to >=2 inter-section
    targets, the first vertical legs must coincide (<= ``OFFSET_STEP`` apart,
    i.e. within the per-bundle stagger) or be cleanly separated (>= a
    section gap apart, the intended split when targets land in different
    columns).

    Scoped to junctions the engine treats as a unified fan (present in
    ``junction_fan_info``); pure L-shape/bypass fans to genuinely distinct
    columns are a separate concern.
    """
    from nf_metro.layout.constants import CURVE_RADIUS, DIAGONAL_RUN, OFFSET_STEP
    from nf_metro.layout.engine import first_vertical_leg_x
    from nf_metro.layout.phases._common import first_vertical_leg_sign
    from nf_metro.layout.routing.core import _build_routing_context

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    ctx = _build_routing_context(graph, DIAGONAL_RUN, CURVE_RADIUS, offsets)
    fan_sources = {key[0] for key in ctx.junction_fan_info}

    # Group inter-section routes by (junction source, line), tracking the V1
    # vertical direction; opposite-direction channels diverge and can't smear.
    by_src_line: dict[tuple[str, str], list] = defaultdict(list)
    for rp in routes:
        if not rp.is_inter_section or rp.edge.source not in fan_sources:
            continue
        vx = first_vertical_leg_x(rp.points)
        sign = first_vertical_leg_sign(rp.points)
        if vx is None or sign is None:
            continue
        by_src_line[(rp.edge.source, rp.line_id)].append((vx, sign))

    coincide_tol = OFFSET_STEP + _Y_TOL
    separate_min = SECTION_Y_GAP
    for (src, line), entries in by_src_line.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries)
        for (lo, lo_sign), (hi, hi_sign) in zip(ordered, ordered[1:]):
            if lo_sign != hi_sign:
                continue
            gap = hi - lo
            assert gap <= coincide_tol or gap >= separate_min, (
                f"{fixture}: junction {src} line {line} fans two routes "
                f"whose first vertical channels are {gap:.1f}px apart "
                f"(x={lo:.1f} vs {hi:.1f}) - neither coincident "
                f"(<= {coincide_tol:.1f}) nor clearly separated "
                f"(>= {separate_min:.1f}); a smeared partial overlap"
            )


@pytest.mark.parametrize("fixture", sorted({*_FIXTURES_MULTI_SECTION_PLUS_STACK}))
def test_inter_section_route_no_full_width_dogleg_clean(fixture):
    """No merge feeder takes a full-width out-and-back dog-leg (#432).

    The corridor route's long leftward traverse in the inter-row gap is a
    monotonic approach toward the target, not a backtrack, so the refined
    full-width guard passes on now.
    """
    from nf_metro.layout.engine import (
        _canvas_width,
        inter_section_route_backtrack_legs,
    )

    graph = _layout(fixture)
    routes = route_edges(graph)
    canvas_width = _canvas_width(graph)
    assert canvas_width > 0
    limit = 0.4 * canvas_width
    for rp, x1, x2 in inter_section_route_backtrack_legs(graph, routes):
        span = abs(x2 - x1)
        assert span <= limit + _Y_TOL, (
            f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
            f"backtracks {span:.1f}px in one leg (x={x1:.1f}->{x2:.1f}), "
            f"exceeding 40% of canvas width {canvas_width:.1f}"
        )


# a multi-row collector fan-in now descends the shared inter-column corridor into
# the left-entry ``reporting`` section, so the feeders are X-monotonic toward
# the target and no longer trip the full-width dog-leg guard.
_XFAIL_DOGLEG: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture", _params_with_xfails(_FIXTURES_DOGLEG, _XFAIL_DOGLEG)
)
def test_inter_section_route_no_full_width_dogleg(fixture):
    """A forward inter-section route may reverse in X to reach a target
    column nested inside an oversized sibling, but no single backtrack leg
    may sweep more than 40% of the canvas width (#425).

    The strict X-monotonic guard above forbids *any* reversal on a forward
    LR route and skips wrapping (``normalize_exempt``) routes.  When a
    narrow target column nests inside an oversized sibling, reaching it
    requires a legitimate reversal, so such routes are exempt.  This still
    bounds those reversals: a right-then-left dog-leg sweeping the whole
    diagram is forbidden even when exempt.
    """
    from nf_metro.layout.engine import (
        _canvas_width,
        inter_section_route_backtrack_legs,
    )

    graph = _layout(fixture)
    routes = route_edges(graph)
    canvas_width = _canvas_width(graph)
    assert canvas_width > 0
    limit = 0.4 * canvas_width
    for rp, x1, x2 in inter_section_route_backtrack_legs(graph, routes):
        span = abs(x2 - x1)
        assert span <= limit + _Y_TOL, (
            f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
            f"backtracks {span:.1f}px in one leg (x={x1:.1f}->{x2:.1f}), "
            f"exceeding 40% of canvas width {canvas_width:.1f}"
        )


# ---------------------------------------------------------------------------
# Routes enter/leave sections only at declared ports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION_PLUS_STACK)
def test_routes_enter_sections_only_at_ports(fixture):
    """No routed segment may cross a section bbox boundary except within
    tolerance of a declared port (#432).

    A line that slices through a section box anywhere other than a port is
    visually entering/leaving where nothing invites it - the symptom of a
    port inferred on the wrong side (so the connector cuts the box) or a
    fan-in bundle ploughing into a section through an undeclared edge.
    """
    from nf_metro.layout.engine import _route_crosses_section_boundary

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    hit = _route_crosses_section_boundary(graph, routes)
    assert hit is None, (
        f"{fixture}: route {hit[0].edge.source}->{hit[0].edge.target} "
        f"({hit[0].line_id}) crosses section {hit[1]!r} boundary at "
        f"({hit[2]:.1f}, {hit[3]:.1f}) away from any declared port"
        if hit is not None
        else ""
    )


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION_PLUS_STACK)
def test_no_route_passes_through_unrelated_section(fixture):
    """A line may only occupy a section's bbox where it connects to a
    station there (#484).

    The route edge's source section (the line starts there) and target
    section (it enters via that section's port) are the only boxes a route
    may intersect.  A segment crossing any other section's interior plots
    the line over a section it never interacts with -- the long-range
    pass-through that makes a dense diagram unreadable.  Unlike
    ``test_routes_enter_sections_only_at_ports`` this checks the final
    rendered geometry for *every* route, including fan-in/-out bundle
    routes through junction/merge nodes.
    """
    from nf_metro.layout.phases._common import routes_through_unrelated_sections

    graph = _layout(fixture)
    offenders = routes_through_unrelated_sections(graph)
    assert not offenders, f"{fixture}: " + "; ".join(
        f"{rp.line_id} {rp.edge.source}->{rp.edge.target} through {sid!r}"
        for rp, sid in offenders[:5]
    )


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION_PLUS_STACK)
def test_entry_approach_arrives_from_port_side(fixture):
    """A route's final approach to an entry port must arrive from the
    port's own outward side, not by crossing the target section interior
    to reach a far-side port (#484).

    A RIGHT entry port must be reached from ``x >= section_right`` and a
    LEFT entry port from ``x <= section_left``; the perpendicular TOP /
    BOTTOM ports from above / below.  A route whose final approach leg
    starts inside the target box and runs outward to the port has sliced
    through the interior and doubled back.  Distinct from
    ``test_no_route_passes_through_unrelated_section`` (which exempts the
    route's own target section), this catches the route crossing its OWN
    target's interior.
    """
    from nf_metro.layout.phases.guards import _entry_approach_offenders
    from nf_metro.layout.routing import route_edges

    graph = _layout(fixture)
    routes = route_edges(graph)
    offenders = _entry_approach_offenders(graph, routes)
    assert not offenders, f"{fixture}: " + "; ".join(
        f"{rp.line_id} {rp.edge.source}->{rp.edge.target}: {reason}"
        for rp, _pid, reason in offenders[:5]
    )


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION_PLUS_STACK)
def test_no_artefactual_counter_flow(fixture):
    """A feed from a row above its target must not dive below the target row
    and run its long horizontal counter to that row's flow when a clear
    inter-row gap above the target was free (#484).

    Serpentine rows alternate flow (even LR, odd RL).  A route into a RIGHT
    entry port from a higher row can run its rightward traverse in the clear
    band just above the target row, then drop straight into the port from the
    right.  Diving under the whole target row instead runs that traverse
    against the row's flow -- artefactual counter-flow caused by the routing
    picking the wrong channel.  Topological counter-flow (LEFT/TOP/BOTTOM
    entry wraps, or a dive whose gap-above band is blocked) is exempt; the
    guard fires only when the with-flow gap above the target was genuinely
    available yet unused.
    """
    from nf_metro.layout.phases.guards import (
        PhaseInvariantError,
        _guard_no_artefactual_counter_flow,
    )
    from nf_metro.layout.routing import route_edges

    graph = _layout(fixture)
    routes = route_edges(graph)
    try:
        _guard_no_artefactual_counter_flow(graph, fixture, routes=routes)
    except PhaseInvariantError as exc:
        pytest.fail(str(exc))


# ---------------------------------------------------------------------------
# Inter-row horizontal channels keep clearance from the source section
# ---------------------------------------------------------------------------


def _section_bbox(sec) -> tuple[float, float, float, float]:
    """Return final ``(left, right, top, bottom)`` of a section bbox.

    By routing time the grid placement offset is already folded into
    ``bbox_x``/``bbox_y`` (they are the rendered coordinates); ``offset_*``
    is a stale placement-time record and must not be re-added.
    """
    return sec.bbox_x, sec.bbox_x + sec.bbox_w, sec.bbox_y, sec.bbox_y + sec.bbox_h


@pytest.mark.parametrize("fixture", _FIXTURES_INTER_ROW_CLEARANCE)
def test_inter_row_run_clears_source_section(fixture):
    """A horizontal leg of an inter-*row* route must not graze its source
    section's bbox edge.

    When an inter-section bundle crosses grid rows (e.g. a right-exit that
    wraps down to a left-entry in the row below), its horizontal run lands
    in the inter-row gap.  That run must keep at least
    ``INTER_ROW_EDGE_CLEARANCE`` from the source section's near edge so it
    doesn't read as "running along under the box".
    """
    graph = _layout(fixture)
    routes = route_edges(graph)
    A = INTER_ROW_EDGE_CLEARANCE
    for rp in routes:
        if not rp.is_inter_section:
            continue
        src_sec = resolve_section(graph, graph.stations.get(rp.edge.source))
        tgt_sec = resolve_section(graph, graph.stations.get(rp.edge.target))
        if src_sec is None or tgt_sec is None:
            continue
        if src_sec.grid_row == tgt_sec.grid_row:
            continue  # inter-row routes only
        left, right, top, bottom = _section_bbox(src_sec)
        pts = rp.points
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y1 - y0) > _Y_TOL or abs(x1 - x0) < _Y_TOL:
                continue  # horizontal runs only
            xlo, xhi = sorted((x0, x1))
            if xhi <= left + _Y_TOL or xlo >= right - _Y_TOL:
                continue  # run doesn't overlap the source section horizontally
            y = y0
            if y > bottom + _Y_TOL:
                assert y - bottom >= A - _Y_TOL, (
                    f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
                    f"horizontal run y={y:.1f} sits only {y - bottom:.1f}px below "
                    f"source section {src_sec.id!r} bottom={bottom:.1f} (< {A})"
                )
            elif y < top - _Y_TOL:
                assert top - y >= A - _Y_TOL, (
                    f"{fixture}: {rp.line_id} {rp.edge.source}->{rp.edge.target} "
                    f"horizontal run y={y:.1f} sits only {top - y:.1f}px above "
                    f"source section {src_sec.id!r} top={top:.1f} (< {A})"
                )


# ---------------------------------------------------------------------------
# Side-branch single-line edges stay off the trunk inside the section
# ---------------------------------------------------------------------------


def _section_for(graph: MetroGraph, sid: str):
    """Return the section a station belongs to (or None)."""
    st = graph.stations.get(sid)
    if st is None or st.section_id is None:
        return None
    return graph.sections.get(st.section_id)


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_side_branch_edge_stays_off_trunk(fixture):
    """A side-branch single-line exit edge must keep its own track
    inside the section instead of joining the main trunk bundle
    immediately after the source station.

    For each non-port internal station S that sits clearly off the
    section's trunk Y (a side branch) and feeds the section's exit
    port or another internal station on the trunk via a single
    outgoing line, walking the routed path from the source forward
    must keep the line on the source Y for at least half of the
    horizontal distance to the target.  The diagonal/climb to the
    trunk must therefore start past the path midpoint between source
    and target, not within the first quarter as the propd regression
    produced.

    Catches the propd regression where the rnaseq line from propd
    climbed to trunk Y immediately after the station, leaving the
    side-branch slot empty for the rest of the section and visually
    merging with the main bundle.
    """
    graph = _layout(fixture)
    routes = route_edges(graph)

    rows = _row_lr_sections(graph)
    section_trunk_y: dict[str, float] = {}
    for sections in rows.values():
        for sec in sections:
            port_ys = _section_lr_port_ys(graph, sec)
            if port_ys:
                section_trunk_y[sec.id] = port_ys[0]

    # Build per-station outbound edges with line set
    outbound: dict[str, list] = defaultdict(list)
    for edge in graph.edges:
        outbound[edge.source].append(edge)

    junction_ids = set(graph.junctions)
    asserted = 0
    for sid, st in graph.stations.items():
        if st.is_port or st.off_track or sid in junction_ids:
            continue
        sec = _section_for(graph, sid)
        if sec is None:
            continue
        trunk_y = section_trunk_y.get(sec.id)
        if trunk_y is None:
            continue
        # Side branch: clearly off the trunk Y (> 2 grid slot offsets).
        if abs(st.y - trunk_y) < 6.0:
            continue
        # Single-line source only.
        src_lines = graph.station_lines(sid)
        if len(src_lines) != 1:
            continue
        for edge in outbound[sid]:
            # Find the matching routed path
            rp = next(
                (
                    r
                    for r in routes
                    if r.edge.source == edge.source
                    and r.edge.target == edge.target
                    and r.edge.line_id == edge.line_id
                ),
                None,
            )
            if rp is None or len(rp.points) < 2:
                continue
            tgt = graph.stations.get(edge.target)
            if tgt is None:
                continue
            tgt_port = graph.ports.get(edge.target)
            same_sec_target = tgt.section_id == sec.id and not tgt.is_port
            is_exit_port = (
                tgt_port is not None
                and not tgt_port.is_entry
                and tgt_port.section_id == sec.id
                and tgt_port.side in (PortSide.LEFT, PortSide.RIGHT)
            )
            if not (same_sec_target or is_exit_port):
                continue
            # Target must sit at or near trunk Y (where the bundle lives).
            if abs(tgt.y - trunk_y) > 6.0:
                continue
            # Walk the path from source: find the X at which the path
            # leaves the source's Y (where the climb starts).
            pts = rp.points
            src_x, src_y = pts[0]
            tgt_x = tgt.x
            leave_x: float | None = None
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                if abs(y0 - src_y) < _Y_TOL and abs(y1 - src_y) >= _Y_TOL:
                    leave_x = x0
                    break
                if abs(y1 - src_y) >= _Y_TOL:
                    leave_x = x1
                    break
            if leave_x is None:
                continue
            # The climb must start past 30% of the source->target run.
            # Pre-fix routes climbed within the first 15% (the diagonal
            # sat near the source under the standard fork bias).
            run = tgt_x - src_x
            if abs(run) < 1.0:
                continue
            climb_frac = (leave_x - src_x) / run
            assert climb_frac >= 0.30 - 1e-6, (
                f"Side-branch edge {edge.source}->{edge.target} "
                f"({edge.line_id}) climbs at x={leave_x:.2f} "
                f"({climb_frac:.0%} of source->target run); expected "
                f">= 30% (src_x={src_x:.2f}, tgt_x={tgt_x:.2f}, "
                f"section={sec.id})"
            )
            asserted += 1
    assert asserted > 0, f"{fixture}: no side-branch single-line exits found to test"


# ---------------------------------------------------------------------------
# Section bbox must contain all stations and off-track inputs
# ---------------------------------------------------------------------------


# Default terminus icon and station marker half-heights from the theme,
# used to verify section bboxes enclose every station's vertical reach.
_ICON_HALF_HEIGHT = 16.0
_MARKER_HALF_HEIGHT = 9.5


# Parameter sets the bbox-contains-content invariant runs at across the
# full corpus.  Limited to ``default`` (each fixture's authored
# directives) because the savepoint-cp param set triggers a pre-existing
# fastp-above-bbox regression in rnaseq_sections that is tracked
# separately; the DA-specific parametrization below covers the
# savepoint-cp + default-no-cp variants on da_pipeline.mmd.
_BBOX_PARAM_SETS = [
    pytest.param({}, id="default"),
]

# The full corpus plus a deliberately-unsupported topology (an internally
# horizontal section whose only ports are perpendicular, leaving no
# flow-aligned edge to anchor the horizontal run -- issue #424).  On the
# corpus the invariant holds outright; on the regression fixture the engine
# must either keep content in-bbox or reject it loudly, never silently
# overflow.
_BBOX_CONTAINMENT_FIXTURES = [
    *ALL_FIXTURES,
    "regressions/lr_perpendicular_ports_overflow.mmd",
]


@pytest.mark.parametrize("fixture", _BBOX_CONTAINMENT_FIXTURES)
@pytest.mark.parametrize("params", _BBOX_PARAM_SETS)
def test_section_bbox_contains_all_content(fixture, params):
    """Every section's bbox must contain its on-track stations and any
    off-track / terminus icons, on both axes.  Catches regressions where
    an icon (off-track input or single-icon terminus) is placed near the
    bbox top so the icon spills outside the section background, and where
    an internally-horizontal section lays its stations out to the right of
    its own bbox (issue #424).

    Margin: on-track station markers reach ~9.5 px above the centre,
    file icons reach ``terminus_height / 2 = 16`` px above the centre
    (both off-track inputs and on-track terminus stations render the
    same icon at ``station.y + bundle_mid``).  We assert
    ``station.y - reach >= bbox_y - 0.5`` (sub-pixel tolerance) and
    ``station.y + reach <= bbox_y + bbox_h + 0.5`` vertically, and the
    station centre within ``[bbox_x - 0.5, bbox_x + bbox_w + 0.5]``
    horizontally.

    A loud ``PhaseInvariantError`` upholds the invariant: it means the
    engine refused to ship an out-of-bbox layout rather than rendering it
    silently.  The dedicated rejection test below pins that path.
    """
    try:
        graph = _layout(fixture, **params)
    except PhaseInvariantError:
        return
    junction_ids = set(graph.junctions)

    for sec_id, section in graph.sections.items():
        if section.bbox_h <= 0:
            continue
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or sid in junction_ids:
                continue
            # File icons are used for off-track inputs and for terminus
            # stations rendered with a file icon (single named output).
            uses_icon = st.off_track or st.is_terminus
            half = _ICON_HALF_HEIGHT if uses_icon else _MARKER_HALF_HEIGHT
            top = st.y - half
            bot = st.y + half
            assert top >= section.bbox_y - 0.5, (
                f"Section {sec_id}: station {sid} top={top} "
                f"(y={st.y}, half={half}) overflows bbox top "
                f"y={section.bbox_y}"
            )
            assert bot <= section.bbox_y + section.bbox_h + 0.5, (
                f"Section {sec_id}: station {sid} bottom={bot} "
                f"(y={st.y}, half={half}) overflows bbox bottom "
                f"y={section.bbox_y + section.bbox_h}"
            )
            assert (
                section.bbox_x - 0.5 <= st.x <= section.bbox_x + section.bbox_w + 0.5
            ), (
                f"Section {sec_id}: station {sid} x={st.x} outside bbox "
                f"x-range [{section.bbox_x}, {section.bbox_x + section.bbox_w}]"
            )


def test_lr_section_all_perpendicular_ports_rejected():
    """An internally-LR/RL section whose only ports are perpendicular
    (every entry/exit on top/bottom) has no flow-aligned edge to anchor
    its horizontal run, so its stations are laid out past the right of
    its own bbox.  The engine must reject this loudly with an actionable
    message naming the section, rather than rendering it silently (#424).
    """
    text = (
        FIXTURES / "regressions" / "lr_perpendicular_ports_overflow.mmd"
    ).read_text()
    graph = parse_metro_mermaid(text)
    graph.center_ports = True
    with pytest.raises(PhaseInvariantError) as excinfo:
        compute_layout(graph)
    msg = str(excinfo.value).lower()
    assert "annotation" in msg
    assert "perpendicular" in msg or "flow-aligned" in msg


@pytest.mark.parametrize(
    "fixture",
    [
        "topologies/lr_perp_top_exit_side_entry.mmd",
        "topologies/lr_perp_bottom_exit_side_entry.mmd",
    ],
)
def test_lr_perp_multiline_exit_routes_cleanly(fixture):
    """A multi-line perpendicular (TOP/BOTTOM) exit on a flow-anchored LR/RL
    section places its exit port off every internal station's X (no
    station-as-elbow) and separates the exiting lines into distinct channels.

    ``validate=True`` succeeds, the station-as-elbow constraint holds, and no
    pair of distinct lines coincides on a vertical run or flips bundle order
    through the up-and-over corners (#706).
    """
    graph = _layout(fixture, validate=True)

    elbow = check_station_as_elbow(graph)
    assert not elbow, "; ".join(v.message for v in elbow)

    # The exiting lines must not collapse onto one channel anywhere.
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    assert not check_no_collinear_distinct_lines(graph, routes, offsets)
    assert not check_bundle_order_preserved(routes)


# ---------------------------------------------------------------------------
# Station labels must not overlap each other or non-owner markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_label_overlap(fixture):
    """No station label may overlap another label or a non-owner marker.

    The auto-spacing engine wraps wide labels and widens column/row pitch so
    dense sections don't ship colliding labels (issue #405).  This asserts
    the final rendered placements are collision-free across the whole corpus
    -- label/label overlap is never allowed; a label grazing a marker within
    ``LABEL_OVERLAP_TOL`` is tolerated (see ``find_label_overlaps``).
    """
    from nf_metro.layout.labels import find_label_overlaps, place_labels

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    overlaps = find_label_overlaps(graph, placements, offsets)
    assert not overlaps, "; ".join(
        f"{ov.a!r} overlaps {ov.kind} {ov.b!r} by ({ov.ox:.1f}, {ov.oy:.1f})px"
        for ov in overlaps
    )


# ---------------------------------------------------------------------------
# Adjacent-row sections that overlap horizontally must keep the configured
# section_y_gap between upper bbox bottom and lower bbox top
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_row_gap_between_adjacent_rows(fixture):
    """For every section pair in adjacent grid rows that share horizontal
    extent, the bbox-to-bbox vertical gap must be at least
    ``SECTION_Y_GAP``.
    """
    graph = _layout(fixture)
    for usid, us in graph.sections.items():
        if us.bbox_w <= 0 or us.bbox_h <= 0:
            continue
        next_row = us.grid_row + us.grid_row_span
        for lsid, ls in graph.sections.items():
            if ls.bbox_w <= 0 or ls.bbox_h <= 0 or ls.grid_row != next_row:
                continue
            if not (
                us.bbox_x < ls.bbox_x + ls.bbox_w and ls.bbox_x < us.bbox_x + us.bbox_w
            ):
                continue
            gap = ls.bbox_y - (us.bbox_y + us.bbox_h)
            assert gap >= SECTION_Y_GAP - 0.5, (
                f"row gap below required: {usid!r} (bottom) and {lsid!r} "
                f"(top) overlap horizontally and are {gap:.1f}px apart, "
                f"expected >= {SECTION_Y_GAP:.1f}px"
            )


# ---------------------------------------------------------------------------
# Sections with empty above-trunk bands but multiple movable siblings below
# should auto-balance so the top band shrinks to one y_spacing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section_top_band_filled(fixture):
    """LR/RL sections with room for another above-trunk slot AND
    multiple below-trunk movable siblings should fill the empty top
    band, not leave it stranded.
    """
    y_spacing = 55.0
    label_clearance = y_spacing / 2
    graph = _layout(fixture, y_spacing=y_spacing)

    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        bundle = _section_full_bundle(graph, section)
        if not bundle:
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        cols: dict[float, list[str]] = defaultdict(list)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            cols[round(st.x, 1)].append(sid)

        trunk_y: float | None = None
        for pid in section.entry_ports + section.exit_ports:
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                trunk_y = st.y
                break
        if trunk_y is None:
            for sids in cols.values():
                for s in sids:
                    if set(graph.station_lines(s)) == bundle:
                        trunk_y = graph.stations[s].y
                        break
                if trunk_y is not None:
                    break
        if trunk_y is None:
            continue

        all_internal = [s for sids in cols.values() for s in sids]
        if not all_internal:
            continue
        top_y = min(graph.stations[s].y for s in all_internal)
        top_band = top_y - section.bbox_y
        if top_band <= y_spacing + _Y_TOL:
            continue

        movable_above = 0
        movable_below_candidates: list[str] = []
        for _x, sids in cols.items():
            trunks_here = [s for s in sids if set(graph.station_lines(s)) == bundle]
            if not trunks_here:
                continue
            for s in sids:
                if s in trunks_here:
                    continue
                lines = set(graph.station_lines(s))
                if not lines or not (lines < bundle):
                    continue
                y = graph.stations[s].y
                if y < trunk_y - _Y_TOL:
                    movable_above += 1
                elif y > trunk_y + _Y_TOL:
                    movable_below_candidates.append(s)

        if len(movable_below_candidates) < 2 or movable_above >= len(
            movable_below_candidates
        ):
            continue

        target_y = top_y - y_spacing
        any_fits = any(
            target_y
            >= section.bbox_y
            + (
                label_clearance
                if graph.stations[s].label and graph.stations[s].label.strip()
                else 0.0
            )
            - _Y_TOL
            for s in movable_below_candidates
        )
        if not any_fits:
            continue

        assert top_band <= y_spacing + _Y_TOL, (
            f"Section {section.id}: top band {top_band:.1f}px > "
            f"{y_spacing:.1f} while {len(movable_below_candidates)} "
            f"movable siblings sit below trunk and only "
            f"{movable_above} above; balance pass should lift one "
            f"into the top slot"
        )


# ---------------------------------------------------------------------------
# Section 1 (data_prep): at least one input must sit above the trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section1_input_above_trunk(fixture):
    """In ``data_prep`` (the source-stack section) inputs must fill
    the above-trunk band: at least one input sits above the trunk, and
    the topmost input is no more than y_spacing below the bbox top.
    """
    y_spacing = 55.0
    graph = _layout(fixture, y_spacing=y_spacing)
    section = graph.sections.get("data_prep")
    assert section is not None
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    trunk_y: float | None = None
    for pid in section.entry_ports + section.exit_ports:
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
            trunk_y = st.y
            break
    assert trunk_y is not None, "data_prep has no LR port for trunk Y"

    has_in: set[str] = {e.target for e in graph.edges}
    inputs = [
        sid
        for sid in section.station_ids
        if sid not in port_ids
        and sid not in has_in
        and sid in graph.stations
        and not graph.stations[sid].is_port
    ]
    inputs_above = [sid for sid in inputs if graph.stations[sid].y < trunk_y - _Y_TOL]
    assert inputs_above, (
        f"data_prep: no input sits above trunk_y={trunk_y:.1f} "
        f"(inputs at y={[graph.stations[s].y for s in inputs]})"
    )
    top_input_y = min(graph.stations[s].y for s in inputs_above)
    top_band = top_input_y - section.bbox_y
    assert top_band <= y_spacing + _Y_TOL, (
        f"data_prep: top input at y={top_input_y:.1f} leaves "
        f"top_band={top_band:.1f}px > {y_spacing:.1f} (bbox_y="
        f"{section.bbox_y:.1f}); balance pass should lift another "
        f"input into the top slot"
    )


# ---------------------------------------------------------------------------
# Terminus stations must not be hit by a diagonal route segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_terminus_not_directly_after_diagonal(fixture):
    """Routes terminating at an output terminus must arrive on an
    orthogonal (horizontal or vertical) final segment.
    """
    MIN_LEN = 30.0
    AXIS_TOL = 1.0
    graph = _layout(fixture)
    routes = route_edges(graph)
    by_target: dict[str, list] = defaultdict(list)
    for r in routes:
        tgt = graph.stations.get(r.edge.target)
        if tgt is None or not tgt.is_terminus:
            continue
        by_target[r.edge.target].append(r)

    for tid, paths in by_target.items():
        sources = {r.edge.source for r in paths}
        if len(sources) < 2:
            continue
        for r in paths:
            pts = r.points
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1, 0, -1):
                x1, y1 = pts[i - 1]
                x2, y2 = pts[i]
                length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                if length < MIN_LEN:
                    continue
                dx = abs(x2 - x1)
                dy = abs(y2 - y1)
                axis_aligned = dx <= AXIS_TOL or dy <= AXIS_TOL
                assert axis_aligned, (
                    f"Terminus {tid}: edge {r.edge.source}->{tid} "
                    f"last segment ({x1:.1f},{y1:.1f}) -> "
                    f"({x2:.1f},{y2:.1f}) is diagonal "
                    f"(dx={dx:.1f}, dy={dy:.1f})"
                )
                break


# ---------------------------------------------------------------------------
# Station markers and off-track file icons must never overlap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_no_station_or_icon_overlap(fixture):
    """No two station marker bboxes (including off-track file icons)
    may overlap; otherwise one station hides another in the rendered
    SVG."""
    from nf_metro.layout.engine import _station_marker_bbox

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for sid in graph.stations:
        b = _station_marker_bbox(graph, sid, offsets=offsets)
        if b is not None:
            boxes.append((sid, b))

    tol = 0.5
    for i, (s1, (x1, y1, X1, Y1)) in enumerate(boxes):
        for s2, (x2, y2, X2, Y2) in boxes[i + 1 :]:
            overlap = (
                x1 < X2 - tol and x2 < X1 - tol and y1 < Y2 - tol and y2 < Y1 - tol
            )
            assert not overlap, (
                f"{fixture}: marker overlap between {s1!r} "
                f"bbox=({x1:.1f},{y1:.1f},{X1:.1f},{Y1:.1f}) "
                f"and {s2!r} "
                f"bbox=({x2:.1f},{y2:.1f},{X2:.1f},{Y2:.1f})"
            )


# ---------------------------------------------------------------------------
# Non-consumed lines bypass intermediate stations via a virtual station
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_non_consumed_lines_route_via_virtual_station(fixture):
    """A line not consumed by station S must not enter S's marker bbox
    and, when it would otherwise cross S's column, must be routed
    through an invisible (``is_hidden``) virtual station in the same
    section.

    Mirrors the v104 terminus-convergence pattern applied to bypassing:
    inserting a hidden station in S's column at a separate trunk-Y row
    forces the layout to allocate the bypass a parallel-branch track,
    so the path uses the existing fan-out / fan-in primitives.
    """
    from nf_metro.layout.routing import compute_station_offsets, route_edges
    from nf_metro.render.svg import apply_route_offsets

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Identify the bypass case in this fixture: ``annotate`` in the
    # ``differential`` section consumes only rnaseq+affy but maxquant
    # and geo travel from limma to differential's exit port, so they
    # would otherwise route past annotate.  After v110, those lines
    # must enter a hidden station in the same section.
    bypass_station_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    assert bypass_station_ids, (
        f"{fixture}: expected at least one __bypass_ hidden station "
        "from _insert_bypass_stations"
    )

    # For each bypass station, the section_id should be a real
    # (visible) section and the virtual station should not have a
    # rendered marker.  Test by inspecting station attributes.
    for vsid in bypass_station_ids:
        vstation = graph.stations[vsid]
        assert vstation.is_hidden, f"{vsid} should be is_hidden"
        assert not vstation.label, f"{vsid} should have no label"
        assert vstation.section_id is not None, f"{vsid} needs section_id"

    # For the specific differential-section case, verify the maxquant
    # and geo lines are routed via a hidden bypass station and the
    # paths' rendered Y at annotate's X does NOT enter annotate's bbox.
    annotate = graph.stations.get("annotate")
    assert annotate is not None, "fixture must contain ``annotate`` station"

    diff_bypass = [
        sid
        for sid in bypass_station_ids
        if graph.stations[sid].section_id == annotate.section_id
    ]
    assert diff_bypass, (
        f"{fixture}: expected a bypass virtual station in section {annotate.section_id}"
    )

    # The two bypassing lines (maxquant, geo) should each have edges
    # ending at and starting from the same hidden bypass station.
    bypass_predecessors_for = {
        v: {e.source for e in graph.edges if e.target == v} for v in diff_bypass
    }
    bypass_successors_for = {
        v: {e.target for e in graph.edges if e.source == v} for v in diff_bypass
    }
    bypass_lines_for = {
        v: {e.line_id for e in graph.edges if e.source == v} for v in diff_bypass
    }
    # At least one bypass virtual station should carry the
    # non-consumed lines and chain limma -> V -> exit_port.
    found_bypass_for_lines = False
    for v in diff_bypass:
        if {"maxquant", "geo"}.issubset(bypass_lines_for[v]):
            assert "limma" in bypass_predecessors_for[v]
            assert any("exit" in succ for succ in bypass_successors_for[v]), (
                f"{v}: expected an exit-port successor, got {bypass_successors_for[v]}"
            )
            found_bypass_for_lines = True
            break
    assert found_bypass_for_lines, (
        f"{fixture}: expected a bypass V carrying maxquant and geo from "
        f"limma to the differential exit port"
    )

    # Rendered routes for the bypassing lines must not cross annotate's
    # bbox.  Use a half-bbox approximation centered at annotate (x, y).
    HALF_H = 14.0  # pill half-height plus slack
    HALF_W = 14.0  # marker half-width plus slack
    ann_cx = annotate.x
    ann_cy = annotate.y
    rendered = [apply_route_offsets(r, offsets) for r in routes]
    for ri, r in enumerate(routes):
        # Only interested in lines NOT consumed by annotate.
        if r.line_id not in {"maxquant", "geo"}:
            continue
        # Skip routes whose endpoints don't span past annotate.
        if r.edge.source == "annotate" or r.edge.target == "annotate":
            continue
        pts = rendered[ri]
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            xlo, xhi = (x1, x2) if x1 <= x2 else (x2, x1)
            if xhi < ann_cx - HALF_W or xlo > ann_cx + HALF_W:
                continue
            # Linearly interpolate Y at ann_cx along this segment.
            if abs(x2 - x1) < 1e-6:
                seg_y = (y1 + y2) / 2
            else:
                t = (ann_cx - x1) / (x2 - x1)
                t = max(0.0, min(1.0, t))
                seg_y = y1 + t * (y2 - y1)
            assert abs(seg_y - ann_cy) > HALF_H, (
                f"{fixture}: line {r.line_id} enters annotate marker "
                f"bbox at x={ann_cx:.1f}, y={seg_y:.1f} (annotate "
                f"cy={ann_cy:.1f})"
            )


# ---------------------------------------------------------------------------
# Bypass virtual stations must clear off-track input rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_BYPASS)
def test_bypass_avoids_off_track_inputs(fixture):
    """Each ``__bypass_`` virtual station must sit at a Y that clears
    every off-track input icon in its section.

    v110 inserted bypass virtual stations to push non-consumed lines off
    the trunk, but the chosen bypass row could coincide with an off-
    track input's Y, producing a marker collision (e.g. ``grea`` lifted
    to ``gmt_in``'s y=100 in the v106 regression).  Asserting a minimum
    Y separation between each bypass V and every off-track icon in the
    same section locks the clearance.
    """
    graph = _layout(fixture)
    # Marker clearance: off-track icons render at ~10 px tall, bypass
    # virtual stations contribute to line-bundle routing whose track
    # half-width is one ``offset_step`` (~3 px) plus the marker radius
    # (~5 px).  ``y_spacing`` (55 px) is the natural row pitch; we
    # require strictly less than one full row, ie ~12 px or more.
    MIN_CLEARANCE = 12.0
    bypass_ids = [
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    ]
    if not bypass_ids:
        pytest.skip(f"{fixture}: no bypass virtual stations")
    for vsid in bypass_ids:
        v = graph.stations[vsid]
        for sid, st in graph.stations.items():
            if sid == vsid or not st.off_track:
                continue
            if st.section_id != v.section_id:
                continue
            # Different column: clearance not required.
            if abs(st.x - v.x) > 0.5:
                continue
            dy = abs(st.y - v.y)
            assert dy >= MIN_CLEARANCE, (
                f"{fixture}: bypass V {vsid!r} at "
                f"({v.x:.1f},{v.y:.1f}) too close to off-track input "
                f"{sid!r} at ({st.x:.1f},{st.y:.1f}); dy={dy:.1f} "
                f"< MIN_CLEARANCE={MIN_CLEARANCE}"
            )


# ---------------------------------------------------------------------------
# v113: Section 1 below-trunk content has no empty row directly below trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section1_below_trunk_compact(fixture):
    """The first below-trunk content row should sit directly below the
    trunk (no empty row gap).

    Section 1 (Data import and preparation) has below-trunk inputs
    (affy_load, proteus, GEOquery) that previously sat one ``y_spacing``
    slot below the trunk row, leaving an empty row between Samples/
    Contrasts and affy_load.  v113 compacts the below-trunk stack so
    the first row is at ``trunk_y + y_spacing``.
    """
    graph = _layout(fixture)
    sec = graph.sections.get("data_prep")
    assert sec is not None, "fixture must contain data_prep section"
    y_spacing = 55.0

    # Trunk Y: take the LR entry port station's Y (the section's
    # inter-section bundle anchor).
    trunk_y: float | None = None
    for pid in list(sec.entry_ports) + list(sec.exit_ports):
        port = graph.ports.get(pid)
        ps = graph.stations.get(pid)
        if port and ps and port.side in (PortSide.LEFT, PortSide.RIGHT):
            trunk_y = ps.y
            break
    assert trunk_y is not None, "data_prep must have an LR port"

    below_ys = [
        graph.stations[sid].y
        for sid in sec.station_ids
        if sid in graph.stations
        and not graph.stations[sid].is_port
        and not graph.stations[sid].is_hidden
        and graph.stations[sid].y > trunk_y + 0.5
    ]
    assert below_ys, "data_prep should have below-trunk content"
    top_below = min(below_ys)
    gap = top_below - trunk_y
    assert gap < y_spacing + 5.0, (
        f"first below-trunk content row should sit at trunk_y+y_spacing="
        f"{trunk_y + y_spacing:.1f}; got top below at {top_below:.1f} "
        f"(gap {gap:.1f} > {y_spacing + 5.0:.1f})"
    )


# ---------------------------------------------------------------------------
# v113: Fan-out side stations are centred on their loop midpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_fan_station_centered_on_loop(fixture):
    """Each fan-out side station should sit at the midpoint of its
    loop's horizontal run.

    A fan-out side station is fed by one on-trunk predecessor and
    feeds one on-trunk successor, both at the same trunk Y.  v113
    repositions such stations to the midpoint of the two diagonal
    corner Xs so they're not biased toward the fork side.
    """
    from nf_metro.layout.constants import (
        CURVE_RADIUS,
        DIAGONAL_RUN,
        MIN_STRAIGHT_EDGE,
        MIN_STRAIGHT_PORT,
    )
    from nf_metro.layout.labels import label_text_width

    graph = _layout(fixture)

    # Index edges for loop detection.
    out_by_src: dict[str, list] = defaultdict(list)
    in_by_tgt: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        out_by_src[e.source].append(e)
        in_by_tgt[e.target].append(e)

    fork_t: dict[str, set] = defaultdict(set)
    join_s: dict[str, set] = defaultdict(set)
    for e in graph.edges:
        fork_t[e.source].add(e.target)
        join_s[e.target].add(e.source)
    fork_stations = {sid for sid, t in fork_t.items() if len(t) > 1}
    join_stations = {sid for sid, s in join_s.items() if len(s) > 1}

    def _corner(a, b, role: str) -> float:
        sx, tx = a.x, b.x
        sign = 1.0 if tx > sx else -1.0
        src_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if a.is_port else MIN_STRAIGHT_EDGE
        tgt_min = CURVE_RADIUS + MIN_STRAIGHT_PORT if b.is_port else MIN_STRAIGHT_EDGE
        if a.id in fork_stations and a.label.strip():
            src_min = max(src_min, label_text_width(a.label) / 2)
        if b.id in join_stations and b.label.strip():
            tgt_min = max(tgt_min, label_text_width(b.label) / 2)
        half_diag = DIAGONAL_RUN / 2
        if a.id in fork_stations:
            mid = sx + sign * (src_min + half_diag)
        elif b.id in join_stations:
            mid = tx - sign * (tgt_min + half_diag)
        else:
            mid = (sx + tx) / 2.0
        diag_start = mid - sign * half_diag
        diag_end = mid + sign * half_diag
        return diag_end if role == "src" else diag_start

    checked = 0
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden:
            continue
        ins = in_by_tgt.get(sid, [])
        outs = out_by_src.get(sid, [])
        if len(ins) != 1 or len(outs) != 1:
            continue
        src = graph.stations.get(ins[0].source)
        tgt = graph.stations.get(outs[0].target)
        if src is None or tgt is None:
            continue
        if abs(src.y - tgt.y) > 0.5 or abs(st.y - src.y) < 0.5:
            continue
        if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
            continue
        cl = _corner(src, st, role="src")
        cr = _corner(st, tgt, role="tgt")
        midpoint = (cl + cr) / 2.0
        # Allow a small tolerance for grid-snap interactions and
        # subsequent shrink/tighten passes.
        assert abs(st.x - midpoint) <= 2.0, (
            f"loop side station {sid!r} should sit at midpoint "
            f"{midpoint:.1f} of corners ({cl:.1f}, {cr:.1f}); "
            f"got x={st.x:.1f} (delta={st.x - midpoint:+.1f})"
        )
        checked += 1
    assert checked >= 1, f"{fixture}: expected at least one loop side station to test"


# ---------------------------------------------------------------------------
# v113: Section bbox height matches actual content extent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_section_bbox_matches_content_extent(fixture):
    """Each LR/RL section's bbox should hug its content top/bottom.

    The Plots section (a 2-branch symfan placed on half-pitch offsets
    by v110) had a bbox top one full ``y_spacing`` above its content,
    leaving empty space.  v113 shrinks the bbox top for half-grid
    sections so the gap from bbox top to first station equals exactly
    ``section_y_padding``.
    """
    from nf_metro.layout.constants import SECTION_Y_PADDING

    graph = _layout(fixture)
    # Section 4 in da_pipeline is the plots section, alone in row 1.
    sec = graph.sections.get("plots")
    assert sec is not None, "fixture must contain plots section"
    assert sec.bbox_h > 0
    content_ys = [
        graph.stations[sid].y
        for sid in sec.station_ids
        if sid in graph.stations
        and not graph.stations[sid].is_port
        and not graph.stations[sid].is_hidden
    ]
    assert content_ys, "plots section should have content stations"
    top_gap = min(content_ys) - sec.bbox_y
    # Allow padding +/- 1 px slack for float rounding.
    assert abs(top_gap - SECTION_Y_PADDING) <= 1.0, (
        f"plots section top gap should equal SECTION_Y_PADDING="
        f"{SECTION_Y_PADDING}; got {top_gap:.1f}"
    )


# ---------------------------------------------------------------------------
# v113 follow-up: recenter only applies to true loop side-branches.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "topologies/mismatched_tracks.mmd",
        "topologies/variant_calling.mmd",
    ],
)
def test_loop_recenter_only_for_pure_side_branches(fixture):
    """Loop side stations that share their X with an on-trunk co-looper
    must keep that column.

    ``_recenter_loop_side_stations`` moves a side station to the
    midpoint of its loop's diagonal corners.  That's a win for true
    fan-out side stations with their own off-trunk siblings (DA's
    deseq2/dream around limma), but breaks visible column alignment
    when the on-trunk member of the same loop sits at the same X
    (e.g. mismatched tracks ``t_a`` ↔ ``t_b``).  The narrowed pass
    leaves those side stations alone so the on-trunk and off-trunk
    siblings stay column-aligned.
    """
    graph = _layout(fixture)

    out_by_src: dict[str, list] = defaultdict(list)
    in_by_tgt: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        out_by_src[e.source].append(e)
        in_by_tgt[e.target].append(e)

    checked = 0
    for section in graph.sections.values():
        if section.bbox_h <= 0 or section.direction not in ("LR", "RL"):
            continue
        port_ids = set(section.entry_ports) | set(section.exit_ports)
        for sid in section.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            ins = in_by_tgt.get(sid, [])
            outs = out_by_src.get(sid, [])
            if len(ins) != 1 or len(outs) != 1:
                continue
            src = graph.stations.get(ins[0].source)
            tgt = graph.stations.get(outs[0].target)
            if src is None or tgt is None:
                continue
            if abs(src.y - tgt.y) > 0.5:
                continue
            trunk_y = src.y
            if abs(st.y - trunk_y) < 0.5:
                continue
            if not ((src.x < st.x < tgt.x) or (tgt.x < st.x < src.x)):
                continue
            # Find any same-src/tgt sibling that sits on the trunk row.
            # These on-trunk co-loopers anchor a column the off-trunk
            # side station should share.
            on_trunk_sibling_x: float | None = None
            for other_sid in section.station_ids:
                if other_sid == sid:
                    continue
                other = graph.stations.get(other_sid)
                if other is None or other.is_port or other.is_hidden:
                    continue
                if abs(other.y - trunk_y) >= 0.5:
                    continue  # off-trunk, ignore here
                other_ins = in_by_tgt.get(other_sid, [])
                other_outs = out_by_src.get(other_sid, [])
                other_srcs = {e.source for e in other_ins}
                other_tgts = {e.target for e in other_outs}
                if other_srcs == {ins[0].source} and other_tgts == {outs[0].target}:
                    on_trunk_sibling_x = other.x
                    break
            if on_trunk_sibling_x is None:
                continue  # nothing to anchor against
            assert abs(st.x - on_trunk_sibling_x) < 0.5, (
                f"loop side station {sid!r} was recentered off the column "
                f"of its on-trunk co-looper: x={st.x:.1f} vs co-looper "
                f"x={on_trunk_sibling_x:.1f}"
            )
            checked += 1
    assert checked >= 1, (
        f"{fixture}: expected at least one off-trunk loop side station "
        "paired with an on-trunk co-looper"
    )


# ---------------------------------------------------------------------------
# v114: Lines never cross a non-consumer station's marker bbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_BREEZE_PAST),
)
def test_lines_dont_cross_non_consumer_markers(fixture):
    """No rendered line segment may pass through the marker bbox of
    any station that neither consumes nor produces that line.

    Complements ``test_no_station_or_icon_overlap`` (which catches
    marker/marker collisions) with the symmetric line/marker check
    that catches the "breeze-past" pattern: a sparse-consumer
    station S sharing a Y row with a busier sibling whose inbound
    bundle traverses S's column.  Pre-v114 ``grea`` (rnaseq-only)
    sat at the same Y as ``decoupler`` (full bundle), so the lines
    flowing from the section entry to decoupler crossed grea's
    marker on the way in.

    Iterates every (station, route) pair and asserts no segment of
    the route's rendered polyline intersects the station's marker
    bbox when the line is not part of the station's consumed or
    produced set.
    """
    from nf_metro.layout.engine import _station_marker_bbox
    from nf_metro.render.svg import apply_route_offsets

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    consumed_by: dict[str, set[str]] = defaultdict(set)
    produced_by: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        consumed_by[e.target].add(e.line_id)
        produced_by[e.source].add(e.line_id)

    def _seg_crosses_bbox(
        p1: tuple[float, float],
        p2: tuple[float, float],
        bbox: tuple[float, float, float, float],
    ) -> bool:
        x1, y1 = p1
        x2, y2 = p2
        bx1, by1, bx2, by2 = bbox
        if max(x1, x2) < bx1 or min(x1, x2) > bx2:
            return False
        if max(y1, y2) < by1 or min(y1, y2) > by2:
            return False
        for k in range(21):
            f = k / 20.0
            x = x1 + f * (x2 - x1)
            y = y1 + f * (y2 - y1)
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                return True
        return False

    for sid, st in graph.stations.items():
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        station_lines = consumed_by.get(sid, set()) | produced_by.get(sid, set())
        for r in routes:
            if r.line_id in station_lines:
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = apply_route_offsets(r, offsets)
            for k in range(len(pts) - 1):
                if _seg_crosses_bbox(pts[k], pts[k + 1], bbox):
                    raise AssertionError(
                        f"{fixture}: line {r.line_id!r} on edge "
                        f"{r.edge.source!r} -> {r.edge.target!r} "
                        f"crosses non-consumer station {sid!r} "
                        f"marker bbox "
                        f"({bbox[0]:.1f},{bbox[1]:.1f})-"
                        f"({bbox[2]:.1f},{bbox[3]:.1f}); segment "
                        f"({pts[k][0]:.1f},{pts[k][1]:.1f})->"
                        f"({pts[k + 1][0]:.1f},{pts[k + 1][1]:.1f})"
                    )


# ---------------------------------------------------------------------------
# On-track stations must snap to the section trunk Y grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_all_stations_snap_to_grid(fixture):
    """Every on-track station's Y must be at ``trunk_y + k * y_spacing``
    for some integer ``k``.

    Half-grid placement (``trunk_y +/- 0.5 * y_spacing``) is reserved
    for the auto-half-grid 2-branch symmetric fan feature: stations
    registered in ``graph.half_grid_station_ids`` whose section
    satisfies ``_section_symfan_uses_half_grid`` and has exactly two
    on-track branches.  Any other half-grid station is a regression.
    """
    from nf_metro.layout.engine import _section_symfan_uses_half_grid

    y_spacing = 55.0
    tol = 1.0
    graph = _layout(fixture, y_spacing=y_spacing)

    half_grid_ids = graph.half_grid_station_ids
    port_ids: set[str] = set()
    for sec in graph.sections.values():
        port_ids.update(sec.entry_ports)
        port_ids.update(sec.exit_ports)
    junction_ids = set(graph.junctions)

    # Compute each LR/RL section's trunk Y from its LR ports.
    section_trunk_y: dict[str, float] = {}
    for sec in graph.sections.values():
        if sec.direction not in ("LR", "RL") or sec.bbox_h <= 0:
            continue
        port_ys = _section_lr_port_ys(graph, sec)
        if port_ys:
            section_trunk_y[sec.id] = port_ys[0]

    # Sections eligible for the half-grid 2-branch fan exception.
    half_grid_sections = {
        sec.id
        for sec in graph.sections.values()
        if sec.direction in ("LR", "RL")
        and sec.bbox_h > 0
        and _section_symfan_uses_half_grid(graph, sec)
    }

    offenders: list[str] = []
    for sid, st in graph.stations.items():
        if (
            st.is_port
            or st.is_hidden
            or st.off_track
            or sid in port_ids
            or sid in junction_ids
        ):
            continue
        if st.section_id is None:
            continue
        trunk_y = section_trunk_y.get(st.section_id)
        if trunk_y is None:
            continue
        offset = (st.y - trunk_y) / y_spacing
        nearest_int = round(offset)
        on_grid = abs(offset - nearest_int) * y_spacing <= tol
        if on_grid:
            continue
        # Half-grid exception is allowed only for 2-branch fan members
        # whose section legitimately uses the half-grid layout.
        is_half = (
            abs(offset - (nearest_int - 0.5)) * y_spacing <= tol
            or abs(offset - (nearest_int + 0.5)) * y_spacing <= tol
        )
        if is_half and sid in half_grid_ids and st.section_id in half_grid_sections:
            continue
        offenders.append(
            f"{sid!r} cy={st.y:.2f} trunk_y={trunk_y:.2f} "
            f"offset/y_spacing={offset:.3f} "
            f"section={st.section_id!r} "
            f"in_half_grid_ids={sid in half_grid_ids} "
            f"section_uses_half_grid="
            f"{st.section_id in half_grid_sections}"
        )
    assert not offenders, (
        f"{fixture}: on-track stations off the y_spacing grid "
        f"without a legitimate half-grid 2-branch fan exception: "
        + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Bypass V must sit on a visible horizontal flat segment, not at a corner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_BYPASS)
def test_bypass_v_has_horizontal_segment(fixture):
    """Each hidden bypass V station must sit in the middle of a clearly
    visible horizontal flat segment, matching how regular fork/join
    stations present a horizontal run through their X.

    Stronger than ``test_bypass_v_horizontal_segment_is_flat``: that
    test only checks the polyline flat at V's Y is flat in Y, which is
    trivially true even when the flat is zero pixels long because the
    two halves of the U meet at V's X.  Here we assert the polyline
    flat reaches V from at least ``MIN_STATION_FLAT_LENGTH`` pixels
    away (in run-axis X) on each side, so that after the curve corner
    consumes ``CURVE_RADIUS`` pixels, a visible flat of
    ``MIN_STATION_FLAT_LENGTH - CURVE_RADIUS`` pixels remains on each
    side of V (matching e.g. propd / dream / DESeq2).
    """
    from nf_metro.layout.constants import MIN_STATION_FLAT_LENGTH

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    bypass_v_ids = {
        sid
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    }
    assert bypass_v_ids, f"{fixture}: expected at least one __bypass_ virtual station"

    by_v_line: dict[tuple[str, str], list] = defaultdict(list)
    for r in routes:
        if r.edge.source in bypass_v_ids:
            by_v_line[(r.edge.source, r.line_id)].append(("out", r))
        if r.edge.target in bypass_v_ids:
            by_v_line[(r.edge.target, r.line_id)].append(("in", r))

    tol = 0.5
    checked = 0
    for (vid, lid), pair in by_v_line.items():
        if len(pair) != 2:
            continue
        in_route = next((r for kind, r in pair if kind == "in"), None)
        out_route = next((r for kind, r in pair if kind == "out"), None)
        if in_route is None or out_route is None:
            continue

        # P -> V: last two polyline points (-2, -1) form the flat
        # segment landing at V.  Its length is what reaches V in X
        # before the curve corner consumes CURVE_RADIUS pixels.
        left_flat = abs(in_route.points[-1][0] - in_route.points[-2][0])
        # V -> T: first two polyline points form the flat leaving V.
        right_flat = abs(out_route.points[1][0] - out_route.points[0][0])

        assert left_flat >= MIN_STATION_FLAT_LENGTH - tol, (
            f"{fixture}: bypass {vid!r} line {lid!r}: P->V flat segment "
            f"too short to render a visible horizontal run through V "
            f"(left_flat={left_flat:.2f}px, "
            f"MIN_STATION_FLAT_LENGTH={MIN_STATION_FLAT_LENGTH}px); "
            f"V would sit at the curve apex instead of on a visible "
            f"horizontal flat like regular stations"
        )
        assert right_flat >= MIN_STATION_FLAT_LENGTH - tol, (
            f"{fixture}: bypass {vid!r} line {lid!r}: V->T flat segment "
            f"too short to render a visible horizontal run through V "
            f"(right_flat={right_flat:.2f}px, "
            f"MIN_STATION_FLAT_LENGTH={MIN_STATION_FLAT_LENGTH}px)"
        )
        checked += 1

    assert checked > 0, (
        f"{fixture}: expected at least one paired bypass V edge to verify"
    )


# ---------------------------------------------------------------------------
# Loop-column stations share X (trunk + off-trunk siblings co-aligned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_loop_column_stations_share_x(fixture):
    """Every station in a loop column (defined by its trunk-Y
    horizontal extent) must share an X with the column's other clean
    members.

    A "loop column" groups stations within an LR/RL section by the
    pair ``(rightmost trunk-Y predecessor X, leftmost trunk-Y
    successor X)``.  A station counts as a column member when:

    - all its visible predecessors and successors sit on the
      section's trunk Y (no off-track inputs that would pull its X
      away from the column), and
    - either it has a single inbound edge and a single outbound
      edge (a "clean" off-trunk side station, mirrored at the loop
      midpoint by ``_recenter_loop_side_stations`` pass 1), or it
      sits ON the trunk row (the column's trunk station, which pass
      2 snaps onto the clean-sibling midpoint).

    Catches the v115 regression where ``limma`` sat at the raw
    layer X (e.g. 629.4) while its off-trunk siblings ``propd``,
    ``dream`` and ``DESeq2`` had been recentered to the column
    midpoint (~648.6).
    """
    from nf_metro.parser.model import PortSide

    graph = _layout(fixture)

    in_by_tgt: dict[str, list] = defaultdict(list)
    out_by_src: dict[str, list] = defaultdict(list)
    for e in graph.edges:
        in_by_tgt[e.target].append(e)
        out_by_src[e.source].append(e)

    checked_columns = 0
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.direction not in ("LR", "RL"):
            continue
        trunk_y: float | None = None
        for pid in sec.entry_ports + sec.exit_ports:
            ps = graph.stations.get(pid)
            port = graph.ports.get(pid)
            if (
                ps is not None
                and port is not None
                and port.side in (PortSide.LEFT, PortSide.RIGHT)
            ):
                trunk_y = ps.y
                break
        if trunk_y is None:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)

        # Group eligible stations by (pred_x, succ_x).
        columns: dict[tuple[float, float], list[str]] = defaultdict(list)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            visible_ins = [
                e
                for e in in_by_tgt.get(sid, [])
                if (
                    (gs := graph.stations.get(e.source)) is not None
                    and not gs.is_hidden
                )
            ]
            visible_outs = [
                e
                for e in out_by_src.get(sid, [])
                if (
                    (gs := graph.stations.get(e.target)) is not None
                    and not gs.is_hidden
                )
            ]
            # All visible neighbours must be on trunk Y; otherwise an
            # off-track input anchors this station elsewhere.
            ok = True
            for e in visible_ins:
                if abs(graph.stations[e.source].y - trunk_y) > 0.5:
                    ok = False
                    break
            if not ok:
                continue
            for e in visible_outs:
                if abs(graph.stations[e.target].y - trunk_y) > 0.5:
                    ok = False
                    break
            if not ok:
                continue
            if not visible_ins or not visible_outs:
                continue
            # Eligibility: clean side station (1 edge in, 1 edge out
            # AND off-trunk) OR trunk-Y station.
            on_trunk = abs(st.y - trunk_y) <= 0.5
            clean_side = (
                not on_trunk and len(visible_ins) == 1 and len(visible_outs) == 1
            )
            if not (on_trunk or clean_side):
                continue
            # Column key: rightmost trunk-Y predecessor X (LR), or
            # leftmost (RL); leftmost trunk-Y successor X (LR), or
            # rightmost (RL).
            if sec.direction == "LR":
                pred_x = max(graph.stations[e.source].x for e in visible_ins)
                succ_x = min(graph.stations[e.target].x for e in visible_outs)
            else:
                pred_x = min(graph.stations[e.source].x for e in visible_ins)
                succ_x = max(graph.stations[e.target].x for e in visible_outs)
            # Station must sit strictly between its trunk-Y
            # neighbours.
            lo, hi = min(pred_x, succ_x), max(pred_x, succ_x)
            if not (lo < st.x < hi):
                continue
            columns[(round(pred_x, 3), round(succ_x, 3))].append(sid)

        for key, members in columns.items():
            if len(members) < 2:
                continue
            xs = [graph.stations[sid].x for sid in members]
            spread = max(xs) - min(xs)
            member_xs = [(sid, round(graph.stations[sid].x, 2)) for sid in members]
            assert spread <= 1.0, (
                f"{fixture}: section {sec.id!r} loop column {key}: "
                f"members {member_xs} span {spread:.2f}px (>1px); "
                f"trunk + clean siblings should share X"
            )
            checked_columns += 1

    assert checked_columns >= 1, (
        f"{fixture}: expected at least one loop column with multiple members to verify"
    )


# ---------------------------------------------------------------------------
# Section bbox bottom padding (Stage 6.14 post-shift padding)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_BBOX_BOTTOM_PAD),
)
def test_section_bbox_has_bottom_padding(fixture):
    """Each section's bbox bottom must sit at least ``section_y_padding``
    below the centre Y of its lowest internal station.

    The codebase convention (see ``_shrink_bboxes_to_content_bottom``)
    measures bottom padding from the station's centre Y, not its marker
    edge, so the invariant is ``bbox_bot >= max(station.y) +
    section_y_padding``.

    ``_shift_and_propagate_loop_stations`` (Stage 6.14) can
    move a sparse loop station like ``grea`` further down without
    restoring this padding.  Catches the v116 regression where
    section 3's bbox sat ~5px below ``grea``'s centre instead of
    ``section_y_padding`` (50px).
    """
    from nf_metro.layout.constants import SECTION_Y_PADDING

    graph = _layout(fixture)
    tol = 1.0

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        internal_ys = [
            graph.stations[sid].y
            for sid in sec.station_ids
            if sid in graph.stations
            and sid not in port_ids
            and not graph.stations[sid].is_hidden
        ]
        if not internal_ys:
            continue
        lowest_marker_cy = max(internal_ys)
        bbox_bot = sec.bbox_y + sec.bbox_h
        gap = bbox_bot - lowest_marker_cy
        if gap + tol < SECTION_Y_PADDING:
            offenders.append(
                f"section {sec.id!r}: bbox bot={bbox_bot:.1f}, "
                f"lowest marker cy={lowest_marker_cy:.1f}, "
                f"gap={gap:.1f} < section_y_padding={SECTION_Y_PADDING}"
            )

    assert not offenders, (
        f"{fixture}: section bbox bottoms must sit at least "
        f"section_y_padding below the lowest station centre: " + "; ".join(offenders)
    )


# Section bbox top doesn't carry the configured section_y_padding above
# the highest station marker (the mirror of bottom padding).  Affects
# sections whose content fans above the trunk: a fan-redistribution pass
# lifts a station above the content-top line the bbox was sized for,
# crowding the topmost marker against the bbox top while the bottom keeps
# its full band.  Sections gap-bounded against the row above (where full
# top padding would crowd the section-header badge against an inter-row
# route) belong here as xfails.
_XFAIL_BBOX_TOP_PAD: dict[str, str] = {
    "differentialabundance_default.mmd": (
        "plots is gap-bounded: growing its top to a full padding band would "
        "bring its section-header badge within the inter-row route clearance "
        "(test_routed_paths_clear_next_row_headers), so the top-padding "
        "restore deliberately stops short. Revisit if the row gap or routing "
        "changes."
    ),
}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_BBOX_TOP_PAD),
)
def test_section_bbox_has_top_padding(fixture):
    """Each section's bbox top must sit at least ``section_y_padding``
    above the centre Y of its highest internal station.

    Symmetric counterpart to ``test_section_bbox_has_bottom_padding``.
    The codebase convention measures padding from the station's centre
    Y, so the invariant is ``bbox_top <= min(station.y) -
    section_y_padding``.

    Fan-redistribution passes (Stages 4.9 / 4.10 / 6.7 / 6.11) lift a
    branch station above the trunk after the section bbox was sized for
    the pre-fan content extent.  Without a top-padding restore the
    topmost marker sits ~10px from the bbox top while the bottom keeps
    its full 50px, leaving the box visibly uncentred about the trunk
    (issue #406).
    """
    from nf_metro.layout.constants import SECTION_Y_PADDING

    graph = _layout(fixture)
    tol = 1.0

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        internal_ys = [
            graph.stations[sid].y
            for sid in sec.station_ids
            if sid in graph.stations
            and sid not in port_ids
            and not graph.stations[sid].is_hidden
        ]
        if not internal_ys:
            continue
        highest_marker_cy = min(internal_ys)
        gap = highest_marker_cy - sec.bbox_y
        if gap + tol < SECTION_Y_PADDING:
            offenders.append(
                f"section {sec.id!r}: bbox top={sec.bbox_y:.1f}, "
                f"highest marker cy={highest_marker_cy:.1f}, "
                f"gap={gap:.1f} < section_y_padding={SECTION_Y_PADDING}"
            )

    assert not offenders, (
        f"{fixture}: section bbox tops must sit at least "
        f"section_y_padding above the highest station centre: " + "; ".join(offenders)
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_section_bbox_top_hugs_content(fixture):
    """Empty-band sections hug content to an EQUALITY, not just a floor.

    A section with a genuinely empty flush band -- no port and no
    ``__bypass_`` helper above its topmost content
    (:func:`_section_band_is_empty`) -- has its bbox top sit exactly
    ``section_y_padding`` above the highest content marker, leaving no
    empty band.  This is the equality companion to the ``>=`` floor
    invariant ``test_section_bbox_has_top_padding``.

    Ceiling-bound sections (where the row-above grow ceiling raises
    :func:`_section_fit_top` above the ceiling-free
    :func:`_section_content_hug_top`) legitimately keep a band so the
    header badge clears the inter-row routing; they are skipped here and
    covered by the floor test instead.
    """
    graph = _layout(fixture)
    tol = 1.0

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or not _section_band_is_empty(graph, sec):
            continue
        fit = _section_fit_top(graph, sec, SECTION_Y_PADDING, SECTION_Y_GAP)
        hug = _section_content_hug_top(graph, sec, SECTION_Y_PADDING)
        # fit > hug means the row-above ceiling raised the top above the
        # content-hug, so a band is reserved for the header badge.
        if fit is None or hug is None or fit > hug + tol:
            continue
        content_top = min(
            graph.stations[sid].y
            for sid in sec.station_ids
            if not graph.stations[sid].is_port and not sid.startswith("__bypass_")
        )
        gap = content_top - sec.bbox_y
        if abs(gap - SECTION_Y_PADDING) > tol:
            offenders.append(
                f"section {sec.id!r}: gap={gap:.1f} != "
                f"section_y_padding={SECTION_Y_PADDING} "
                f"(leftover band {gap - SECTION_Y_PADDING:.1f})"
            )

    assert not offenders, (
        f"{fixture}: section tops with an empty band must hug content to "
        f"section_y_padding with no leftover space: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Inter-row gap accommodates grown bboxes from Stage 6.14 shifts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_row_gap_accommodates_bypass(fixture):
    """The vertical gap between row ``r`` sections' bbox bottoms and
    row ``r + 1`` sections' bbox tops must be at least
    ``section_y_gap`` for every column-overlapping pair.

    When ``_shift_and_propagate_loop_stations`` grows an
    upper-row section's bbox downward (e.g. section 3 in the
    differentialabundance pipeline, around ``grea``), the row offset
    computed by ``_compute_section_offsets`` from the pre-shift bbox
    height is no longer enough; the lower row must be pushed down so
    routing has room between the new bbox bottom and the next row's
    header.  Catches the v116 regression where section 4 (plots) sat
    only ~40px below section 3's grown bbox bottom.

    Tested at ``y_spacing=55`` because the production render uses that
    pitch; the default ``y_spacing=40`` happens to leave the bbox
    growth absorbed by row-0's taller rowspan section, hiding the
    regression.
    """
    from nf_metro.layout.constants import SECTION_Y_GAP

    graph = _layout(fixture, y_spacing=55)
    tol = 1.0

    by_row: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        by_row[sec.grid_row + sec.grid_row_span - 1].append(sec)
    starting_at: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        starting_at[sec.grid_row].append(sec)

    def _cols_overlap(a, b) -> bool:
        a_start = a.grid_col
        a_end = a_start + a.grid_col_span - 1
        b_start = b.grid_col
        b_end = b_start + b.grid_col_span - 1
        return not (a_end < b_start or b_end < a_start)

    offenders: list[str] = []
    if not by_row:
        return
    max_row = max(by_row)
    for r in range(max_row):
        upper = by_row.get(r, [])
        lower = starting_at.get(r + 1, [])
        for us in upper:
            for ls in lower:
                if not _cols_overlap(us, ls):
                    continue
                upper_bot = us.bbox_y + us.bbox_h
                lower_top = ls.bbox_y
                gap = lower_top - upper_bot
                if gap + tol < SECTION_Y_GAP:
                    offenders.append(
                        f"rows {r}->{r + 1} col-overlap "
                        f"{us.id!r} (bot={upper_bot:.1f}) -> "
                        f"{ls.id!r} (top={lower_top:.1f}): "
                        f"gap={gap:.1f} < section_y_gap={SECTION_Y_GAP}"
                    )

    assert not offenders, (
        f"{fixture}: row gap must be >= section_y_gap for every "
        f"column-overlapping pair: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Auto y_spacing must fit the worst-case content in every LR/RL section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "example",
    [
        "differentialabundance.mmd",
        "differentialabundance_default.mmd",
        "rnaseq_auto.mmd",
        "rnaseq_sections.mmd",
        "variantprioritization.mmd",
        "genomeassembly.mmd",
    ],
)
def test_auto_y_spacing_fits_content(example):
    """With the engine's auto-derived ``y_spacing``, no two on-track
    stations in the same LR/RL section column may stack tighter than the
    chosen pitch.

    Captioned file-icon stations sit at fixed offsets relative to their
    station marker (caption below the icon, icon centred on the marker)
    so the row pitch must accommodate the worst stacking case; the
    invariant fails if any captioned station's caption would overlap
    the next station's icon or label.

    Also verifies that ``compute_min_y_spacing`` is not below the floor
    and that the historical default-content cases (small simple maps)
    don't widen unnecessarily.
    """
    from nf_metro.layout.constants import MIN_Y_SPACING_FLOOR
    from nf_metro.layout.engine import compute_min_y_spacing

    graph = _layout_example(example)  # y_spacing=None -> auto

    y_spacing = compute_min_y_spacing(graph)
    assert y_spacing >= MIN_Y_SPACING_FLOOR, (
        f"{example}: auto y_spacing {y_spacing:.2f} below floor {MIN_Y_SPACING_FLOOR}"
    )

    # No two on-track stations in the same section column may sit
    # tighter than the chosen pitch.
    port_ids: set[str] = set()
    for sec in graph.sections.values():
        port_ids.update(sec.entry_ports)
        port_ids.update(sec.exit_ports)
    junction_ids = set(graph.junctions)

    by_section_x: dict[tuple[str, float], list[tuple[float, str]]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if (
            st.is_port
            or st.is_hidden
            or st.off_track
            or sid in port_ids
            or sid in junction_ids
            or st.section_id is None
        ):
            continue
        sec = graph.sections.get(st.section_id)
        if sec is None or sec.direction not in ("LR", "RL"):
            continue
        by_section_x[(st.section_id, round(st.x, 1))].append((st.y, sid))

    tol = 1.0
    offenders: list[str] = []
    for (sec_id, xx), entries in by_section_x.items():
        entries.sort()
        for i in range(len(entries) - 1):
            y1, s1 = entries[i]
            y2, s2 = entries[i + 1]
            gap = y2 - y1
            if gap + tol < y_spacing:
                offenders.append(
                    f"sec={sec_id} x={xx} {s1}@{y1:.1f} -> {s2}@{y2:.1f} "
                    f"gap={gap:.2f} < y_spacing={y_spacing:.2f}"
                )

    assert not offenders, (
        f"{example}: stations stacked tighter than auto y_spacing "
        f"({y_spacing:.2f}); each pair would risk caption/label "
        f"overlap: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Off-track / single-icon terminus icons must not be crossed by routed paths
# ---------------------------------------------------------------------------


def _icon_half_height_default() -> float:
    """Vertical reach (half-height) of a file-input icon.  Mirrors the
    renderer's default ``terminus_height`` of 32 px.
    """
    return 16.0


def _icon_x_extent(graph: MetroGraph, station, section) -> tuple[float, float]:
    """Approximate the rendered X span of an off-track / single-icon
    terminus station's file icon.  Mirrors the renderer placement:
    ``icon_cx = station.x +/- (radius + ICON_STATION_GAP + width/2)``.
    """
    r = 5.0  # station_radius
    icon_gap = 5.0  # ICON_STATION_GAP
    icon_half_w = 14.0  # terminus_width / 2 = 28 / 2
    icon_step = icon_gap + r + icon_half_w
    is_source = not any(e.target == station.id for e in graph.edges)
    if section.direction == "RL":
        icons_go_right = is_source
    else:
        icons_go_right = not is_source
    cx = station.x + icon_step if icons_go_right else station.x - icon_step
    return cx - icon_half_w, cx + icon_half_w


# a multi-row collector fan-in now descends the inter-column corridor into the
# left-entry ``reporting`` section instead of sweeping the reporting row's
# full width, so the merge bundle no longer crosses the file icons (#432).
_XFAIL_ICON_OVERLAP: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture", _params_with_xfails(ALL_FIXTURES, _XFAIL_ICON_OVERLAP)
)
def test_no_icon_overlaps_line_path(fixture):
    """A station's rendered file icon must not be crossed by routed line
    segments belonging to lines the station neither produces nor consumes.

    The renderer offsets file icons from the station pill; when the icon
    sits where an unrelated line's routed polyline passes through, the
    rendered SVG shows a track crossing the icon.  Catches the DA-render
    section 3 bad-params regression where the network icon was crossed
    by trunk lines heading from the entry port to ``gsea``.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    icon_half = _icon_half_height_default()
    XY_TOL = 1.0

    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden:
            continue
        icon_count = len(st.terminus_labels or [])
        if not (st.off_track or (st.is_terminus and icon_count == 1)):
            continue
        section = graph.sections.get(st.section_id)
        if section is None or section.direction not in ("LR", "RL"):
            continue
        icon_top = st.y - icon_half
        icon_bot = st.y + icon_half
        icon_xl, icon_xr = _icon_x_extent(graph, st, section)

        for r in routes:
            # The icon's own routes legitimately enter the icon column.
            if r.edge.source == sid or r.edge.target == sid:
                continue
            src = graph.stations.get(r.edge.source)
            tgt = graph.stations.get(r.edge.target)
            if src is None or tgt is None:
                continue
            # Only consider routes that traverse the icon's section.
            if st.section_id not in (src.section_id, tgt.section_id):
                continue
            pts = r.points
            for i in range(len(pts) - 1):
                x1, y1 = pts[i]
                x2, y2 = pts[i + 1]
                # Skip strictly vertical segments (they may legitimately
                # route around the icon).
                if abs(x2 - x1) < 1e-3:
                    continue
                xlo, xhi = (x1, x2) if x1 <= x2 else (x2, x1)
                if xhi < icon_xl + XY_TOL or xlo > icon_xr - XY_TOL:
                    continue
                xmid = max(xlo, icon_xl + XY_TOL)
                xmid = min(xmid, icon_xr - XY_TOL)
                if abs(x2 - x1) < 1e-6:
                    seg_y = (y1 + y2) / 2
                else:
                    t = (xmid - x1) / (x2 - x1)
                    t = max(0.0, min(1.0, t))
                    seg_y = y1 + t * (y2 - y1)
                if icon_top + XY_TOL <= seg_y <= icon_bot - XY_TOL:
                    raise AssertionError(
                        f"{fixture}: icon for station {sid!r} "
                        f"(y={st.y:.1f}, "
                        f"bbox y={icon_top:.1f}..{icon_bot:.1f}, "
                        f"x={icon_xl:.1f}..{icon_xr:.1f}) crossed by "
                        f"route {r.edge.source}->{r.edge.target} line "
                        f"{r.line_id!r} at seg "
                        f"({x1:.1f},{y1:.1f})->({x2:.1f},{y2:.1f}) "
                        f"crossing y={seg_y:.1f}"
                    )


# ---------------------------------------------------------------------------
# Fan-out branches in the same column must land at distinct Y rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_fanout_branches_at_distinct_y(fixture):
    """When a station fans out to multiple in-section successors at the
    same X column, each branch must land at a distinct Y row.

    Catches the DA-render Reporting regression where Quarto fanned out
    to ``bundle`` and ``report_html`` at the same Y, so the report_html
    terminus icon overlapped ``bundle``'s station marker.
    """
    graph = _layout(fixture)
    by_source_col: dict[tuple[str, float], list[str]] = defaultdict(list)
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if src is None or tgt is None:
            continue
        if src.is_port or tgt.is_port or tgt.is_hidden:
            continue
        if src.section_id != tgt.section_id:
            continue
        by_source_col[(edge.source, round(tgt.x, 1))].append(edge.target)
    for (src_id, col_x), targets in by_source_col.items():
        unique_targets = list(dict.fromkeys(targets))
        if len(unique_targets) < 2:
            continue
        ys = [(tid, graph.stations[tid].y) for tid in unique_targets]
        for i, (t1, y1) in enumerate(ys):
            for t2, y2 in ys[i + 1 :]:
                assert abs(y1 - y2) > _Y_TOL, (
                    f"{fixture}: fan-out from {src_id!r} to {t1!r} "
                    f"(y={y1}) and {t2!r} (y={y2}) at column x={col_x} - "
                    f"both land at the same Y row"
                )


# ---------------------------------------------------------------------------
# Bypass V clearance from the next-row section header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_BYPASS)
def test_bypass_clearance_from_lower_section(fixture):
    """A bypass virtual station that sits below its section's bbox bottom
    must leave at least ``SECTION_Y_GAP`` of clearance to the lower
    section's bbox top.

    Catches the param-dependent regression where bypass routing grows
    the upper section's effective extent (via bypass Y) below
    ``bbox_bottom``, but the lower row was placed using only the
    ``bbox_bottom`` geometry, so the lower section's header visually
    crowds the bypass V.
    """
    from nf_metro.layout.constants import SECTION_Y_GAP

    graph = _layout(fixture)
    bypass_stations = [
        st
        for sid, st in graph.stations.items()
        if st.is_hidden and sid.startswith("__bypass_")
    ]
    if not bypass_stations:
        pytest.skip(f"{fixture}: no bypass virtual stations")
    tol = 1.0
    for v in bypass_stations:
        v_sec = graph.sections.get(v.section_id)
        if v_sec is None or v_sec.bbox_h <= 0:
            continue
        v_sec_bot = v_sec.bbox_y + v_sec.bbox_h
        effective_bot = max(v_sec_bot, v.y)
        v_end_row = v_sec.grid_row + v_sec.grid_row_span - 1
        for ls in graph.sections.values():
            if ls.bbox_h <= 0:
                continue
            if ls.grid_row != v_end_row + 1:
                continue
            a_s = v_sec.grid_col
            a_e = a_s + v_sec.grid_col_span - 1
            b_s = ls.grid_col
            b_e = b_s + ls.grid_col_span - 1
            if a_e < b_s or b_e < a_s:
                continue
            gap = ls.bbox_y - effective_bot
            assert gap + tol >= SECTION_Y_GAP, (
                f"{fixture}: bypass V at y={v.y:.1f} in section "
                f"{v_sec.id!r} (bot={v_sec_bot:.1f}) crowds lower "
                f"section {ls.id!r} (top={ls.bbox_y:.1f}); "
                f"effective_bot={effective_bot:.1f}, gap={gap:.1f} "
                f"< SECTION_Y_GAP={SECTION_Y_GAP}"
            )


# ---------------------------------------------------------------------------
# Inter-section routed paths must clear next-row section headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_routed_paths_clear_next_row_headers(fixture):
    """Every horizontal-or-near-horizontal segment of an inter-section
    routed path must stay clear of any next-row section header.

    Bypass routes (cross-column edges with intervening same-row sections)
    dip into the inter-row gap below the intervening bbox bottom.  When
    the next row's section header (number badge + label) protrudes
    ``SECTION_HEADER_PROTRUSION`` above its bbox, an inter-row routed
    segment passing through the same column range can visually crowd the
    badge.  The section-placement-side ``_predicted_bypass_bottom_in_row``
    floor exists specifically to keep these from colliding.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Collect (header_top_y, x_lo, x_hi, section_id) for every section
    # whose header could be crowded from above.
    headers: list[tuple[float, float, float, str]] = []
    for sid, sec in graph.sections.items():
        if sec.bbox_h <= 0 or sec.bbox_w <= 0:
            continue
        header_top = sec.bbox_y - SECTION_HEADER_PROTRUSION
        headers.append((header_top, sec.bbox_x, sec.bbox_x + sec.bbox_w, sid))

    # Must exceed the stacked-bundle half-width (~6px for 4 lines at
    # OFFSET_STEP=3) while staying under TOP-entry channel routes that
    # legitimately sit ~14px above the badge.
    min_clearance = 12.0
    h_axis_tol = 2.0
    for r in routes:
        if not r.is_inter_section:
            continue
        pts = r.points
        if len(pts) < 2:
            continue
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if abs(y2 - y1) > h_axis_tol:
                continue
            seg_y = (y1 + y2) / 2
            seg_x_lo = min(x1, x2)
            seg_x_hi = max(x1, x2)
            for header_top, hx_lo, hx_hi, hsid in headers:
                if seg_x_hi <= hx_lo or seg_x_lo >= hx_hi:
                    continue
                # Only consider headers strictly below this segment.
                if header_top <= seg_y:
                    continue
                gap = header_top - seg_y
                assert gap + 0.5 >= min_clearance, (
                    f"{fixture}: inter-section routed segment "
                    f"({x1:.1f},{y1:.1f})->({x2:.1f},{y2:.1f}) "
                    f"of edge {r.edge.source!r}->{r.edge.target!r} "
                    f"sits {gap:.1f}px above header of section "
                    f"{hsid!r} (header_top={header_top:.1f}), "
                    f"below required clearance "
                    f"{min_clearance:.1f}px"
                )


# ---------------------------------------------------------------------------
# Section entry hubs must sit on the row Y grid (audit item 12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_section_entry_hub_on_grid(fixture):
    """Section entry/exit hub stations (``_hub`` suffix) must sit on the
    row's Y grid (integer or half-integer multiple of ``y_spacing``
    relative to the section's trunk Y).

    The existing ``test_stations_on_grid`` invariant explicitly exempts
    hubs.  This is the corresponding affirmative check: replace the
    blanket exemption with a real assertion so off-grid hubs surface
    as failures instead of being silently allowed.
    """
    y_spacing = 55.0
    tol = 1.0
    graph = _layout(fixture, y_spacing=y_spacing)

    section_trunk_y: dict[str, float] = {}
    for sec in graph.sections.values():
        if sec.direction not in ("LR", "RL") or sec.bbox_h <= 0:
            continue
        for pid in list(sec.entry_ports) + list(sec.exit_ports):
            port = graph.ports.get(pid)
            st = graph.stations.get(pid)
            if port and st and port.side in (PortSide.LEFT, PortSide.RIGHT):
                section_trunk_y[sec.id] = st.y
                break

    offenders: list[str] = []
    for sid, st in graph.stations.items():
        if "_hub" not in sid:
            continue
        if st.section_id is None:
            continue
        trunk_y = section_trunk_y.get(st.section_id)
        if trunk_y is None:
            continue
        offset = (st.y - trunk_y) / y_spacing
        nearest_int = round(offset)
        on_grid = abs(offset - nearest_int) * y_spacing <= tol
        is_half = (
            abs(offset - (nearest_int - 0.5)) * y_spacing <= tol
            or abs(offset - (nearest_int + 0.5)) * y_spacing <= tol
        )
        if not (on_grid or is_half):
            offenders.append(
                f"{sid!r} cy={st.y:.2f} trunk_y={trunk_y:.2f} "
                f"offset/y_spacing={offset:.3f} "
                f"section={st.section_id!r}"
            )
    assert not offenders, (
        f"{fixture}: hub stations off the y_spacing grid: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Inter-section routes between same-row sections stay in the row's Y band
# (audit items 6 and 18 / issue #317)
# ---------------------------------------------------------------------------


# Fixtures known to fail ``test_inter_section_route_y_stays_within_row_band``
# because a same-row inter-section route dips outside its row band.
_XFAIL_ROW_BAND: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_ROW_BAND),
)
def test_inter_section_route_y_stays_within_row_band(fixture):
    """Inter-section routes whose endpoints both sit in grid row R must
    keep all waypoint Ys within a one-row vertical band centered on R.

    Catches the variantbenchmarking case (issue #317) where 3-4 and 4-5
    inter-section bands dipped 250+ px through the lower-row Y band.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    # Compute Y bounds per grid row from rowspan=1 sections.
    row_band: dict[int, tuple[float, float]] = {}
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.grid_row_span != 1:
            continue
        cur = row_band.get(sec.grid_row)
        top = sec.bbox_y
        bot = sec.bbox_y + sec.bbox_h
        if cur is None:
            row_band[sec.grid_row] = (top, bot)
        else:
            row_band[sec.grid_row] = (min(cur[0], top), max(cur[1], bot))

    # Slack: a clean below-row wrap channel (bypass clearance + bundle
    # nest + diagonal corner approach), shared with the runtime band guard.
    SLACK = ROW_BAND_SLACK

    offenders: list[str] = []
    for r in routes:
        src = graph.stations.get(r.edge.source)
        tgt = graph.stations.get(r.edge.target)
        if src is None or tgt is None:
            continue
        if src.section_id is None or tgt.section_id is None:
            continue
        if src.section_id == tgt.section_id:
            continue
        sec_a = graph.sections.get(src.section_id)
        sec_b = graph.sections.get(tgt.section_id)
        if sec_a is None or sec_b is None:
            continue
        if sec_a.grid_row != sec_b.grid_row:
            continue
        if sec_a.grid_row_span != 1 or sec_b.grid_row_span != 1:
            continue
        band = row_band.get(sec_a.grid_row)
        if band is None:
            continue
        lo, hi = band[0] - SLACK, band[1] + SLACK
        for _x, y in r.points:
            if y < lo or y > hi:
                offenders.append(
                    f"route {r.edge.source}->{r.edge.target} "
                    f"line {r.line_id!r} at y={y:.1f} outside "
                    f"row-{sec_a.grid_row} band {lo:.1f}..{hi:.1f}"
                )
                break
        if len(offenders) > 5:
            break
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:5])


# ---------------------------------------------------------------------------
# Topologically-equivalent siblings share Y or sit symmetrically
# (audit item 15 / issue #453)
# ---------------------------------------------------------------------------


# Fixtures known to fail ``test_topological_siblings_share_y_or_symmetric``
# (audit item 15).  The sibling-Y defect this dict tracked is resolved, so
# no fixture is currently exempted; the invariant now holds gallery-wide.
_XFAIL_SIBLINGS: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_SIBLINGS),
)
def test_topological_siblings_share_y_or_symmetric(fixture):
    """Stations with identical ``(predecessor_set, successor_set,
    line_set)`` should share Y, or for >= 3 members be symmetrically
    distributed around their mean Y.

    Catches the audit-15 defect (tracked in #453): gatk and deepvariant
    have the same predecessors, successors, and consumed lines but end up
    at different Ys when they should be mirrored around the trunk.
    """
    graph = _layout(fixture)
    preds: dict[str, set[str]] = defaultdict(set)
    succs: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        preds[e.target].add(e.source)
        succs[e.source].add(e.target)
    classes: dict[tuple[frozenset[str], frozenset[str], frozenset[str]], list[str]] = (
        defaultdict(list)
    )
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or st.off_track:
            continue
        if not preds[sid] or not succs[sid]:
            continue
        line_set = frozenset(graph.station_lines(sid))
        key = (frozenset(preds[sid]), frozenset(succs[sid]), line_set)
        classes[key].append(sid)
    offenders: list[str] = []
    for _key, members in classes.items():
        if len(members) < 2:
            continue
        ys = sorted(graph.stations[s].y for s in members)
        if max(ys) - min(ys) < 2.0:
            continue
        xs = [graph.stations[s].x for s in members]
        if max(xs) - min(xs) < 2.0:
            continue
        if len(members) == 2:
            offenders.append(
                f"siblings {members} ys={ys} differ by {max(ys) - min(ys):.1f}"
            )
        else:
            mean_y = sum(ys) / len(ys)
            symmetric = True
            for y in ys:
                mirror = 2 * mean_y - y
                if not any(abs(other - mirror) < 2.0 for other in ys):
                    symmetric = False
                    break
            if not symmetric:
                offenders.append(
                    f"siblings {members} ys={ys} not symmetric around mean {mean_y:.1f}"
                )
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# Layout is deterministic in X (audit item 11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_grid_snap_does_not_mutate_x(fixture):
    """Re-running the full layout pipeline on the same fixture must
    produce identical station X coordinates.

    The grid-snap phase is supposed to act on Y only; a regression where
    it (or any subsequent phase) introduced non-determinism into X would
    surface here as a per-station mismatch between the two runs.
    """
    g1 = _layout(fixture)
    g2 = _layout(fixture)
    offenders: list[str] = []
    for sid, st1 in g1.stations.items():
        st2 = g2.stations.get(sid)
        if st2 is None:
            continue
        if abs(st1.x - st2.x) > 0.5:
            offenders.append(f"{sid!r} run1.x={st1.x:.2f} != run2.x={st2.x:.2f}")
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# Station X stays within column tolerance
# ---------------------------------------------------------------------------


# All fixtures pass with the median-column-X tolerance once
# loop-side-branch stations (which the engine deliberately moves to the
# midpoint of their loop's diagonal corners) are excluded.  The
# placeholder dict locks in the invariant so a future bug-fix that
# accidentally drifts a station off-column lights up here.
_XFAIL_COL_DRIFT: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_COL_DRIFT),
)
def test_station_x_within_column_tolerance(fixture):
    """For each LR/RL section, every (non-loop-side-branch) station at
    layer L must sit within ``x_spacing`` of the median X of all
    (non-loop-side-branch) stations at the same layer in the same
    section.

    The median acts as the section's implicit "column X" for layer L,
    after fan-out spacing has been applied.  A station drifting more
    than one full x_spacing off its column indicates either an X-mutating
    phase regression or a new topology case that the engine handles
    incorrectly.

    Loop-side-branch stations (matched by
    ``_recenter_loop_side_stations``'s precondition) are exempted: the
    engine deliberately moves them to the midpoint of their loop's
    diagonal corners.  See ``is_loop_side_branch_station``.
    """
    import statistics

    x_spacing = X_SPACING
    graph = _layout(fixture, x_spacing=x_spacing)

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0 or sec.direction not in ("LR", "RL"):
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        layer_xs: dict[int, list[tuple[str, float]]] = defaultdict(list)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            if st.off_track:
                continue
            if is_loop_side_branch_station(graph, sid):
                continue
            layer_xs[st.layer].append((sid, st.x))
        for layer, members in layer_xs.items():
            if len(members) < 2:
                continue
            xs = [x for _, x in members]
            median_x = statistics.median(xs)
            for sid, x in members:
                if abs(x - median_x) > x_spacing:
                    offenders.append(
                        f"section={sec.id!r} layer={layer} {sid!r} "
                        f"x={x:.1f} median={median_x:.1f} "
                        f"drift={abs(x - median_x):.1f} > x_spacing={x_spacing:.1f}"
                    )
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# Label X anchored to station marker on horizontal runs (issue #348)
# ---------------------------------------------------------------------------

_SV_STATS_NUDGE_REASON = (
    "issue #348: sv_stats label nudged 14.1px to clear bcftools_stats "
    "label collision; revisit when the engine collision-clearance is "
    "tuned or the section is restructured"
)
_XFAIL_LABEL_AT_STATION_X: dict[str, str] = {
    "variantbenchmarking.mmd": _SV_STATS_NUDGE_REASON,
    "variantbenchmarking_auto.mmd": _SV_STATS_NUDGE_REASON,
}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_LABEL_AT_STATION_X),
)
def test_label_x_anchored_to_station_marker_on_horizontal_runs(fixture):
    """For non-port, non-junction, non-off-track LR/RL stations whose
    immediate inbound and outbound route segments are horizontal at the
    station's Y, the label X must equal the station's marker X within
    ``LABEL_DRIFT_TOL`` (10 px).

    ``place_labels`` defaults ``label.x = station.x`` for these stations;
    the only documented exception is the collision-avoidance nudge in
    ``_nudge_to_clear``, capped at ``LABEL_NUDGE_MAX`` (20 px).  10 px is
    half that cap and the empirical visual perception threshold: nudges
    smaller than this look centred to a human reader, larger ones look
    visibly off-centre.

    Replaces the removed ``test_label_x_matches_segment_midpoint_on_horizontal_runs``
    predicate (issue #348), which anchored against bracketing neighbour
    station Xs and fired on 25 fixtures where the visual was actually
    centred.  The new predicate fires only on labels that have drifted
    visibly from their station marker.
    """
    from nf_metro.layout.labels import place_labels

    Y_TOL = 1.0
    LABEL_DRIFT_TOL = 10.0

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    labels = place_labels(
        graph, station_offsets=offsets, label_angle=graph.label_angle or 0.0
    )
    label_by_sid = {lp.station_id: lp for lp in labels}

    in_routes: dict[str, list] = defaultdict(list)
    out_routes: dict[str, list] = defaultdict(list)
    for r in routes:
        in_routes[r.edge.target].append(r)
        out_routes[r.edge.source].append(r)

    offenders: list[str] = []
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or sid in graph.junctions:
            continue
        if st.off_track:
            continue
        sec = graph.sections.get(st.section_id) if st.section_id else None
        if sec is None or sec.direction not in ("LR", "RL"):
            continue
        lp = label_by_sid.get(sid)
        if lp is None or lp.text_anchor != "middle":
            # TB-style side anchors place labels at pill_left/pill_right
            # by construction; only middle-anchored labels are expected
            # to sit at the marker X.
            continue
        ins = in_routes.get(sid, [])
        outs = out_routes.get(sid, [])
        if not ins or not outs:
            continue
        in_horizontal = all(
            len(r.points) >= 2
            and abs(r.points[-2][1] - r.points[-1][1]) <= Y_TOL
            and abs(r.points[-1][1] - st.y) <= Y_TOL
            for r in ins
        )
        if not in_horizontal:
            continue
        out_horizontal = all(
            len(r.points) >= 2
            and abs(r.points[0][1] - r.points[1][1]) <= Y_TOL
            and abs(r.points[0][1] - st.y) <= Y_TOL
            for r in outs
        )
        if not out_horizontal:
            continue
        drift = abs(lp.x - st.x)
        if drift > LABEL_DRIFT_TOL:
            offenders.append(
                f"{sid!r} label.x={lp.x:.1f} station.x={st.x:.1f} drift={drift:.1f}"
            )
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# Visual stack stations share their column X (issue #348)
# ---------------------------------------------------------------------------

_XFAIL_VISUAL_STACK: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_VISUAL_STACK),
)
def test_visual_stack_station_xs_share_column(fixture):
    """Stations forming a visual stack must agree in X within 1 px.

    A visual stack is a group of stations in the same section sharing
    predecessor set and layer, where at least one pair sits
    0 < ΔY <= ``STACK_Y_WINDOW`` (2 * Y_SPACING = 80 px) apart.

    The grouping key deliberately omits the successor set: stations in a
    fan-out column share their feed and layer but routinely differ in
    where they go next (one rejoins the trunk, another also exits the
    section).  Keying on successors splits such a column into singletons
    and lets a mis-placed member slip past (issue #514: ``propd`` shares
    the ``differential`` column with ``dream``/``limma``/``deseq2`` but
    its extra exit-port edge gave it a distinct successor set).

    The Y-window distinguishes visually-stacked stations (close enough
    in Y that a viewer reads them as a column) from:

    - Side-by-side layouts (ΔY = 0): topological siblings in TB/BT
      sections are spread along X within their layer; X disagreement
      is intentional, not a stack regression.

    - Far-spread groups (ΔY > 80): topologically-similar stations the
      engine deliberately placed in different visual "bands" of the
      section.  Their X disagreement reads as independent placement,
      not as a misaligned stack.

    Replaces the removed ``test_stack_station_xs_share_column`` predicate
    (issue #348), which used the bare (preds, succs, layer) signature
    and fired on 4 fixtures where the topological-stack-mate framing
    didn't match the visual outcome.
    """
    from nf_metro.layout.constants import Y_SPACING

    STACK_Y_WINDOW = 2.0 * Y_SPACING
    X_TOL = 1.0

    graph = _layout(fixture)
    preds: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        preds[e.target].add(e.source)

    offenders: list[str] = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        groups: dict[tuple[frozenset[str], int], list[str]] = defaultdict(list)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            key = (frozenset(preds[sid]), st.layer)
            groups[key].append(sid)
        for members in groups.values():
            if len(members) < 2:
                continue
            xs = [graph.stations[s].x for s in members]
            ys = [graph.stations[s].y for s in members]
            x_drift = max(xs) - min(xs)
            if x_drift <= X_TOL:
                continue
            visual_stack = any(
                0 < abs(ys[i] - ys[j]) <= STACK_Y_WINDOW
                for i in range(len(members))
                for j in range(i + 1, len(members))
            )
            if not visual_stack:
                continue
            rounded_xs = [round(x, 1) for x in xs]
            rounded_ys = [round(y, 1) for y in ys]
            offenders.append(
                f"section={sec.id!r} stack {members} xs={rounded_xs} "
                f"ys={rounded_ys} dx={x_drift:.1f}"
            )
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# LR routes do not go backwards (#250)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_routes_dont_loop_backwards(fixture):
    """Cross-column routes must not contain a segment that reverses
    the route's overall X direction.  Catches issue #250's "pinwheel"
    artefact: a junction at the boundary of one column exits going +x
    then immediately turns -x to curve down into the next column's
    section.

    Same-column near-vertical routes are exempt: when source and
    target share a grid column with tiny dx, the routing engine
    legitimately wraps the channel beyond the column to enter from the
    appropriate side.  Routes touching TB/BT sections are also exempt
    (those route in either X direction internally).
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    offenders: list[str] = []
    for r in routes:
        pts = r.points
        if len(pts) < 2:
            continue
        src_st = graph.stations.get(r.edge.source)
        tgt_st = graph.stations.get(r.edge.target)
        src_sec = (
            graph.sections.get(src_st.section_id)
            if src_st and src_st.section_id
            else None
        )
        tgt_sec = (
            graph.sections.get(tgt_st.section_id)
            if tgt_st and tgt_st.section_id
            else None
        )
        if (src_sec and src_sec.direction in ("TB", "BT")) or (
            tgt_sec and tgt_sec.direction in ("TB", "BT")
        ):
            continue
        src_col = _resolve_section_col_for_station(graph, src_st)
        tgt_col = _resolve_section_col_for_station(graph, tgt_st)
        if src_col is not None and tgt_col is not None and src_col == tgt_col:
            continue
        overall_dx = pts[-1][0] - pts[0][0]
        if abs(overall_dx) < 1.0:
            continue
        # Both endpoint-segment reversals are legitimate.  Final segment:
        # a route arriving at an entry port on the opposite side from its
        # overall direction must curve back to enter (RL section's
        # right-entry, LR section's right-entry from below, etc.).  First
        # segment: a U-turn route (around-section-below,
        # left-entry-wrap with dx<0) leads OUT of the source's right edge
        # before turning to head left toward the target.  Only check
        # interior segments where a reversal would indicate the pinwheel
        # anti-pattern from #250.
        first_check = 2 if len(pts) >= 4 else 1
        check_until = len(pts) - 1 if len(pts) >= 3 else len(pts)
        for j in range(first_check, check_until):
            seg_dx = pts[j][0] - pts[j - 1][0]
            if overall_dx > 0 and seg_dx < -0.5:
                offenders.append(
                    f"{r.edge.source} -> {r.edge.target} (line={r.line_id}, "
                    f"overall +x): "
                    f"{tuple(round(c, 1) for c in pts[j - 1])} -> "
                    f"{tuple(round(c, 1) for c in pts[j])}"
                )
                break
            if overall_dx < 0 and seg_dx > 0.5:
                offenders.append(
                    f"{r.edge.source} -> {r.edge.target} (line={r.line_id}, "
                    f"overall -x): "
                    f"{tuple(round(c, 1) for c in pts[j - 1])} -> "
                    f"{tuple(round(c, 1) for c in pts[j])}"
                )
                break
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


# ---------------------------------------------------------------------------
# Ports must sit on a section bbox edge, and Port/Station registries must
# agree on the port's coordinates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_ports_on_section_boundary(fixture):
    """Every port station must sit on its section's bbox edge.

    Mirrors the runtime ``_guard_ports_on_boundaries`` check but exercises
    the full fixture corpus so a regression on any single pipeline trips
    the test rather than only ``validate=True`` runs.
    """
    from nf_metro.layout.constants import GUARD_TOLERANCE

    graph = _layout(fixture)
    tol = GUARD_TOLERANCE

    offenders: list[str] = []
    for pid, port in graph.ports.items():
        st = graph.stations.get(pid)
        if st is None:
            continue
        sec = graph.sections.get(st.section_id or "")
        if sec is None or sec.bbox_w == 0 or sec.bbox_h == 0:
            continue
        on_left = abs(st.x - sec.bbox_x) <= tol
        on_right = abs(st.x - (sec.bbox_x + sec.bbox_w)) <= tol
        on_top = abs(st.y - sec.bbox_y) <= tol
        on_bottom = abs(st.y - (sec.bbox_y + sec.bbox_h)) <= tol
        if not (on_left or on_right or on_top or on_bottom):
            offenders.append(
                f"port {pid!r} (side={port.side.name}) at "
                f"({st.x:.1f}, {st.y:.1f}) not on any edge of section "
                f"{st.section_id!r} bbox "
                f"({sec.bbox_x:.1f}, {sec.bbox_y:.1f}, "
                f"w={sec.bbox_w:.1f}, h={sec.bbox_h:.1f})"
            )

    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_inter_section_routes_dont_reenter_source_section(fixture):
    """An inter-section route, after exiting through a port on one side
    of its source section's bbox, must not have any segment that crosses
    BACK INTO the source section's bbox.

    Catches the "left-and-down at section right edge" pattern: route
    exits at the right (x = section.right) then a subsequent segment
    goes leftward at the same Y, re-entering the source's column at the
    source's y, before bending down.  See
    docs/dev/authoring_misfires.md #11.6 and #12.5.

    Same-section (intra-section) routes are exempt - they stay inside.
    Routes touching TB/BT sections are exempt (those route vertically
    inside their column).
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    offenders: list[str] = []
    for r in routes:
        pts = r.points
        if len(pts) < 2:
            continue
        if not r.is_inter_section:
            continue
        src_st = graph.stations.get(r.edge.source)
        tgt_st = graph.stations.get(r.edge.target)
        if src_st is None or tgt_st is None:
            continue
        # Resolve the SOURCE section (tracing through junctions).
        src_sec = _resolve_section_for_station(graph, src_st)
        if src_sec is None:
            continue
        if src_sec.direction in ("TB", "BT"):
            continue
        # Skip routes that stay in the same section (intra-section).
        tgt_sec = _resolve_section_for_station(graph, tgt_st)
        if tgt_sec is not None and tgt_sec.id == src_sec.id:
            continue
        sec_l = src_sec.bbox_x
        sec_r = src_sec.bbox_x + src_sec.bbox_w
        sec_t = src_sec.bbox_y
        sec_b = src_sec.bbox_y + src_sec.bbox_h
        # Two checks per route:
        # (a) For each interior corner point pts[1..-2] (which is the
        #     Q-curve's CONTROL point in the rendered SVG), the corner
        #     must not lie strictly inside the source section's bbox.
        #     This catches "channel x is inside the source section" bugs
        #     in L-shape routes.
        # (b) For each segment midpoint, the midpoint must not lie
        #     strictly inside the source section's bbox.  Catches
        #     segments that cross the bbox interior.
        # The source station itself (pts[0]) and the target (pts[-1]) are
        # allowed to coincide with the section boundary.
        EDGE_TOL = 0.5

        def _strictly_inside(px: float, py: float) -> bool:
            return (
                sec_l + EDGE_TOL < px < sec_r - EDGE_TOL
                and sec_t + EDGE_TOL < py < sec_b - EDGE_TOL
            )

        # A junction-originated route can START strictly inside its source
        # section (pts[0] is a junction placed inside the bbox).  The outward
        # run from that interior start to the exit boundary is legitimate;
        # the invariant only forbids geometry that crosses BACK IN after the
        # route has left.  ``exit_idx`` is the first point that is not
        # strictly inside - anything before it is the outward run and exempt.
        # Routes that start on the boundary (the usual exit-port case) have
        # exit_idx == 0 and get the full strict check.
        exit_idx = 0
        for k, (px, py) in enumerate(pts):
            if not _strictly_inside(px, py):
                exit_idx = k
                break
        found = False
        for j in range(1, len(pts) - 1):
            if j < exit_idx:
                continue
            cx, cy = pts[j]
            if _strictly_inside(cx, cy):
                offenders.append(
                    f"{r.edge.source} -> {r.edge.target} "
                    f"(line={r.line_id}) corner "
                    f"{tuple(round(c, 1) for c in pts[j])} inside "
                    f"source section {src_sec.id} bbox "
                    f"[{sec_l},{sec_t}]-[{sec_r},{sec_b}]"
                )
                found = True
                break
        if found:
            continue
        for j in range(1, len(pts)):
            if j <= exit_idx:
                continue
            x0, y0 = pts[j - 1]
            x1, y1 = pts[j]
            mx = (x0 + x1) / 2.0
            my = (y0 + y1) / 2.0
            if _strictly_inside(mx, my):
                offenders.append(
                    f"{r.edge.source} -> {r.edge.target} "
                    f"(line={r.line_id}) seg "
                    f"{tuple(round(c, 1) for c in pts[j - 1])} -> "
                    f"{tuple(round(c, 1) for c in pts[j])} "
                    f"midpoint ({round(mx, 1)}, {round(my, 1)}) inside "
                    f"source section {src_sec.id} bbox "
                    f"[{sec_l},{sec_t}]-[{sec_r},{sec_b}]"
                )
                break
    assert not offenders, f"{fixture}: " + "; ".join(offenders[:3])


def _resolve_section_for_station(graph, station):
    """Resolve a station's section, tracing back through junctions.

    For regular stations, returns the section they belong to.
    For junction stations (section_id=None), follows an incoming edge to
    a real station and returns that station's section.
    """
    if station is None:
        return None
    if station.section_id:
        return graph.sections.get(station.section_id)
    if station.id in graph.junctions:
        for e in graph.edges:
            if e.target == station.id:
                pred = graph.stations.get(e.source)
                if pred and pred.section_id:
                    return graph.sections.get(pred.section_id)
    return None


def _resolve_section_col_for_station(graph, station):
    """Resolve a station's grid column.  For ports, use the section.
    For junctions, follow an incoming edge back to a real station.
    """
    sec = _resolve_section_for_station(graph, station)
    if sec and sec.grid_col >= 0:
        return sec.grid_col
    return None


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_debug_grid_overlay_boundaries_outside_section_bboxes(fixture):
    """Debug-overlay row/column separator segments must not cut through
    any section bbox.

    The overlay draws per-column horizontal segments between consecutive
    grid rows (and per-row vertical segments between consecutive columns).
    Each segment sits at the local midpoint between two adjacent
    sections' bboxes; when a fold extends a section past its row's
    natural extent the local midpoint would land inside a bbox, so the
    segment for that column is dropped.  This test asserts no emitted
    segment cuts any section bbox in the rows/columns it joins.

    Bug: https://github.com/pinin4fjords/nf-metro/issues/316
    """
    from nf_metro.render.svg import (
        _compute_col_boundary_xs,
        _compute_row_boundary_segments,
        _grid_bbox_bounds,
    )

    graph = _layout(fixture)
    sections = list(graph.sections.values())
    if not sections:
        pytest.skip("no sections")
    col_bounds, row_bounds = _grid_bbox_bounds(sections)

    offenders: list[str] = []
    for ra, rb, x_start, x_end, y in _compute_row_boundary_segments(
        sections, col_bounds
    ):
        for sec in sections:
            if sec.grid_row_span != 1 or sec.grid_row not in (ra, rb):
                continue
            y0, y1 = sec.bbox_y, sec.bbox_y + sec.bbox_h
            x0, x1 = sec.bbox_x, sec.bbox_x + sec.bbox_w
            x_overlaps = max(x_start, x0) < min(x_end, x1)
            if x_overlaps and y0 < y < y1:
                offenders.append(
                    f"row {ra}|{rb} segment y={y:.1f} x={x_start:.0f}..{x_end:.0f} "
                    f"cuts {sec.id!r} (row={sec.grid_row}, y={y0:.1f}..{y1:.1f})"
                )
    for ca, cb, mid_x in _compute_col_boundary_xs(col_bounds, sections):
        for sec in sections:
            if sec.grid_col_span != 1:
                continue
            x0, x1 = sec.bbox_x, sec.bbox_x + sec.bbox_w
            if x0 < mid_x < x1:
                offenders.append(
                    f"col {ca}|{cb} mid_x={mid_x:.1f} cuts {sec.id!r} "
                    f"(col={sec.grid_col}, x={x0:.1f}..{x1:.1f})"
                )

    assert not offenders, f"{fixture}: " + "; ".join(offenders[:5])


# ---------------------------------------------------------------------------
# Trunk-Y / fan-symmetry invariants.
#
# Three properties that an entry/exit-port placement must preserve:
# equal-rank fans stay symmetric about their port, fan-and-reconverge exit
# ports stay on their merge row, and thick multi-line bundles keep vertical
# clearance.  Each is violated by anchoring a fan on its topmost target.
# ---------------------------------------------------------------------------


def _lr_port(graph: MetroGraph, port_ids) -> str | None:
    """Return the first LEFT/RIGHT port id in ``port_ids`` (or None)."""
    for pid in port_ids:
        port = graph.ports.get(pid)
        if port is not None and port.side in (PortSide.LEFT, PortSide.RIGHT):
            return pid
    return None


# Fixtures whose terminal LR section has an entry port that fans directly
# into >= 2 equal-rank targets straddling the port.  The fan must stay
# symmetric about the port; the pre-fix engine top-anchored the whole fan
# (shifting every target below the port), so its mean drifts off the port.
#
# Curated rather than corpus-wide: under default station pitch some real
# pipelines (differentialabundance_default, hlatyping reporting,
# variantbenchmarking stats) legitimately stack their two sinks on one
# side of the port even on the fixed engine, so a corpus-wide assertion
# would false-positive there.  These three fixtures have the room to fan
# symmetrically and do so on the fixed engine.
_SYMFAN_ABOUT_PORT_FIXTURES = [
    "differentialabundance.mmd",
    "da_pipeline.mmd",
    "topologies/terminal_symmetric_fan.mmd",
]


@pytest.mark.parametrize("fixture", _SYMFAN_ABOUT_PORT_FIXTURES)
def test_terminal_fan_symmetric_about_entry_port(fixture):
    """A terminal LR/RL section whose entry port fans directly into a
    set of equal-rank targets must keep that fan symmetric about the
    port: the mean of the target Ys equals the entry port Y.

    Regression lock for the top-anchor bug where the fan was pinned to
    its topmost target (the entry port row), so one branch collapsed
    onto the trunk and the fan's centre of mass dropped below the port.
    Evidence (differentialabundance.mmd ``reporting``): fixed engine
    places shinyngs at 175.2 and quarto at 292.0 (mean 233.6 == port
    Y 233.6); the pre-fix engine pinned shinyngs to 233.6, dropping the
    mean to 262.8.
    """
    graph = _layout(fixture)
    tested = 0
    for sec in graph.sections.values():
        if sec.direction not in ("LR", "RL"):
            continue
        # Terminal: no LR/RL exit port (the bundle ends in this section).
        if _lr_port(graph, sec.exit_ports) is not None:
            continue
        ep = _lr_port(graph, sec.entry_ports)
        if ep is None:
            continue
        port_y = graph.stations[ep].y
        # Direct fan targets: visible internal stations the entry port
        # feeds, de-duplicated across the per-line edge fan.
        targets: list[str] = []
        for e in graph.edges_from(ep):
            t = graph.stations.get(e.target)
            if t is None or t.is_port or t.off_track or t.is_hidden:
                continue
            if e.target not in targets:
                targets.append(e.target)
        if len(targets) < 2:
            continue
        # Equal-rank: every target carries the same line set.
        line_sets = {frozenset(graph.station_lines(t)) for t in targets}
        if len(line_sets) != 1:
            continue
        ys = [graph.stations[t].y for t in targets]
        # Straddle precondition: a genuine fan has targets both above and
        # below the port.  A same-side stack (both below) is a different
        # layout and not the symmetric-fan pattern this locks.
        if not (min(ys) < port_y - _Y_TOL and max(ys) > port_y + _Y_TOL):
            continue
        tested += 1
        mean = sum(ys) / len(ys)
        assert abs(mean - port_y) <= 2.0, (
            f"{fixture} section {sec.id}: terminal fan not symmetric about "
            f"entry port (port_y={port_y:.1f}, target mean={mean:.1f}, "
            f"targets={sorted((t, round(graph.stations[t].y, 1)) for t in targets)})"
        )
    assert tested >= 1, (
        f"{fixture}: no terminal entry-port fan matched the precondition"
    )


# Fixtures with a pass-through LR section (entry + exit both carry the
# full bundle) whose exit port is fed by an in-section reconvergence
# (a merge station with in-degree >= 2).  The exit must sit on the merge
# row; the pre-fix engine top-anchored the exit to the entry trunk row,
# detaching it from the merge and kinking the inter-section trunk.
_TRUNK_RECONVERGE_FIXTURES = [
    "hlatyping.mmd",
    "topologies/trunk_through_fan.mmd",
]


@pytest.mark.parametrize("fixture", _TRUNK_RECONVERGE_FIXTURES)
def test_trunk_exit_follows_reconvergence(fixture):
    """For a pass-through LR/RL section (LR entry and exit ports both
    carrying the section's full bundle) whose exit port is fed by a
    single in-section reconvergence merge station, the exit port Y must
    equal the merge station's Y.

    Regression lock for the fan-and-reconverge trunk-Y kink: the merge
    of a side branch back onto the trunk sits below the entry trunk row,
    and the exit port must follow it down.  Evidence (hlatyping.mmd
    ``hla_typing``): the fixed engine places the exit port at 160.0 to
    match the merge ``_merge1`` at 160.0; the pre-fix engine pinned the
    exit to the entry trunk Y 120.0, a 40px detachment from its feeder.
    """
    graph = _layout(fixture)
    tested = 0
    for sec_id, sec in graph.sections.items():
        if sec.direction not in ("LR", "RL"):
            continue
        ep = _lr_port(graph, sec.entry_ports)
        xp = _lr_port(graph, sec.exit_ports)
        if ep is None or xp is None:
            continue
        bundle = _section_full_bundle(graph, sec)
        if not bundle:
            continue
        if set(graph.station_lines(ep)) != bundle:
            continue
        if set(graph.station_lines(xp)) != bundle:
            continue
        # Sole in-section, non-port feeder of the exit port.
        feeders: list[str] = []
        for e in graph.edges_to(xp):
            s = graph.stations.get(e.source)
            if s is None or s.is_port or s.section_id != sec_id:
                continue
            if e.source not in feeders:
                feeders.append(e.source)
        if len(feeders) != 1:
            continue
        merge = feeders[0]
        # Reconvergence: the merge joins >= 2 distinct upstream sources.
        in_sources = {e.source for e in graph.edges_to(merge)}
        if len(in_sources) < 2:
            continue
        tested += 1
        exit_y = graph.stations[xp].y
        merge_y = graph.stations[merge].y
        assert abs(exit_y - merge_y) <= 2.0, (
            f"{fixture} section {sec_id}: exit port detached from its "
            f"reconvergence merge (exit_y={exit_y:.1f}, "
            f"merge {merge!r} y={merge_y:.1f})"
        )
    assert tested >= 1, (
        f"{fixture}: no pass-through section with a reconvergence-fed "
        f"exit port matched the precondition"
    )


# Thick-bundle fixtures whose fan columns stack on-track stations that
# all carry a >= 4-line bundle.  Consecutive station rows in such a
# column must clear ``min_track_gap`` so the bundle's parallel lines and
# their labels don't crowd.  Curated to rnaseq_sections_manual.mmd: it
# stacks a 6-line bundle (BBSplit/SortMeRNA/RiboDetector) at a clean grid
# pitch on the fixed engine.  rnaseq_sections.mmd and variantbenchmarking
# stack the same kind of bundle at a sub-min_track_gap pitch even on the
# fixed engine (a separate, pre-existing tightness), so a corpus-wide
# assertion would false-positive there.
_THICK_BUNDLE_FIXTURES = ["rnaseq_sections_manual.mmd"]


@pytest.mark.parametrize("fixture", _THICK_BUNDLE_FIXTURES)
def test_thick_bundle_row_pitch(fixture):
    """A column that stacks >= 2 on-track stations each carrying a
    bundle of N >= 4 lines must keep consecutive station Ys at least
    ``min_track_gap`` apart, so the parallel lines plus the under-marker
    label have vertical breathing room.

    Regression lock for the flat-``y_spacing`` crowding bug.  The fixed
    engine widens the row pitch to ``max(y_spacing, min_track_gap)``,
    where ``min_track_gap = (max_lines-1)*OFFSET_STEP + 2*STATION_RADIUS
    _APPROX + LABEL_OFFSET + FONT_HEIGHT``.  Evidence
    (rnaseq_sections_manual.mmd ``preprocessing``): the fixed engine
    stacks the 6-line bundle at a 50px pitch (== min_track_gap); the
    pre-fix engine crowded it to a flat 40px ``y_spacing``.
    """
    from nf_metro.layout.constants import (
        FONT_HEIGHT,
        LABEL_OFFSET,
        OFFSET_STEP,
        STATION_RADIUS_APPROX,
    )

    graph = _layout(fixture)
    tested = 0
    for sec in graph.sections.values():
        if sec.direction not in ("LR", "RL"):
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        cols: dict[float, list[float]] = defaultdict(list)
        max_lines = 0
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            max_lines = max(max_lines, len(graph.station_lines(sid)))
            cols[round(st.x, 1)].append(st.y)
        if max_lines < 4:
            continue
        min_track_gap = (
            (max_lines - 1) * OFFSET_STEP
            + 2 * STATION_RADIUS_APPROX
            + LABEL_OFFSET
            + FONT_HEIGHT
        )
        for x, raw_ys in cols.items():
            ys = sorted(set(round(y, 1) for y in raw_ys))
            if len(ys) < 2:
                continue
            min_gap = min(ys[i + 1] - ys[i] for i in range(len(ys) - 1))
            tested += 1
            assert min_gap >= min_track_gap - _Y_TOL, (
                f"{fixture} section {sec.id} column x={x}: thick "
                f"{max_lines}-line bundle crowded - consecutive station "
                f"Ys {ys} have min gap {min_gap:.1f}px < min_track_gap "
                f"{min_track_gap:.1f}px"
            )
    assert tested >= 1, (
        f"{fixture}: no thick (>= 4-line) bundle column with >= 2 stacked "
        f"stations matched the precondition"
    )


# ---------------------------------------------------------------------------
# Merge-port approach-side slot allocation
# ---------------------------------------------------------------------------


def _fixtures_with_merge_port() -> list[str]:
    """Fixtures with at least one reconvergence merge port.

    A merge port here is an LR/RL entry port fed by >= 2 exit ports with
    both a horizontal co-traveller and a perpendicular feeder - the case
    the approach-side allocation governs.  Computed once at import time.
    """
    from nf_metro.layout.routing.invariants import classify_merge_port_feeders

    out: list[str] = []
    for name in ALL_FIXTURES:
        try:
            g = _layout(name)
        except Exception:
            continue
        if any(classify_merge_port_feeders(g, pid) is not None for pid in g.ports):
            out.append(name)
    return out


_FIXTURES_WITH_MERGE_PORT = _fixtures_with_merge_port()


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_MERGE_PORT)
def test_merge_port_rejoining_line_takes_approach_slot(fixture):
    """A line that re-joins a bundle perpendicular at a multi-feeder
    entry port must take the bundle slot nearest its approach side.

    A line rising from a section below must sit at or below every
    horizontally-arriving co-traveller (bottom slot); a line descending
    from a section above must sit at or above them (top slot).  When it
    is forced into a priority slot on the far side instead, its
    perpendicular riser crosses over the horizontal lines - the visible
    crossover this invariant guards against (issue #415).
    """
    from nf_metro.layout.routing.invariants import check_merge_port_approach_side

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    violations = check_merge_port_approach_side(graph, offsets)
    assert violations == [], (
        f"{fixture}: {len(violations)} merge-port approach-side "
        f"violation(s); first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Bypass line at fan-in entry port must hold the outer slot
# ---------------------------------------------------------------------------


_FIXTURES_WITH_BYPASS_FAN_IN = [
    "topologies/bypass_fan_in_outer_slot.mmd",
]


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_BYPASS_FAN_IN)
def test_bypass_fan_in_outer_slot(fixture):
    """At a multi-feeder entry port containing a bypass horizontal line, all
    bundle slots must be consecutive with no empty interior gaps.

    A bypass line misclassified as a plain horizontal co-traveller inflates
    ``max_horiz``, pushing perpendicular feeders into outer slots and leaving
    empty interior slots.  After the fix, all N lines pack into slots 0..N-1.
    """
    from nf_metro.layout.constants import COORD_TOLERANCE_FINE, OFFSET_STEP
    from nf_metro.layout.routing.invariants import (
        bypass_horizontal_targets,
        classify_merge_port_feeders,
        distinct_offset_levels,
    )

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)

    for port_id in graph.ports:
        if classify_merge_port_feeders(graph, port_id) is None:
            continue
        bypass = bypass_horizontal_targets(graph, port_id)
        if not bypass:
            continue
        lines = list(graph.station_lines(port_id))
        port_offsets = sorted(offsets.get((port_id, lid), 0.0) for lid in lines)
        levels = distinct_offset_levels(port_offsets)
        gaps = [
            (levels[i], levels[i + 1])
            for i in range(len(levels) - 1)
            if levels[i + 1] - levels[i] > OFFSET_STEP + COORD_TOLERANCE_FINE
        ]
        assert not gaps, (
            f"{fixture}: merge port {port_id!r} has empty bundle slot gaps: "
            f"gaps at {gaps} "
            f"(offsets: {[f'{o:.0f}' for o in port_offsets]})"
        )


# ---------------------------------------------------------------------------
# Partial-line fan branches must not reserve absent-line offset slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_COMPACT)
def test_partial_fan_branch_has_no_offset_gap(fixture):
    """Under ``compact_offsets``, an independent fan branch that carries
    only a subset of a bundle's lines must place those lines on
    consecutive offset slots, not reserve an empty slot for the lines it
    omits.

    Catches the genomic_pipeline Variant-calling regression where
    ``strelka``/``indexcov`` (germline + somatic, no tumor_only) parked
    their two lines in the top and bottom of three reserved slots,
    leaving a visible gap where the absent ``tumor_only`` track would be
    (issue #443).
    """
    from nf_metro.layout.routing.invariants import check_partial_branch_offset_gaps

    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    violations = check_partial_branch_offset_gaps(graph, offsets)
    assert violations == [], (
        f"{fixture}: {len(violations)} partial-branch offset-gap "
        f"violation(s); first: {violations[0].message() if violations else ''}"
    )


# ---------------------------------------------------------------------------
# Anchor invariant: content placement never moves a port anchor (any side,
# either axis)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_MULTI_SECTION)
def test_content_placement_leaves_port_anchors_frozen(fixture, monkeypatch):
    """Anchors-first invariant: every content-placement phase (fans,
    off-track lift, band-fill, balance, recenter) positions content around
    the resolved anchors and must not move one.  Wrap each phase to snapshot
    every port's ``(x, y)`` before/after and assert none move - covering all
    port sides and both axes, not just the LR/RL-Y subset, so a phase that
    nudged a TOP/BOTTOM port or a port's cross-axis would be caught too.
    Checked in isolation (no full ``validate`` suite) so an unrelated guard
    cannot pre-empt this assertion."""
    from nf_metro.layout import engine
    from nf_metro.layout.phases.guards import _port_anchor_snapshot

    moved: list[tuple[str, str, float, float]] = []

    def _make_probe(name: str, orig):
        def probe(graph, *args, **kwargs):
            before = _port_anchor_snapshot(graph)
            result = orig(graph, *args, **kwargs)
            after = _port_anchor_snapshot(graph)
            for pid, (x0, y0) in before.items():
                coords = after.get(pid)
                if coords is None:
                    continue
                x1, y1 = coords
                if abs(x1 - x0) > 1.0 or abs(y1 - y0) > 1.0:
                    moved.append((name, pid, round(x1 - x0, 2), round(y1 - y0, 2)))
            return result

        return probe

    for name in CONTENT_PLACEMENT_PHASES:
        monkeypatch.setattr(engine, name, _make_probe(name, getattr(engine, name)))

    _layout(fixture, _cache=False)
    assert not moved, f"{fixture}: content placement moved port anchor(s): {moved[:3]}"


@pytest.mark.parametrize(
    "example, sides, axis",
    [
        # LR/RL port Y -- the original (subset) coverage.
        ("differentialabundance.mmd", (PortSide.LEFT, PortSide.RIGHT), "y"),
        # TOP/BOTTOM port X -- newly covered by the widened snapshot.
        ("rnaseq_sections.mmd", (PortSide.TOP, PortSide.BOTTOM), "x"),
        # LR/RL port X (cross-axis) -- also newly covered.
        ("differentialabundance.mmd", (PortSide.LEFT, PortSide.RIGHT), "x"),
    ],
)
def test_anchor_guard_catches_a_displaced_port(monkeypatch, example, sides, axis):
    """The guard is meaningful, not vacuous: if a content-placement phase
    moves any port anchor, ``compute_layout(validate=True)`` raises.
    Monkeypatch the balance phase to shove the first matching port (by side
    and axis) after it runs and assert the guard catches it.  Parametrised
    across port sides and both axes to exercise the widened snapshot."""
    from nf_metro.layout import engine
    from nf_metro.layout.phases.balancing import (
        _balance_section_content_around_trunk as _orig_balance,
    )

    def _evil(graph, *args):
        _orig_balance(graph, *args)
        for pid, st in graph.stations.items():
            port = graph.ports.get(pid)
            if st.is_port and port is not None and port.side in sides:
                if axis == "y":
                    st.y += 50.0
                else:
                    st.x += 50.0
                break

    monkeypatch.setattr(engine, "_balance_section_content_around_trunk", _evil)
    with pytest.raises(PhaseInvariantError, match="port anchor"):
        _layout_example(example, validate=True, _cache=False)


# ---------------------------------------------------------------------------
# TOP-entry cross-column bundle: concentric corners
# ---------------------------------------------------------------------------


def _corner_arc_center(p_prev, p_corner, p_next, radius):
    """Arc centre that fits ``radius`` into the bend at ``p_corner``."""
    import math

    v1 = (p_prev[0] - p_corner[0], p_prev[1] - p_corner[1])
    v2 = (p_next[0] - p_corner[0], p_next[1] - p_corner[1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return None
    u1 = (v1[0] / n1, v1[1] / n1)
    u2 = (v2[0] / n2, v2[1] / n2)
    bis = (u1[0] + u2[0], u1[1] + u2[1])
    nb = math.hypot(*bis)
    if nb == 0:
        return None
    ub = (bis[0] / nb, bis[1] / nb)
    dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    half = math.acos(dot) / 2.0
    if math.sin(half) == 0:
        return None
    dist = radius / math.sin(half)
    return (p_corner[0] + ub[0] * dist, p_corner[1] + ub[1] * dist)


def test_cross_row_top_entry_bundle_corners_are_concentric():
    """The multi-line U-route into a TOP entry port must bend concentrically.

    ``cross_row_gap_wrap`` carries ``main`` and ``feed`` from a junction in
    row 0 across and down into the ``merge_pt`` TOP entry port in row 1.  The
    two lines must keep a constant perpendicular gap through the lead-in
    corner and the two trunk corners: their arc centres must coincide there
    (the final jog onto the shared port point is exempt, as both lines must
    converge on one marker).
    """
    import math

    graph = _layout("topologies/cross_row_gap_wrap.mmd")
    routes = route_edges(graph)
    offsets = compute_station_offsets(graph)

    from nf_metro.render.svg import apply_route_offsets

    bundle = [
        r
        for r in routes
        if r.edge.source == "__junction_8" and r.edge.target == "merge_pt__entry_top_6"
    ]
    assert len(bundle) == 2, f"expected 2 lines, got {len(bundle)}"
    bundle.sort(key=lambda r: r.line_id)

    rendered = [(r, apply_route_offsets(r, offsets)) for r in bundle]
    # The reference line drops straight into the port (no jog, 5 points); the
    # offset line steps across with a converging jog (6 points).  Compare the
    # three shared bends C1 (lead-in), C2 and C3 (trunk).
    centers = []
    for r, pts in rendered:
        cs = [
            _corner_arc_center(pts[k - 1], pts[k], pts[k + 1], r.curve_radii[k - 1])
            for k in range(1, 4)
        ]
        centers.append(cs)

    for k in range(3):
        a = centers[0][k]
        b = centers[1][k]
        assert a is not None and b is not None
        gap = math.hypot(a[0] - b[0], a[1] - b[1])
        assert gap <= 1.0, (
            f"corner C{k + 1} arc centres are {gap:.2f}px apart "
            f"(non-concentric): {a} vs {b}"
        )


# ---------------------------------------------------------------------------
# Linear section spine should not fold when it fits one row
# ---------------------------------------------------------------------------

# A linear section spine threads one section per topo column; forward "skip"
# edges over that spine create a convergence point. When the whole flow fits
# comfortably in a single row, splitting it onto a return row only bends the
# flow backward, so every section must land in grid row 0. These fixtures are
# such chains.
_LINEAR_SPINE_FITS_ONE_ROW = {
    "single_skip": """%%metro title: Single Skip
%%metro line: m | M | #0570b0
graph LR
    subgraph a [A]
        a1[A1]
    end
    subgraph b [B]
        b1[B1]
    end
    subgraph c [C]
        c1[C1]
    end
    subgraph d [D]
        d1[D1]
    end
    a1 -->|m| b1
    b1 -->|m| c1
    c1 -->|m| d1
    a1 -->|m| c1
    b1 -->|m| d1
""",
    "genomeassembly_shape": """%%metro title: Genomeassembly Shape
%%metro line: m | M | #0570b0
graph LR
    subgraph raw [Raw]
        r1[R1]
        r2[R2]
        r1 -->|m| r2
    end
    subgraph purge [Purge]
        p1[P1]
        p2[P2]
        p1 -->|m| p2
    end
    subgraph polish [Polish]
        o1[O1]
        o2[O2]
        o1 -->|m| o2
    end
    subgraph scaffold [Scaffold]
        s1[S1]
        s2[S2]
        s1 -->|m| s2
    end
    subgraph qc [QC]
        q1[Q1]
    end
    r2 -->|m| p1
    p2 -->|m| o1
    o2 -->|m| s1
    s2 -->|m| q1
    r2 -->|m| o1
    r2 -->|m| s1
    p2 -->|m| s1
    r2 -->|m| q1
    p2 -->|m| q1
    o2 -->|m| q1
""",
}


@pytest.mark.parametrize("name", sorted(_LINEAR_SPINE_FITS_ONE_ROW))
def test_linear_spine_fitting_one_row_is_single_row(name):
    """A linear section spine that fits the fold threshold stays in one row.

    Forward skip edges over an already-linear chain of sections form a
    convergence point, but splitting the chain onto a return row when it
    fits a single row only bends the flow backward.  Each fixture here is a
    one-section-per-column spine whose total width is well under the
    threshold; every section must sit in grid row 0.
    """
    graph = parse_metro_mermaid(_LINEAR_SPINE_FITS_ONE_ROW[name])
    rows = {sec.grid_row for sec in graph.sections.values()}
    assert rows == {0}, (
        f"{name}: linear spine fitting one row spread across rows "
        f"{sorted(rows)}; expected all sections in row 0"
    )


def test_genomeassembly_auto_layout_is_single_row():
    """The de-pinned genomeassembly example must auto-lay-out in one row.

    Its sections form a single chain (Raw assembly -> Purging -> Polishing
    -> Scaffolding -> Genome QC) that fits one row; auto-layout must
    reproduce the single-row flow rather than fold it.
    """
    graph = parse_metro_mermaid((EXAMPLES / "genomeassembly.mmd").read_text())
    rows = {sec.grid_row for sec in graph.sections.values()}
    assert rows == {0}, (
        f"genomeassembly sections spread across rows {sorted(rows)}; "
        f"expected a single row"
    )


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_wrapped_label_clears_foreign_trunk(fixture):
    """No wrapped (multi-line) label's ink overruns a foreign metro trunk.

    A label that wraps stacks its extra lines toward the row above (above
    placement) or below, so its block grows toward a neighbouring track.  When
    a collision push-out has already nudged the label toward that track, the
    grown block can land on a metro line the station does not serve, drawing
    the name straight through the line.  The renderer's pull-back keeps the
    label at its un-pushed anchor (closest to its own pill) so the block stays
    clear, accepting a graze with a neighbouring label rather than a line
    striking through the text.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    theme = THEMES["nfcore"]
    icon_obstacles = _compute_icon_obstacles(graph, theme, offsets)
    placements = place_labels(
        graph,
        station_offsets=offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
        label_angle=graph.label_angle or 0.0,
    )
    strikes = find_wrapped_label_trunk_strikes(graph, placements, routes, offsets)
    assert not strikes, (
        f"{fixture}: wrapped label(s) overrun a foreign trunk: "
        + ", ".join(f"{sid} crosses line {lid} at y={y:.1f}" for sid, y, lid in strikes)
    )


def test_wrapped_label_trunk_lift_has_teeth():
    """Without the lift, the wrapped-label fixture genuinely strikes a trunk.

    Locks the invariant's meaningfulness: ``Samtools sort`` wraps and, left at
    the collision push-out's anchor, its first line crosses the QC trunk one
    track above.  The lift clears it; disabling the lift reinstates the strike,
    so the passing case above is the lift working, not an empty topology.
    """
    fixture = "wrapped_label_trunk.mmd"
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    icon_obstacles = _compute_icon_obstacles(graph, THEMES["nfcore"], offsets)
    unlifted = place_labels(
        graph,
        station_offsets=offsets,
        icon_obstacles=icon_obstacles,
        routes=routes,
        lift_wrapped_off_trunks=False,
    )
    strikes = find_wrapped_label_trunk_strikes(graph, unlifted, routes, offsets)
    assert any(sid == "sort" for sid, _y, _lid in strikes), (
        f"expected an un-lifted strike on 'sort', got {strikes}"
    )

    # The lift runs in the render path place_labels uses; validate=True asserts
    # the settled render leaves no strike (the guard does not raise).
    _layout(fixture, validate=True)


# Bottom-drop: an LR/RL section feeding a TB section's perpendicular TOP entry
# (issue #720) leaves through a BOTTOM exit and drops straight onto the target
# trunk, which is aligned under the exit.
_BOTTOM_DROP_FIXTURES = [
    "lr_to_tb_top_drop.mmd",
    "lr_to_tb_top_drop_two_lines.mmd",
]


def _lr_bottom_drop_exits(graph: MetroGraph) -> set[str]:
    """Port IDs of BOTTOM exits on horizontal-flow sections."""
    return {
        pid
        for pid, port in graph.ports.items()
        if not port.is_entry
        and port.side == PortSide.BOTTOM
        and (sec := graph.sections.get(port.section_id)) is not None
        and sec.direction in ("LR", "RL")
    }


@pytest.mark.parametrize("fixture", _BOTTOM_DROP_FIXTURES)
def test_lr_to_tb_bottom_drop_routes_straight(fixture):
    """The inter-section leg from a BOTTOM exit into a TOP entry is vertical.

    A single curve lives in the internal trunk->exit segment; the leg between
    the two sections is then a straight drop (one X per line), and the target
    trunk sits directly under the exit so no jog is needed inside the target.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    drop_exits = _lr_bottom_drop_exits(graph)
    assert drop_exits, f"{fixture}: expected a BOTTOM drop exit on an LR section"

    drop_legs = [r for r in routes if r.edge.source in drop_exits]
    assert drop_legs, f"{fixture}: no route leaves the BOTTOM drop exit"
    for leg in drop_legs:
        xs = {round(x, 3) for x, _y in leg.points}
        assert len(xs) == 1, (
            f"{fixture}: bottom-drop leg {leg.edge.source}->{leg.edge.target} "
            f"is not a straight vertical drop: {leg.points}"
        )
        target_port = graph.ports.get(leg.edge.target)
        if target_port is not None and target_port.is_entry:
            exit_x = graph.stations[leg.edge.source].x
            entry_x = graph.stations[leg.edge.target].x
            assert abs(exit_x - entry_x) < 1.0, (
                f"{fixture}: target trunk not aligned under the exit "
                f"(exit x={exit_x}, entry x={entry_x})"
            )


@pytest.mark.parametrize("fixture", _BOTTOM_DROP_FIXTURES)
def test_lr_to_tb_bottom_drop_clears_last_station(fixture):
    """The BOTTOM exit sits clear of every internal station along the flow.

    The trunk curves out after the trailing station rather than turning the
    line through a marker (a station-as-elbow on the perpendicular port).
    """
    graph = _layout(fixture)
    for pid in _lr_bottom_drop_exits(graph):
        section = graph.sections[graph.ports[pid].section_id]
        exit_x = graph.stations[pid].x
        internal_xs = [
            graph.stations[sid].x
            for sid in section.station_ids
            if sid in graph.stations and not graph.stations[sid].is_port
        ]
        assert internal_xs, f"{fixture}: section {section.id} has no stations"
        assert all(abs(exit_x - sx) > 10.0 for sx in internal_xs), (
            f"{fixture}: BOTTOM exit x={exit_x} coincides with a station "
            f"(would route through the marker)"
        )


@pytest.mark.parametrize("fixture", _BOTTOM_DROP_FIXTURES)
def test_lr_to_tb_bottom_drop_no_boundary_crossover(fixture):
    """Co-travelling drop lines keep their X order across the section boundary.

    Each line's inter-section drop leg must land at the same X its in-section
    trunk segment runs on, so a multi-line bundle flows straight through the
    shared entry port instead of two lines swapping sides (a crossover) at the
    boundary.
    """
    graph = _layout(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    drop_exits = _lr_bottom_drop_exits(graph)
    drop_leg_x = {
        r.line_id: r.points[-1][0] for r in routes if r.edge.source in drop_exits
    }
    if len(drop_leg_x) < 2:
        pytest.skip(f"{fixture}: single-line drop has no bundle order to cross")

    # First in-section trunk segment each line runs after the entry port.
    entry_ports = {
        pid
        for pid, port in graph.ports.items()
        if port.is_entry and port.side in (PortSide.TOP, PortSide.BOTTOM)
    }
    trunk_x = {
        r.line_id: r.points[0][0]
        for r in routes
        if r.edge.source in entry_ports and r.line_id in drop_leg_x
    }
    for line_id, x in drop_leg_x.items():
        assert line_id in trunk_x, f"{fixture}: line {line_id} has no trunk segment"
        assert abs(x - trunk_x[line_id]) < 1.0, (
            f"{fixture}: line {line_id} drops at x={x} but its trunk runs at "
            f"x={trunk_x[line_id]} -- the bundle swaps sides at the boundary"
        )
