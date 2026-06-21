"""Regression tests for issue #705 (reconvergence reversed fold).

The fixture is a serpentine-fold reconvergence pipeline.  Two defects were
reported; this PR fixes two of the three crossing sites plus the offset:

* **Defect 2 - return-row offset.**  ``final_report`` is an independent
  reversed reconvergence section.  ``_reorder_reconvergence`` built its bundle
  order top-down, but ``_apply_section_line_order`` draws a reversed section
  bottom-up, so the returning line landed on the top slot and pushed the whole
  bundle one offset low.  The ``bio_interp -> final_report`` back-run then sloped
  (almost horizontal) instead of running level.

* **Defect 1, junction_14.**  ``preprocessing`` fans out to three analysis
  sections stacked in one column.  ``fanout_divergence_peel_order`` only handled
  fans spreading across columns, so this vertical fan kept declaration order and
  ``atac``/``protein`` crossed inside the fan corner.  Ordering the vertical fan
  by target row settles the bundle on the straight run.

The ``integration`` merge and ``junction_15`` crossings are out of scope: they
are coupled through ``integration``'s single TB trunk and need the bottom-exit
fan ordered by row depth (tracked separately).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.context as routing_context
import nf_metro.layout.routing.offsets as routing_offsets
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.context import fanout_divergence_peel_order
from nf_metro.layout.routing.invariants import (
    _first_axis_crossing,
    _route_axis_segments,
)
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "examples" / "topologies" / "reconverge_reversed_fold.mmd"


def _layout():
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, offsets


def _preprocessing_fanout_junction(graph) -> str:
    """The single junction ``preprocessing`` fans out through."""
    exits = {
        e.target
        for pid in graph.sections["preprocessing"].exit_ports
        for e in graph.edges_from(pid)
        if e.target in graph.junction_ids
    }
    assert len(exits) == 1, exits
    return next(iter(exits))


# --- Defect 2: return-row back-run runs level ---------------------------------


def test_return_row_back_run_is_axis_aligned():
    """The bio_interp -> final_report back-run is flat, not shallow-sloped."""
    from layout_validator import check_almost_horizontal_edges

    graph, _ = _layout()
    sloped = [
        v
        for v in check_almost_horizontal_edges(graph)
        if "bio_interp__exit" in v.message and "final_report__entry" in v.message
    ]
    assert not sloped, "\n".join(v.message for v in sloped)


def test_final_report_bundle_is_top_anchored():
    """final_report's bundle uses the top slot (offset 0), not one step low."""
    graph, offsets = _layout()
    bundle = {
        lid: offsets[("fr_aggregate", lid)]
        for lid in graph.station_lines("fr_aggregate")
    }
    assert min(bundle.values()) == pytest.approx(0.0), bundle


def test_return_row_drops_off_top_slot_with_forward_order(monkeypatch):
    """A reversed section reordered with the forward logical arrangement
    (``continuing + returning``) puts the returning line on the top slot and
    drops the bundle one offset low - the state that slopes the back-run - so
    the reversed-aware order is load-bearing."""

    def reorder_forward_logical(ctx, section_local):
        graph = ctx.graph
        for sec_id, section in graph.sections.items():
            if not section.entry_ports:
                continue
            line_feeder = routing_offsets._section_line_feeders(ctx, section)
            if not line_feeder:
                continue
            lines_by_feeder = {}
            for lid, fid in line_feeder.items():
                lines_by_feeder.setdefault(fid, []).append(lid)
            if len(lines_by_feeder) < 2:
                continue
            primary_fid = max(lines_by_feeder, key=lambda f: len(lines_by_feeder[f]))
            primary_lines = set(lines_by_feeder[primary_fid])
            if len(primary_lines) < 2:
                continue
            primary_order = section_local.get(primary_fid, ctx.line_priority)
            continuing = sorted(primary_lines, key=lambda lid: primary_order.get(lid, 0))
            sec_present = routing_offsets._section_present_line_set(ctx, sec_id)
            returning = sorted(
                sec_present - primary_lines,
                key=lambda lid: ctx.line_priority.get(lid, 0),
            )
            new_order = continuing + returning
            global_ordered = sorted(
                sec_present, key=lambda lid: ctx.line_priority.get(lid, 0)
            )
            if new_order == global_ordered:
                continue
            routing_offsets._apply_section_line_order(ctx, sec_id, new_order)

    monkeypatch.setattr(
        routing_offsets, "_reorder_reconvergence", reorder_forward_logical
    )
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    bundle = {
        lid: offsets[("fr_aggregate", lid)]
        for lid in graph.station_lines("fr_aggregate")
    }
    assert min(bundle.values()) > 0.0, bundle


# --- Defect 1: vertical fan peels without a corner crossing --------------------


def test_vertical_fan_peel_order_follows_target_rows():
    """The same-column fan orders its lead-in by target row, top to bottom."""
    graph, _ = _layout()
    jid = _preprocessing_fanout_junction(graph)
    line_priority = {lid: i for i, lid in enumerate(graph.lines.keys())}
    order = fanout_divergence_peel_order(graph, jid, line_priority)
    assert order == ["rna", "atac", "protein"], order


def test_preprocessing_fanout_has_no_corner_crossing():
    """The three lines peeling out of preprocessing's fan never cross."""
    graph, offsets = _layout()
    jid = _preprocessing_fanout_junction(graph)
    routes = route_edges(graph, station_offsets=offsets)
    fan = [r for r in routes if r.edge.source == jid and len(r.points) >= 2]
    for i in range(len(fan)):
        for j in range(i + 1, len(fan)):
            a, b = fan[i], fan[j]
            if a.line_id == b.line_id:
                continue
            va, ha = _route_axis_segments(a)
            vb, hb = _route_axis_segments(b)
            hit = _first_axis_crossing(va, hb) or _first_axis_crossing(vb, ha)
            assert hit is None, (
                f"{a.line_id}/{b.line_id} cross at {hit} leaving {jid}"
            )


def test_vertical_fan_crosses_without_peel_order(monkeypatch):
    """With the vertical-fan peel order disabled the fan keeps declaration order
    and the corner crossing returns, proving the order is what fixes it."""
    real = routing_context.fanout_divergence_peel_order

    def no_vertical_fan(graph, jid, line_priority):
        order = real(graph, jid, line_priority)
        if order is None:
            return None
        # Drop the order only for the same-column (vertical) fan so the
        # column-spreading fans keep their fix.
        jst = graph.stations.get(jid)
        reach = set()
        for edge in graph.edges_from(jid):
            tgt = graph.stations.get(edge.target)
            col, _ = routing_context._resolve_section_colrow(graph, tgt)
            scol, _ = routing_context._resolve_section_colrow(graph, jst)
            if col is not None and scol is not None:
                reach.add(col - scol)
        return None if len(reach) == 1 else order

    monkeypatch.setattr(
        routing_context, "fanout_divergence_peel_order", no_vertical_fan
    )
    monkeypatch.setattr(
        routing_offsets, "fanout_divergence_peel_order", no_vertical_fan
    )
    graph = parse_metro_mermaid(FIXTURE.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    jid = _preprocessing_fanout_junction(graph)
    routes = route_edges(graph, station_offsets=offsets)
    fan = [r for r in routes if r.edge.source == jid and len(r.points) >= 2]
    crossings = []
    for i in range(len(fan)):
        for j in range(i + 1, len(fan)):
            a, b = fan[i], fan[j]
            if a.line_id == b.line_id:
                continue
            va, ha = _route_axis_segments(a)
            vb, hb = _route_axis_segments(b)
            hit = _first_axis_crossing(va, hb) or _first_axis_crossing(vb, ha)
            if hit is not None:
                crossings.append((a.line_id, b.line_id, hit))
    assert crossings, "expected a fan corner crossing with the peel order disabled"
