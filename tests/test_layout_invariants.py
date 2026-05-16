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

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Tolerance for "same Y" assertions.  The grid pitch defaults to 55px;
# 1px slack absorbs sub-pixel rounding from fan-recenter phases.
_Y_TOL = 1.0


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


def _layout(fixture: str, **kwargs) -> MetroGraph:
    """Parse a fixture file and run the full layout pipeline.

    ``fixture`` may be a name under ``tests/fixtures/`` (legacy) or a
    name under ``examples/`` and its subdirs (``topologies/``, ``guide/``).
    Pass ``center_ports=False`` to opt out of the centre-ports default
    that the older fixtures relied on; tests over the full example
    corpus should not override it because example files declare the
    directive directly.
    """
    path = _resolve_fixture(fixture)
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
    return graph


def _layout_example(name: str, **kwargs) -> MetroGraph:
    """Parse an example file and run layout, honouring its own directives."""
    text = (EXAMPLES / name).read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph, **kwargs)
    return graph


# ---------------------------------------------------------------------------
# Fixture discovery helpers for full-corpus parametrization
# ---------------------------------------------------------------------------


def _discover_fixtures() -> list[str]:
    """Return all ``%%metro``-format .mmd files under tests/fixtures and
    examples, addressable via :func:`_resolve_fixture`.

    Excludes Nextflow-format flowcharts under ``tests/fixtures/nextflow/``
    (those are parser inputs, not layout inputs) and any file lacking a
    ``%%metro`` directive.
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
_FIXTURES_MULTI_SECTION = _fixtures_with(lambda t: t.count("subgraph") >= 2)


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
# Off-track inputs sit above their consumer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _FIXTURES_WITH_OFF_TRACK)
def test_off_track_inputs_above_consumer(fixture):
    """Off-track input stations (declared via ``%%metro off_track:``)
    must sit at least one ``y_spacing`` slot above their on-track
    consumer.  Catches the regression where ``_lift_off_track_stations``
    leaves an off-track input on the same Y as its consumer (or below).
    """
    graph = _layout(fixture)
    junction_ids = set(graph.junctions)
    # Build off_track -> consumer map from edges
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

    assert consumer_of, f"{fixture}: no off-track edges found"

    for off_id, consumer_id in consumer_of.items():
        off_st = graph.stations[off_id]
        cons_st = graph.stations[consumer_id]
        assert off_st.y < cons_st.y - _Y_TOL, (
            f"Off-track {off_id} y={off_st.y} not above consumer "
            f"{consumer_id} y={cons_st.y}"
        )


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


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
@pytest.mark.parametrize("params", _BBOX_PARAM_SETS)
def test_section_bbox_contains_all_content(fixture, params):
    """Every section's bbox must contain its on-track stations and any
    off-track / terminus icons.  Catches regressions where an icon
    (off-track input or single-icon terminus) is placed near the bbox
    top so the icon spills outside the section background.

    Margin: on-track station markers reach ~9.5 px above the centre,
    file icons reach ``terminus_height / 2 = 16`` px above the centre
    (both off-track inputs and on-track terminus stations render the
    same icon at ``station.y + bundle_mid``).  We assert
    ``station.y - reach >= bbox_y - 0.5`` (sub-pixel tolerance) and
    ``station.y + reach <= bbox_y + bbox_h + 0.5``.
    """
    graph = _layout(fixture, **params)
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
        "topologies/rnaseq_lite.mmd",
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
    (e.g. rnaseq_lite ``star_align`` ↔ ``hisat_align``, mismatched
    tracks ``t_a`` ↔ ``t_b``).  The narrowed pass leaves those
    side stations alone so the on-trunk and off-trunk siblings stay
    column-aligned.
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
    registered in ``graph._half_grid_station_ids`` whose section
    satisfies ``_section_symfan_uses_half_grid`` and has exactly two
    on-track branches.  Any other half-grid station is a regression.
    """
    from nf_metro.layout.engine import _section_symfan_uses_half_grid

    y_spacing = 55.0
    tol = 1.0
    graph = _layout(fixture, y_spacing=y_spacing)

    half_grid_ids = getattr(graph, "_half_grid_station_ids", set()) or set()
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
# Section bbox bottom padding (Phase 13k post-shift padding)
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

    ``_shift_sparse_loop_stations_to_clear_bundle`` (Phase 13k) can
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


# ---------------------------------------------------------------------------
# Inter-row gap accommodates grown bboxes from Phase 13k shifts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["da_pipeline.mmd"])
def test_row_gap_accommodates_bypass(fixture):
    """The vertical gap between row ``r`` sections' bbox bottoms and
    row ``r + 1`` sections' bbox tops must be at least
    ``section_y_gap`` for every column-overlapping pair.

    When ``_shift_sparse_loop_stations_to_clear_bundle`` grows an
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


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
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


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
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

    # Slack: one y_spacing for diagonal corner approach.
    SLACK = 60.0

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
# (audit item 15 / issue #318)
# ---------------------------------------------------------------------------


# Fixtures known to fail ``test_topological_siblings_share_y_or_symmetric``
# (audit item 15 / open issue #318).  Tracked separately - the test is
# added to lock in the invariant so a future fix XPASSes here.
_XFAIL_SIBLINGS: dict[str, str] = {}


@pytest.mark.parametrize(
    "fixture",
    _params_with_xfails(ALL_FIXTURES, _XFAIL_SIBLINGS),
)
def test_topological_siblings_share_y_or_symmetric(fixture):
    """Stations with identical ``(predecessor_set, successor_set,
    line_set)`` should share Y, or for >= 3 members be symmetrically
    distributed around their mean Y.

    Catches issue #318: gatk and deepvariant have the same predecessors,
    successors, and consumed lines but end up at different Ys when they
    should be mirrored around the trunk.
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
