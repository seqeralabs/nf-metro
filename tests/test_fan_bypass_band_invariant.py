"""C3: a fanning junction's same-line bypass branches share one below-row band.

A junction that fans one metro line out to several bypass targets must run the
span those branches share as ONE trunk -- splitting only where each branch turns
into its port -- not as two parallel same-colour tracks a full offset-step-plus
apart.  The :class:`FanCorridor` pins one shared below-row bypass band per
fanning junction (the deepest sibling's ``bypass_bottom_y``) and the bypass
handler consumes it, so the branches coincide by construction rather than being
reconciled afterward.

Covers ``longread_variant_calling`` and ``convergent_offrow_exit_climb``, whose
SV-VCF bypass fans doubled onto two bands before the shared band, and proves the
check is meaningful by disabling the corridor's bypass band.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nf_metro.layout.routing.context as routing_context
from nf_metro.layout.constants import COORD_TOLERANCE, resolve_offset_step
from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing import compute_station_offsets, route_edges
from nf_metro.layout.routing.common import iter_horizontal_trunks
from nf_metro.parser.mermaid import parse_metro_mermaid

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

FIXTURES = [
    "longread_variant_calling.mmd",
    "topologies/convergent_offrow_exit_climb.mmd",
]


def _same_line_traverse_spreads(path: Path) -> list[tuple[str, str, float]]:
    """Y-spread of each ``(source, line)`` group of overlapping traverse legs.

    Groups every inter-section horizontal trunk by its source junction and line,
    keeps only groups whose x-spans overlap (a span the branches genuinely
    share), and reports the group's Y-spread.  A spread above one offset step
    means the shared span renders as two parallel same-colour tracks.
    """
    graph = parse_metro_mermaid(path.read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges(graph, station_offsets=offsets)

    legs: dict[tuple[str, str], list[tuple[float, float, float]]] = {}
    for rp in routes:
        if not rp.is_inter_section:
            continue
        for _k, seg in iter_horizontal_trunks(rp):
            legs.setdefault((rp.edge.source, rp.line_id), []).append(
                (seg.y, seg.x_lo, seg.x_hi)
            )

    spreads: list[tuple[str, str, float]] = []
    for (source, line), members in legs.items():
        if len(members) < 2:
            continue
        lo = max(m[1] for m in members)
        hi = min(m[2] for m in members)
        if hi - lo <= COORD_TOLERANCE:
            continue  # no genuinely shared span
        ys = [m[0] for m in members]
        spreads.append((source, line, max(ys) - min(ys)))
    return spreads


@pytest.mark.parametrize("fixture", FIXTURES)
def test_same_line_bypass_fans_share_one_band(fixture: str) -> None:
    """Same-line bypass fan branches sharing an x-span read as one trunk."""
    graph = parse_metro_mermaid((EXAMPLES / fixture).read_text())
    step = resolve_offset_step(graph.track_gap)
    for source, line, spread in _same_line_traverse_spreads(EXAMPLES / fixture):
        assert spread <= step + COORD_TOLERANCE, (
            f"{fixture}: line '{line}' from '{source}' traverses a shared span on "
            f"bands {spread:.1f}px apart -- two parallel same-colour tracks"
        )


@pytest.mark.parametrize("fixture", FIXTURES)
def test_checker_fires_without_bypass_band(
    fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling the corridor's shared bypass band reproduces the doubled
    traverses, proving the invariant is not vacuous."""
    monkeypatch.setattr(routing_context, "_fan_bypass_band", lambda *a, **k: None)
    graph = parse_metro_mermaid((EXAMPLES / fixture).read_text())
    step = resolve_offset_step(graph.track_gap)
    spreads = _same_line_traverse_spreads(EXAMPLES / fixture)
    assert any(spread > step + COORD_TOLERANCE for _s, _l, spread in spreads), (
        f"{fixture}: expected a doubled same-line bypass traverse with the "
        "shared band disabled"
    )
