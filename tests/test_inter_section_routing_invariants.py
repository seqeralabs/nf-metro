"""Inter-section and intra-section routing invariants.

These tests catch the U-shaped detour that occurs when a fan-out
section places a single-line passthrough station far off the trunk
row.  The line then dives from the hub trunk Y down to the station's
per-line track Y and climbs back up to the exit-port Y for no
payload reason (the symptom described in #317).

The invariant: for a station S that is the only consumer of its line
within an LR/RL section, with one predecessor P and one successor T,
S's Y must lie within the [min(P.y, T.y), max(P.y, T.y)] band so the
route doesn't dip below (or rise above) the source-to-target band.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# 1px slack absorbs sub-pixel rounding from fan-recenter passes.
_Y_TOL = 1.0


def _layout_example(name: str, **kwargs) -> MetroGraph:
    text = (EXAMPLES / name).read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph, **kwargs)
    return graph


def _in_section_edges(
    graph: MetroGraph, section_id: str
) -> tuple[dict[str, list], dict[str, list]]:
    """Return per-target and per-source edge maps restricted to a section."""
    in_by_tgt: dict[str, list] = defaultdict(list)
    out_by_src: dict[str, list] = defaultdict(list)
    sids = set(graph.sections[section_id].station_ids)
    for e in graph.edges:
        if e.source in sids and e.target in sids:
            in_by_tgt[e.target].append(e)
            out_by_src[e.source].append(e)
    return in_by_tgt, out_by_src


@pytest.mark.parametrize(
    "example_name",
    ["variantbenchmarking.mmd", "variantbenchmarking_auto.mmd"],
)
def test_single_line_passthrough_station_stays_in_pred_succ_band(
    example_name: str,
) -> None:
    """Single-line passthrough stations must sit in the [pred.y, succ.y] band.

    A passthrough station S in an LR/RL section is one whose line has
    no other stop in the same section, with exactly one same-section
    predecessor and one same-section successor.  Placing S outside
    the [min(P.y, T.y), max(P.y, T.y)] band produces a U-shaped
    detour visible in the rendered route (#317).
    """
    graph = _layout_example(example_name)

    # Count non-port, non-hidden stations per (section, line) so we
    # can identify lines with exactly one real stop in the section.
    # Hub/port markers are excluded because every line passes through
    # the section's entry/exit ports by definition.
    stations_per_section_line: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or not st.section_id:
            continue
        # Skip stations that carry the section's full bundle: they're
        # hub/trunk markers, not single-line stops.
        lines = graph.station_lines(sid)
        if len(lines) > 1:
            continue
        for lid in lines:
            stations_per_section_line[(st.section_id, lid)].append(sid)

    failures: list[str] = []
    for section in graph.sections.values():
        if section.direction not in ("LR", "RL") or section.bbox_h <= 0:
            continue
        in_by_tgt, out_by_src = _in_section_edges(graph, section.id)
        for sid in section.station_ids:
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden or st.off_track:
                continue
            lines = graph.station_lines(sid)
            if len(lines) != 1:
                continue
            (lid,) = lines
            if len(stations_per_section_line.get((section.id, lid), [])) != 1:
                continue
            ins = in_by_tgt.get(sid, [])
            outs = out_by_src.get(sid, [])
            if len(ins) != 1 or len(outs) != 1:
                continue
            pred = graph.stations.get(ins[0].source)
            succ = graph.stations.get(outs[0].target)
            if pred is None or succ is None:
                continue
            band_lo = min(pred.y, succ.y) - _Y_TOL
            band_hi = max(pred.y, succ.y) + _Y_TOL
            if not (band_lo <= st.y <= band_hi):
                failures.append(
                    f"{section.id}/{sid} (line={lid}): "
                    f"y={st.y:.2f} not in [{band_lo:.2f}, {band_hi:.2f}] "
                    f"(pred={pred.y:.2f}, succ={succ.y:.2f})"
                )

    assert not failures, (
        f"Single-line passthrough stations out of pred/succ band in "
        f"{example_name}:\n  " + "\n  ".join(failures)
    )
