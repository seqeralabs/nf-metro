"""Invariants asserting non-consumer marker bypass across guide fixtures.

Companion to ``test_layout_invariants.py``'s
``test_lines_dont_cross_non_consumer_markers``, which only parametrizes
over ``da_pipeline.mmd`` and ``rnaseq_sections.mmd``.  This file
parametrizes the same invariant over the guide-family fixtures whose
topology produces a non-consumer crossing that the differential-abundance
trigger in ``_insert_bypass_stations`` historically did not catch.

The 5 fixtures are:

* ``examples/guide/05_file_icons.mmd`` -- ``qc`` from ``trim`` to
  reporting crosses ``align``'s marker on the way to the section exit.
* ``examples/guide/05c_files_icon.mmd`` -- same pattern as 05.
* ``examples/guide/05d_folder_icon.mmd`` -- same pattern as 05.
* ``examples/guide/06a_without_hidden.mmd`` -- ``prot`` from ``search``
  to reporting crosses ``quant``'s marker as the fan-up to trunk
  begins past ``quant``'s column.
* ``examples/guide/06b_with_hidden.mmd`` -- same pattern as 06a.

A second set of tests guards the over-trigger side: the bypass insertion
should NOT fire for multi-trunk sections (``rnaseq_auto``'s
``genome_align``, ``epitopeprediction``'s ``input_processing``) or for
single-trunk sections whose only consumed line is a local spur
(``with_subworkflows``'s ``samtools_index``).  Those fixtures'
routing engine already clears the markers via track consolidation, and
adding a V there inflates section height without visual benefit.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.engine import _station_marker_bbox, compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import is_bypass_v
from nf_metro.render.svg import apply_route_offsets

GUIDE = Path(__file__).resolve().parent.parent / "examples" / "guide"

_GUIDE_BYPASS_FIXTURES = [
    "05_file_icons.mmd",
    "05c_files_icon.mmd",
    "05d_folder_icon.mmd",
    "06a_without_hidden.mmd",
    "06b_with_hidden.mmd",
]


def _layout_guide(name: str):
    text = (GUIDE / name).read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    return graph


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


@pytest.mark.parametrize("fixture", _GUIDE_BYPASS_FIXTURES)
def test_guide_lines_dont_cross_non_consumer_markers(fixture):
    """No rendered line segment may pass through the marker bbox of any
    station that neither consumes nor produces that line.
    """
    graph = _layout_guide(fixture)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    consumed_by: dict[str, set[str]] = defaultdict(set)
    produced_by: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        consumed_by[e.target].add(e.line_id)
        produced_by[e.source].add(e.line_id)

    for sid in graph.stations:
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


# Fixtures whose section topology already provides a parallel track
# for the bypassing line (multi-trunk sections, or single-trunk
# sections whose only consumed line at S is a local spur).  The
# bypass insertion must not fire on these - it would only inflate
# section height or push a row down without visual benefit.
_BYPASS_QUIET_FIXTURES = [
    "examples/rnaseq_auto.mmd",
    "examples/epitopeprediction.mmd",
]


@pytest.mark.parametrize("rel_path", _BYPASS_QUIET_FIXTURES)
def test_no_bypass_inserted_for_quiet_fixtures(rel_path):
    text = (Path(__file__).resolve().parent.parent / rel_path).read_text()
    graph = parse_metro_mermaid(text)
    bypass_ids = [sid for sid in graph.stations if is_bypass_v(sid)]
    assert bypass_ids == [], (
        f"{rel_path}: expected no bypass stations, got {bypass_ids}"
    )


def _nonconsumer_crossings(graph, only_station: str | None = None) -> list[str]:
    """Return messages for every line drawn through a non-consumer marker.

    Restricting to ``only_station`` scopes the check to one station, which lets
    a fixture carrying an unrelated, out-of-scope crossing still assert the
    targeted marker is clear.
    """
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    consumed_or_produced: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        consumed_or_produced[e.target].add(e.line_id)
        consumed_or_produced[e.source].add(e.line_id)
    found: list[str] = []
    for sid in graph.stations:
        if only_station is not None and sid != only_station:
            continue
        bbox = _station_marker_bbox(graph, sid, offsets=offsets)
        if bbox is None:
            continue
        for r in routes:
            if r.line_id in consumed_or_produced.get(sid, set()):
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = apply_route_offsets(r, offsets)
            if any(
                _seg_crosses_bbox(pts[k], pts[k + 1], bbox) for k in range(len(pts) - 1)
            ):
                found.append(f"line {r.line_id!r} crosses non-consumer {sid!r}")
    return found


def test_inrow_express_skip_bows_around_skipped_marker():
    """An express line skipping a collinear station bows around its marker.

    Three collinear stations ``s1 -> s2 -> s3`` (line ``a``) with an express
    ``s1 -> s3`` (line ``b``): topology alone cannot tell the express is drawn
    through ``s2``'s marker, so the geometric bypass pass must catch and bow
    it (#990).
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "topologies"
        / "inrow_skip_breeze.mmd"
    )
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    assert not _nonconsumer_crossings(graph), _nonconsumer_crossings(graph)
    assert any(is_bypass_v(sid) for sid in graph.stations), (
        "expected a geometric bypass-V helper to be inserted"
    )


def test_folded_genome_align_express_clears_skipped_marker():
    """A folded multi-trunk column must not draw a line through a skipped marker.

    ``rnaseq_auto --fold-threshold 1`` folds ``genome_align`` to a TB column
    where ``hisat2`` (via ``umi_tools_dedup``) exits past ``salmon_quant``,
    which it does not consume (#990).
    """
    path = Path(__file__).resolve().parent.parent / "examples" / "rnaseq_auto.mmd"
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=1)
    compute_layout(graph)
    assert not _nonconsumer_crossings(graph, only_station="salmon_quant"), (
        _nonconsumer_crossings(graph, only_station="salmon_quant")
    )


@pytest.mark.parametrize("name", _GUIDE_BYPASS_FIXTURES)
def test_is_bypass_v_recognises_resolve_generated_helpers(name):
    """The helper ids ``resolve`` builds are recognised by ``is_bypass_v`` and
    carry the V's defining traits (hidden, label-less).  Pins the
    producer/consumer pairing so renaming the prefix stays a one-site change
    rather than silently splitting the id-builder from its predicate.
    """
    graph = _layout_guide(name)
    flagged = [st for sid, st in graph.stations.items() if is_bypass_v(sid)]
    assert flagged, f"{name}: expected at least one bypass-V helper"
    for st in flagged:
        assert st.is_hidden, f"{name}: {st.id!r} flagged as bypass-V but is visible"
        assert not st.label.strip(), f"{name}: bypass-V {st.id!r} carries a label"
