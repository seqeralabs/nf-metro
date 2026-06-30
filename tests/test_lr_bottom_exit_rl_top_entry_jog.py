"""Regression: an LR bottom exit dropping into a stacked ``direction: RL`` top entry.

When an LR section's BOTTOM exit drops straight into the TOP entry of the
``direction: RL`` section stacked directly below it, the descending bundle must
land each line on the same per-line X the entry's intra-section drop departs from.

The exit carries only the lines shared across the seam, while the receiving RL
section reserves offset slots for a line that peels off deeper inside it.  The
exit reflects its bundle within its own (narrower) present-line width, so the
descent must inherit the entry's wider anchoring or it lands one ``OFFSET_STEP``
off the entry port.

``serpentine_rl_bundle`` exercises the same seam with no reserved slot, where the
two anchorings coincide, so it pins that a reserved-slot fix leaves the
already-straight seam straight.

See issue #1212.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nf_metro.layout.engine import compute_layout
from nf_metro.layout.routing.core import route_edges_centred
from nf_metro.layout.routing.invariants import check_perp_entry_boundary_consistent
from nf_metro.layout.routing.offsets import compute_station_offsets
from nf_metro.parser import parse_metro_mermaid

TOPOLOGIES = Path(__file__).parent.parent / "examples" / "topologies"

FIXTURES = [
    "lr_bottom_exit_rl_top_entry_jog",
    "serpentine_rl_bundle",
]


def _boundary_violations(name: str):
    graph = parse_metro_mermaid((TOPOLOGIES / f"{name}.mmd").read_text())
    compute_layout(graph)
    offsets = compute_station_offsets(graph)
    routes = route_edges_centred(graph, station_offsets=offsets)
    return check_perp_entry_boundary_consistent(graph, routes)


@pytest.mark.parametrize("name", FIXTURES)
def test_perp_drop_lands_straight_into_top_entry(name: str) -> None:
    """No line reverses lateral direction crossing the stacked TOP entry port."""
    violations = _boundary_violations(name)
    assert not violations, "\n".join(v.message() for v in violations)
