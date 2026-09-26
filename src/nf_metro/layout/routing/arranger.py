"""Direction-agnostic section lane arranger.

A section arranges its internal lanes from its *boundary configuration*: the
order in which lines cross its edges.  When a line crosses the determining edge
at slot ``k`` and rides lane ``k``, the lines run parallel and never cross by
construction.

This module owns the reduction at the heart of that idea -- mapping a boundary
edge's crossing order to a lane order -- and nothing else.  The reduction is
axis-free: a line's position *along* an edge (an X coordinate on a TOP/BOTTOM
edge, a Y coordinate on a LEFT/RIGHT edge) is resolved by the caller before it
reaches here, so the same reduction serves any flow direction.

Callers supply the determining edge's crossing order: fan-out divergence
supplies its exit edge's peel order, and reconvergence supplies its entry
edge's primary-feeder order.  The edge derivation -- which lines cross, in what
order -- stays with each caller.  The two reductions differ only in the
lines the edge does not constrain: :func:`lane_order` stacks them behind the
determining lines, while :func:`peel_lane_order` leaves each on its own lane.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryConfig:
    """A section's lane-determining boundary configuration.

    :param present: every line on the section, in the section's default
        (priority) lane order.
    :param determining: the lines that cross the determining edge, in the order
        they cross it along that edge.  Lines absent from *present* are ignored;
        lines in *present* but absent here are unconstrained.
    """

    present: tuple[str, ...]
    determining: tuple[str, ...]


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
    rest = tuple(
        sorted(present - set(determining), key=lambda lid: line_priority.get(lid, 0))
    )
    order = determining + rest
    priority_order = tuple(
        sorted(config.present, key=lambda lid: line_priority.get(lid, 0))
    )
    if order == priority_order:
        return None
    return order


def peel_lane_order(
    config: BoundaryConfig,
    line_priority: dict[str, int],
    *,
    leading: Iterable[str] = (),
    trailing: Iterable[str] = (),
) -> tuple[str, ...] | None:
    """A fan-out section's lane order, or ``None`` when it matches priority.

    A shared exit fan fixes only the order of the lines it peels, so those
    lines are dealt, in edge order, across the lanes they hold in priority
    order, and every line the fan does not carry keeps its own lane.  That
    line's lane is the one it arrives on or leaves from: pushing it past the
    fan would jog its run beside the section and leave a dead lane wherever
    it meets a fan line.  The exception is a line pinned to one side of the
    bundle by a crossing elsewhere -- *leading* lines take the front lanes and
    *trailing* lines the back, in priority order -- since from the far side
    its turn through that crossing would pass over the bundle.  A line the fan
    carries is never pinned.
    """
    present = set(config.present)
    determining = [lid for lid in config.determining if lid in present]
    fan = set(determining)
    pinned_front = present.intersection(leading) - fan
    pinned_back = present.intersection(trailing) - fan - pinned_front
    priority_order = sorted(config.present, key=lambda lid: line_priority.get(lid, 0))
    dealt = iter(determining)
    middle = [
        next(dealt) if lid in fan else lid
        for lid in priority_order
        if lid not in pinned_front and lid not in pinned_back
    ]
    order = tuple(
        [lid for lid in priority_order if lid in pinned_front]
        + middle
        + [lid for lid in priority_order if lid in pinned_back]
    )
    if order == tuple(priority_order):
        return None
    return order
