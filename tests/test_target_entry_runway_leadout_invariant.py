"""A RIGHT-entry cross-row wrap's lead-out must clear same-line sibling descents.

A RIGHT-entry cross-row wrap leads out of its source, turns down into a
bypass band, then runs on to the target column.  When a same-line descent from
another source already runs down the gap just right of the source column, a
source-hugging lead-out corner turns down a few px from it and the two turns
read as one merged corner (the "self-meet").  The lead-out must carry its
horizontal further right and turn down clear of that sibling descent.

Covers:

* Targeted: ``target_entry_runway_bypass`` -- the Branch A l1 output turns
  down to the RIGHT of both feeder descents that share its gap.
* Corpus happy-path: no shipped fixture routes two same-line, different-edge
  vertical descents so close in one gap that they merge (bar the one
  documented dogleg-exempt case), so the fix introduces no new merges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.constants import COORD_TOLERANCE, CURVE_RADIUS
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges_centred
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = REPO_ROOT / "examples" / "topologies"
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# A deliberately-exempt same-line dogleg (see test_dogleg_exempt_trunk_invariant):
# its two ``wrap`` legs sit a dogleg apart by design, so the merged-descent
# heuristic below flags it as a known, accepted exemption rather than a defect.
KNOWN_MERGED_DESCENT_EXEMPTIONS = {"dogleg_exempt_sameline.mmd"}


def _route(path: Path):
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    return graph, route_edges_centred(graph, station_offsets=offsets)


def _vertical_segments(route) -> list[tuple[float, float, float]]:
    """``(x, y_lo, y_hi)`` of each vertical leg of *route*."""
    segs = []
    for a, b in zip(route.points, route.points[1:]):
        if abs(a[0] - b[0]) < COORD_TOLERANCE and abs(a[1] - b[1]) > COORD_TOLERANCE:
            segs.append((a[0], min(a[1], b[1]), max(a[1], b[1])))
    return segs


def _descent_x_in_band(
    routes, source: str, target_prefix: str, x_lo: float, x_hi: float
) -> float:
    """X of the ``source -> target`` route's vertical leg inside ``[x_lo, x_hi]``."""
    for r in routes:
        if r.edge.source == source and r.edge.target.startswith(target_prefix):
            xs = [x for x, _lo, _hi in _vertical_segments(r) if x_lo <= x <= x_hi]
            assert xs, f"{source}->{target_prefix} has no vertical leg in the gap"
            return min(xs)
    raise AssertionError(f"no route {source}->{target_prefix}")


def test_branch_a_leadout_clears_feeder_descents() -> None:
    """Branch A's l1 output turns down to the right of the feeder descents that
    share the gap just right of its column, so no two turns self-meet."""
    _graph, routes = _route(FIXTURES / "target_entry_runway_bypass.mmd")

    # The Branch A / Branch B column gap the three lines descend through.
    gap_lo, gap_hi = 1408.0, 1472.0
    branch_a_leadout = _descent_x_in_band(
        routes, "branch_a__exit_right_3", "branch_b__entry_right", gap_lo, gap_hi
    )
    feeder_l1_descent = _descent_x_in_band(
        routes, "__junction_13", "feeder_l1__entry_right", gap_lo, gap_hi
    )
    feeder_l2_turn = _descent_x_in_band(
        routes, "__junction_14", "target__entry_left", gap_lo, gap_hi
    )

    assert branch_a_leadout > feeder_l1_descent + CURVE_RADIUS, (
        f"Branch A lead-out {branch_a_leadout} self-meets the Feeder L1 descent "
        f"{feeder_l1_descent}"
    )
    assert branch_a_leadout > feeder_l2_turn + CURVE_RADIUS, (
        f"Branch A lead-out {branch_a_leadout} clashes with the Feeder L2 turn "
        f"{feeder_l2_turn}"
    )


def _merged_same_line_descents(routes) -> list[str]:
    """Same-line, different-edge vertical descents that merge into one corner.

    Two inter-section verticals of the same line that neither coincide (a fused
    track, X within tolerance) nor separate clearly (X at least a curve radius
    apart) draw as one thick doubled corner where their Y ranges overlap.
    """
    inter = [r for r in routes if getattr(r, "is_inter_section", False)]
    hits: list[str] = []
    for i, ra in enumerate(inter):
        for rb in inter[i + 1 :]:
            if ra.edge.line_id != rb.edge.line_id:
                continue
            if (ra.edge.source, ra.edge.target) == (rb.edge.source, rb.edge.target):
                continue
            for xa, la, ha in _vertical_segments(ra):
                for xb, lb, hb in _vertical_segments(rb):
                    dx = abs(xa - xb)
                    overlap = min(ha, hb) - max(la, lb)
                    if (
                        COORD_TOLERANCE < dx <= CURVE_RADIUS
                        and overlap > COORD_TOLERANCE
                    ):
                        hits.append(
                            f"{ra.edge.line_id}: {ra.edge.source}->{ra.edge.target} "
                            f"(x={xa:.0f}) merges {rb.edge.source}->{rb.edge.target} "
                            f"(x={xb:.0f})"
                        )
    return hits


def _corpus_fixtures() -> list[Path]:
    return (
        sorted(TOPOLOGIES.glob("*.mmd"))
        + sorted(EXAMPLES.glob("*.mmd"))
        + sorted(FIXTURES.glob("*.mmd"))
    )


@pytest.mark.parametrize(
    "path", _corpus_fixtures(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_no_merged_same_line_descents(path: Path) -> None:
    """No shipped fixture merges two same-line descents in one gap (bar the one
    documented dogleg-exempt case)."""
    if path.name in KNOWN_MERGED_DESCENT_EXEMPTIONS:
        pytest.skip("documented same-line dogleg exemption")
    _graph, routes = _route(path)
    merged = _merged_same_line_descents(routes)
    assert not merged, "\n".join(merged)
