"""Crossing-free opening order for semantic fan branches."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from nf_metro.layout.geometry import packed_section_visual_rank
from nf_metro.layout.route_topology import convergence_entry_port_id
from nf_metro.layout.routing.common import resolve_section, resolve_section_colrow
from nf_metro.parser.model import MetroGraph, PortSide, Station

if TYPE_CHECKING:
    from nf_metro.layout.fan_plans import FanTopologyQuery


def _fan_branch_entry_port(
    graph: MetroGraph,
    target_id: str,
    topology: FanTopologyQuery | None = None,
) -> str | None:
    """Return the entry port reached by one resolved divergence branch."""
    port = graph.ports.get(target_id)
    if port is not None:
        return target_id if port.is_entry else None
    if topology is not None:
        convergence = topology.convergence_for_junction(target_id)
        return convergence.entry_port_id if convergence is not None else None
    return convergence_entry_port_id(graph, target_id)


def _section_order_coordinate(graph: MetroGraph, station: Station) -> float | None:
    """Return settled X or a stable pre-placement packed-cell coordinate."""
    section = resolve_section(graph, station, prefer_upstream=False)
    if section is None:
        return None
    col, row = resolve_section_colrow(graph, station)
    if col is None or row is None:
        return None
    if section.bbox_w > 0:
        return station.x
    rank = packed_section_visual_rank(graph, section, col, row)
    return col * (len(graph.sections) + 1) + rank


class _SlotShape(NamedTuple):
    """What a branch needs of the bundle slot it opens on.

    Two branches can share one slot only when they agree on all three: they hop
    the same number of columns, they turn onto (or clear of) the near gap band
    the same way, and neither runs on the source row, where the lane is pinned
    at both ends by the entry port the run has to reach flat.
    """

    reach: int
    near_band: bool
    on_source_row: bool


def _traverses_near_band(side: PortSide | None, reach: int, row_delta: int) -> bool:
    """Whether a cross-row branch turns onto the gap band beside its source row.

    A far-side entry -- the port sits on the side the branch arrives from --
    cannot be reached by crossing into the target row: the branch traverses the
    inter-row gap beside the junction, then runs on outside the target box.
    Mirrors the ``is_wrap`` classification the fan corridor uses, restricted to a
    single-column hop, where the bypass kind (which turns onto the band past the
    target row instead) is structurally impossible.
    """
    if row_delta == 0:
        return False
    return (reach == 1 and side is PortSide.RIGHT) or (
        reach == -1 and side is PortSide.LEFT
    )


def _line_opening_order(
    branches: list[int],
    line_of: list[str],
    slot_shape: list[_SlotShape],
    stack_depth: list[float],
    peel_depth: list[float] | None = None,
) -> list[str] | None:
    """Collapse a per-branch opening order onto one slot per line.

    A line leaving one fan on several branches still occupies a single slot in
    the source bundle, so its branches have to sort as one contiguous block: a
    distinct line ranked between two of them cannot be served by either slot
    order, and the fan has no crossing-free per-line permutation.

    Contiguity alone is not enough to merge them.  Slots are handed out by index
    and each target's entry port inherits the lane its own branch holds, so
    merging two slots renumbers every slot beyond them and leaves the lines out
    there on lanes their ports never inherited.  Only the trailing slots can
    therefore merge.  The merged branches also have to want that one lane: they
    must share a *slot shape* -- the column reach and band classification the
    order sorts on -- and none of them may run on the source row, where the run
    is flat only while it keeps the lane its target's entry port inherited.

    The block also has to own every *stack depth* it spans -- ``stack_depth``
    being the coordinate this fan's lanes stack on, the one the incoming order
    sorted them by.  One slot across several depths leaves nothing to rank a
    distinct line that stacks at one of them: it reaches a different target at
    the same depth, so it reaches it by running past that depth, a descent the
    coordinate it tied on does not express.  Either way round, one of the two
    lays its riser across the other's flat run in.  The fan declines rather
    than claim a crossing-free order it has no coordinate to derive.

    That tie test is the whole ownership question only while the incoming order
    is itself sorted by the depth the lanes peel at: the block trails, so it is
    then the deepest thing in the fan and a tie is the only way a distinct line
    can reach past it.  An order sorted on some other coordinate carries no such
    guarantee, and callers on those orders pass ``peel_depth`` -- the distance
    from the source row at which each branch leaves the bundle.  The block runs
    innermost, so its peel-offs travel outward across every lane outside it that
    has not already turned away; the merge holds only while every distinct line
    peels strictly nearer the source row than the block's nearest peel.
    """
    order: list[str] = []
    for branch in branches:
        line_id = line_of[branch]
        if line_id in order:
            if order[-1] != line_id:
                return None
            continue
        order.append(line_id)
    if len(order) < 2:
        return None
    if len(branches) == len(order):
        return order
    trailing = order[-1]
    merged = [branch for branch in branches if line_of[branch] == trailing]
    if len(branches) - len(merged) != len(order) - 1:
        return None
    shapes = {slot_shape[branch] for branch in merged}
    if len(shapes) != 1 or shapes.pop().on_source_row:
        return None
    spanned = {stack_depth[branch] for branch in merged}
    if any(
        stack_depth[branch] in spanned
        for branch in branches
        if line_of[branch] != trailing
    ):
        return None
    if peel_depth is not None:
        nearest = min(peel_depth[branch] for branch in merged)
        if any(
            peel_depth[branch] >= nearest
            for branch in branches
            if line_of[branch] != trailing
        ):
            return None
    return order


def fanout_divergence_peel_order(
    graph: MetroGraph,
    junction_id: str,
    line_priority: dict[str, int],
    topology: FanTopologyQuery | None = None,
) -> list[str] | None:
    """Return the crossing-free opening order for a clean divergence.

    The result runs outermost to innermost at the shared turn. Unsupported or
    ambiguous groups return ``None`` and keep declaration order.
    """
    junction = graph.stations.get(junction_id)
    if junction is None:
        return None
    source_col, source_row = resolve_section_colrow(graph, junction)
    if source_col is None or source_row is None:
        return None
    source_x = _section_order_coordinate(graph, junction)
    if source_x is None:
        return None

    line_of: list[str] = []
    near_band: set[int] = set()
    reach: list[int] = []
    row_delta: list[int] = []
    target_x: list[float] = []
    entry_side: list[PortSide | None] = []
    claimed: dict[str, str] = {}
    converging = False
    for edge in graph.edges_from(junction_id):
        entry_id = _fan_branch_entry_port(graph, edge.target, topology)
        if entry_id is None:
            return None
        converging |= entry_id != edge.target
        entry = graph.stations[entry_id]
        target_col, target_row = resolve_section_colrow(graph, entry)
        if target_col is None or target_row is None:
            return None
        if entry_id in claimed and claimed[entry_id] != edge.line_id:
            return None
        coordinate = _section_order_coordinate(graph, entry)
        if coordinate is None:
            return None
        branch = len(line_of)
        line_of.append(edge.line_id)
        claimed[entry_id] = edge.line_id
        reach.append(target_col - source_col)
        row_delta.append(target_row - source_row)
        target_x.append(coordinate)
        entry_port = graph.ports.get(entry_id)
        entry_side.append(entry_port.side if entry_port is not None else None)
        if _traverses_near_band(entry_side[branch], reach[branch], row_delta[branch]):
            near_band.add(branch)

    if len(reach) < 2:
        return None

    branches = range(len(line_of))
    slot_shape = [
        _SlotShape(reach[branch], branch in near_band, row_delta[branch] == 0)
        for branch in branches
    ]

    def opening_order(
        branch_order: list[int],
        stack_depth: list[float],
        peel_depth: list[float] | None = None,
    ) -> list[str] | None:
        return _line_opening_order(
            branch_order, line_of, slot_shape, stack_depth, peel_depth
        )

    def priority(branch: int) -> int:
        return line_priority.get(line_of[branch], 0)

    if len(set(reach)) > 1 and set(row_delta) == {0}:
        # Every branch stays on the source row, so each leaves the shared run at
        # its own target column.  A fan whose branches all climb over the row --
        # into a TOP entry, or round to a flow-side entry facing away from the
        # source -- peels each branch down off the run above it, so the
        # farthest-reaching branch runs outermost, on top, and every nearer one
        # peels off inside the siblings that travel on.  Any other branch may
        # leave below the row, which leaves the fan no single side to order by.
        def climbs(branch: int) -> bool:
            side = entry_side[branch]
            return side is PortSide.TOP or (
                (side is PortSide.RIGHT and reach[branch] > 0)
                or (side is PortSide.LEFT and reach[branch] < 0)
            )

        if PortSide.TOP not in entry_side or not all(map(climbs, branches)):
            return None
        return opening_order(
            sorted(
                branches, key=lambda branch: (-abs(reach[branch]), priority(branch))
            ),
            [float(abs(reach[branch])) for branch in branches],
        )
    if len(set(reach)) > 1 and 0 in row_delta:
        if len({value > 0 for value in row_delta if value != 0}) != 1:
            return None

        descent_sign = 1 if next(v for v in row_delta if v != 0) > 0 else -1

        # Lanes stack in target-row order.  A near-band branch never reaches its
        # target row through the box, so it stacks at the gap it turns into --
        # half a row from the source, on the descent side, which keeps its
        # onward riser clear of the runs of branches that return to the source
        # row.
        def band_depth(branch: int) -> float:
            if branch in near_band:
                return descent_sign * 0.5
            return row_delta[branch]

        return opening_order(
            sorted(
                branches,
                key=lambda branch: (
                    band_depth(branch),
                    (
                        abs(reach[branch])
                        if row_delta[branch] == 0
                        else -abs(reach[branch])
                    ),
                    priority(branch),
                ),
            ),
            [band_depth(branch) for branch in branches],
        )

    if converging:
        return None

    if len(set(reach)) == 1:
        descenders = [value for value in row_delta if value != 0]
        if len({value > 0 for value in descenders}) != 1:
            return None
        if len(set(row_delta)) < 2:
            if len(set(target_x)) < 2:
                return None
            drop_down = descenders[0] > 0
            return opening_order(
                sorted(
                    branches,
                    key=lambda branch: (
                        (
                            abs(target_x[branch] - source_x)
                            if drop_down
                            else -abs(target_x[branch] - source_x)
                        ),
                        priority(branch),
                    ),
                ),
                [abs(target_x[branch] - source_x) for branch in branches],
            )
        return opening_order(
            sorted(
                branches,
                key=lambda branch: (row_delta[branch], priority(branch)),
            ),
            [float(row_delta[branch]) for branch in branches],
        )

    if len({value > 0 for value in row_delta}) != 1:
        return None
    drop_down = row_delta[0] > 0
    # Every branch here leaves the source row, so the lanes stack on the column
    # reach rather than on a depth: |reach| is what the order below sorts by, and
    # it is also the quantity a shared slot shape forces equal, so it can rank a
    # distinct line only against a block that already ties with it.  The peel
    # depth is what a merged block actually spreads over, so it travels
    # alongside for the ownership test.
    return opening_order(
        sorted(
            branches,
            key=lambda branch: (
                -abs(reach[branch]) if drop_down else abs(reach[branch]),
                priority(branch),
            ),
        ),
        [float(abs(reach[branch])) for branch in branches],
        [float(abs(row_delta[branch])) for branch in branches],
    )
