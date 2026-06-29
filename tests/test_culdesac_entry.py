"""A line entering a section on its flow-trailing edge to reach an interior
station must not fold back over its own trunk (#1182).

When a section's producers all sit to one side, a line enters on the section's
flow-*trailing* edge (LEFT on an RL section, RIGHT on an LR one -- the same edge
the section flows out of), yet its first consumer is an interior station held
off the leading end (e.g. by a file terminus).  Drawn flat, the entry leg runs
the full trunk to reach that consumer and the line doubles straight back -- a
same-side hairpin that covers a stretch of the trunk in opposing directions.

The fix lifts the entry leg onto a track clear of the trunk and drops it
perpendicular onto the consumer, so the entry leg and the return trunk leg ride
two separate tracks (a cul-de-sac U).
"""

from __future__ import annotations

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.phases.guards import iter_opposing_line_overlaps
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import apply_route_offsets
from nf_metro.parser.mermaid import parse_metro_mermaid

CULDESAC = "examples/topologies/same_side_culdesac.mmd"


def _layout(path: str, *, validate: bool, fold: int | None = None):
    text = open(path).read()
    graph = parse_metro_mermaid(
        text, **({"max_station_columns": fold} if fold is not None else {})
    )
    compute_layout(graph, validate=validate)
    return graph


def test_same_side_culdesac_validates() -> None:
    """The minimal cul-de-sac map lays out without raising.

    A flat entry leg would cover a stretch of the trunk in opposing directions
    and ``compute_layout(validate=True)`` would raise ``PhaseInvariantError`` on
    the opposing-overlap guard; this end-to-end check pins that it does not.
    """
    _layout(CULDESAC, validate=True)


def _mid_entry_leg(graph):
    """The routed leg from the ``mid`` section's entry port to its consumer."""
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    for r in routes:
        port = graph.ports.get(r.edge.source)
        if port is not None and port.is_entry and port.section_id == "mid":
            return apply_route_offsets(r, offsets)
    return None


def test_same_side_culdesac_entry_leg_lifts_off_trunk() -> None:
    """The entry leg leaves the trunk row before reaching its interior consumer.

    A single-Y entry leg is the folded shape; the lifted leg spans more than one
    Y, riding an off-trunk track to drop perpendicular onto the consumer.
    """
    graph = _layout(CULDESAC, validate=False)
    pts = _mid_entry_leg(graph)
    assert pts is not None
    ys = {round(y, 1) for _, y in pts}
    assert len(ys) > 1, f"entry leg stayed flat on the trunk: {pts}"


def test_same_side_culdesac_entry_leg_has_no_foldback() -> None:
    """No opposing-direction overlap involves the cul-de-sac entry leg."""
    graph = _layout(CULDESAC, validate=False)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    entry_ports = {pid for pid, p in graph.ports.items() if p.is_entry}
    offending = [
        ov
        for ov in iter_opposing_line_overlaps(graph, offsets=offsets, routes=routes)
        if ov.src_a in entry_ports or ov.src_b in entry_ports
    ]
    assert not offending, f"entry leg folds back: {offending}"


@pytest.mark.parametrize("fold", [3, 5, 7, 9, 11])
def test_genomeassembly_polishing_entry_no_foldback(fold: int) -> None:
    """The polishing same-side hairpin entry leg (#1182) does not fold back.

    genomeassembly under a lowered fold has a separate, fold-induced
    inter-section fan tangle (#1187) that keeps the whole map from validating
    clean, tracked by the strict-xfail
    ``test_genomeassembly_renders_clean_under_lowered_fold``.  This narrower
    check pins the part this fix owns: the entry-port leg into the interior
    consumer must not be one half of an opposing overlap.
    """
    graph = _layout("examples/genomeassembly.mmd", validate=False, fold=fold)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)
    polishing_entries = {
        pid
        for pid, p in graph.ports.items()
        if p.is_entry and p.section_id == "polishing"
    }
    offending = [
        ov
        for ov in iter_opposing_line_overlaps(graph, offsets=offsets, routes=routes)
        if ov.src_a in polishing_entries or ov.src_b in polishing_entries
    ]
    assert not offending, f"polishing entry leg folds back at fold {fold}: {offending}"
