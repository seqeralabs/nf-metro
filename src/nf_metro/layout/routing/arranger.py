"""Direction-agnostic section lane arranger.

A section is treated as a black box that arranges its internal lanes from its
**boundary configuration** alone: the order in which lines cross its edges.
When the order a line crosses the determining edge matches the lane it rides,
the lines run parallel and never cross *by construction*.

This module owns the reduction at the heart of that idea -- mapping a boundary
edge's crossing order to a section's lane order -- and nothing else.  The
reduction is axis-free: a line's position *along* an edge (an X coordinate on a
TOP/BOTTOM edge, a Y coordinate on a LEFT/RIGHT edge) is resolved by the caller
via :class:`~nf_metro.layout.geometry.AxisFrame` before it reaches here, so the
same code serves LR, RL and TB.

Today two callers feed it, both LR/RL:

* a fan-out section reads its **exit** edge -- the peel order at the shared
  downstream fan -- so the bundle leaves in the order its lines diverge;
* a reconvergence section reads its **entry** edge -- the primary feeder's
  order -- so the bundle arrives in the order its lines are fed.

The edge-*derivation* (which lines cross, in what order) stays with each
caller; only the order-to-lanes reduction lives here.  Reading a section's edge
crossings directly from its boundary geometry -- the step that lets a TB
section join through the same path -- is layered on top of this primitive
elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BoundaryEdge(Enum):
    """Which boundary edge's crossing order determines a section's lanes."""

    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True)
class BoundaryConfig:
    """A section's lane-determining boundary configuration.

    :param present: every line on the section, already in the section's default
        (priority) lane order.
    :param determining: the lines that cross the determining edge, in the order
        they cross it along that edge.  Lines absent from *present* are ignored;
        lines in *present* but absent here are unconstrained.
    :param edge: which edge *determining* was read from -- recorded so callers
        and tests can reason about which boundary fixed the arrangement.
    """

    present: tuple[str, ...]
    determining: tuple[str, ...]
    edge: BoundaryEdge


def lane_order(
    config: BoundaryConfig, line_priority: dict[str, int]
) -> tuple[str, ...] | None:
    """The section's lane order, or ``None`` when it already matches priority.

    Lane *k* carries the *k*-th line crossing the determining edge, so a line
    crossing at edge-slot *k* rides lane *k*; the lines the edge does not
    constrain fall to the back of the bundle in priority order.  ``None`` means
    the resulting order is the plain priority order, so no re-slot is needed.
    """
    present = set(config.present)
    determining = tuple(lid for lid in config.determining if lid in present)
    determining_set = set(determining)
    rest = tuple(
        sorted(present - determining_set, key=lambda lid: line_priority.get(lid, 0))
    )
    order = determining + rest
    priority_order = tuple(
        sorted(config.present, key=lambda lid: line_priority.get(lid, 0))
    )
    if order == priority_order:
        return None
    return order
