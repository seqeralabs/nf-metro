"""The granted-clearance arm of short-destination cohort settling bites.

``_settle_plannable_short_destination_cohorts`` publishes no settled turn on any
shipped or generated input, which makes the arm that runs once a boundary
clearance is *granted* look unreachable.  It is not: the grant is the only route
to the re-seating loop at the end of that pass, and emptying the granted-owner
set changes the SVG of the fixture named here.

The effect is visible only along the render's settlement path -- laying the
fixture out and re-routing it from scratch reproduces the same coordinates
either way -- so the assertion is made on the rendered document.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from nf_metro.api import RenderConfig, prepare_graph, render_graph
from nf_metro.layout.routing import member_geometry
from nf_metro.themes import resolve_theme

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "topologies"
    / "same_destination_short_overlap.mmd"
)


def _rendered_digest() -> str:
    graph = prepare_graph(FIXTURE.read_text(), source_dir=str(FIXTURE.parent))
    svg = render_graph(graph, resolve_theme(graph), RenderConfig())
    return hashlib.sha256(svg.encode()).hexdigest()


def test_emptying_the_granted_clearance_owners_changes_the_render() -> None:
    """Withdrawing the grant must move the fixture the granted arm re-seats."""
    granted = _rendered_digest()

    name = "_settle_plannable_short_destination_cohorts"
    original = getattr(member_geometry, name)

    def without_grant(*args: Any, **kwargs: Any) -> Any:
        kwargs["granted_clearance_owner_ids"] = frozenset()
        return original(*args, **kwargs)

    setattr(member_geometry, name, without_grant)
    try:
        withheld = _rendered_digest()
    finally:
        setattr(member_geometry, name, original)

    assert granted != withheld, (
        "the granted-clearance arm no longer changes "
        f"{FIXTURE.name}; either the arm has gone inert or this fixture no "
        "longer reaches it"
    )
