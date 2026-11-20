"""Regression tests for issue #705 (reconvergence reversed fold).

The fixture is a serpentine-fold reconvergence pipeline whose lines fan out to
stacked analysis sections and reconverge onto a reversed return row.  Each test
pins one routing property the layout must hold:

* **Return-row offset.**  ``final_report`` is a reversed reconvergence section
  whose primary feeder is a flat-frame neighbour, so its continuing lines carry
  the feeder's offsets and the ``bio_interp -> final_report`` back-run is level.

* **Vertical fan-out (junction_14).**  ``preprocessing`` fans out to three
  analysis sections stacked in one column; the same-column fan orders by target
  row, so the lines peel without crossing inside the corner.

* **TB merge-in (integration entry).**  ``integration`` is a TB section fed by
  three single-line feeders from distinct rows; its bundle stacks in feeder-row
  order so the merge is concentric and the bundle leaves the section's bottom
  with the shared lines (rna+atac) adjacent rather than split around protein.

The remaining junction_15 crossing -- rna+atac turning into Biological
Interpretation cross protein on its straight run down to the deeper Technical QC
row -- is a geometrically-required over/under, not a defect, so the tests below
allow it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import OFFSET_STEP
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
            assert hit is None, f"{a.line_id}/{b.line_id} cross at {hit} leaving {jid}"


# --- TB merge-in concentric; bottom-exit descent is a tight bundle ------------


def test_integration_entry_orders_bundle_by_feeder_row():
    """integration's TB bundle stacks rna<atac<protein, matching feeder rows 0/1/2."""
    graph, offsets = _layout()
    bundle = {
        lid: offsets[("int_merge", lid)] for lid in graph.station_lines("int_merge")
    }
    assert bundle["rna"] < bundle["atac"] < bundle["protein"], bundle


def test_integration_merge_in_is_concentric():
    """The lines rising into integration's left entry don't cross each other."""
    graph, offsets = _layout()
    routes = route_edges(graph, station_offsets=offsets)
    feeds = [
        r
        for r in routes
        if r.edge.target == "integration__entry_left_10" and len(r.points) >= 2
    ]
    for i in range(len(feeds)):
        for j in range(i + 1, len(feeds)):
            a, b = feeds[i], feeds[j]
            va, ha = _route_axis_segments(a)
            vb, hb = _route_axis_segments(b)
            hit = _first_axis_crossing(va, hb) or _first_axis_crossing(vb, ha)
            assert hit is None, (
                f"{a.line_id}/{b.line_id} cross entering integration at {hit}"
            )


def test_bottom_exit_descent_keeps_rna_atac_tight():
    """rna+atac descend from junction_15 on adjacent channels, protein not wedged
    between them, so the bio_interp bundle enters tight.  protein crosses them on
    the straight run as it carries on to the deeper tech_qc row -- that over/under
    is geometrically required, not the gap this guards against."""
    graph, offsets = _layout()
    routes = route_edges(graph, station_offsets=offsets)
    descent_x = {
        r.line_id: max(x for x, _y in r.points)
        for r in routes
        if r.edge.source in graph.junction_ids
        for tgt in [graph.stations.get(r.edge.target)]
        if tgt is not None and tgt.section_id in ("bio_interp", "tech_qc")
    }
    if not {"rna", "atac", "protein"} <= set(descent_x):
        pytest.skip("junction_15 descent not present")
    assert abs(descent_x["rna"] - descent_x["atac"]) <= OFFSET_STEP + 1, descent_x
    lo, hi = sorted((descent_x["rna"], descent_x["atac"]))
    assert not (lo < descent_x["protein"] < hi), descent_x
