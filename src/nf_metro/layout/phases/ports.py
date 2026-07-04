"""Entry/exit port positioning and alignment on section boundaries."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    ENTRY_SHIFT_TB,
    EXIT_CORRIDOR_ICON_CLEARANCE,
    MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC,
    MIN_PORT_STATION_GAP,
    MIN_STATION_FLAT_LENGTH,
    SAME_COORD_TOLERANCE,
    STATION_ELBOW_TOLERANCE,
)
from nf_metro.layout.geometry import AxisFrame, lanes_run_along_x, lanes_run_along_y
from nf_metro.layout.phases._common import (
    _expand_bbox_for_y,
    _grid_group_section_ids,
    _is_fold_section,
    _lr_exit_aligned_target,
    exit_entry_ports_face,
    flow_exit_carrier_anchor,
    iter_fold_lr_exits_short_of_target,
)
from nf_metro.layout.phases.bbox import _predict_section_content_bottom
from nf_metro.layout.phases.guards import (
    _exit_off_consumer_trunk,
    _section_lacks_flow_aligned_port,
)
from nf_metro.layout.phases.junctions import (
    _resolve_source_section_id,
    _resolve_source_xy,
)
from nf_metro.parser.model import Edge, MetroGraph, Port, PortSide, Section, Station


def _set_port_y(graph: MetroGraph, port_id: str, y: float) -> None:
    """Set the Y coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.y = y
    if port:
        port.y = y


def _set_port_x(graph: MetroGraph, port_id: str, x: float) -> None:
    """Set the X coordinate on both the station and port objects."""
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if station:
        station.x = x
    if port:
        port.x = x


def _align_entry_ports(graph: MetroGraph, vertical_only: bool = False) -> None:
    """Align entry ports with their incoming connection's coordinates.

    LEFT/RIGHT ports: align Y for straight horizontal runs.
    TOP/BOTTOM ports: align X for vertical drops or Y for cross-column.

    With ``vertical_only`` set, only ports on vertical-flow (TB/BT) sections
    are re-aligned -- used by the late re-alignment pass (Stage 6.16), which
    targets the perpendicular-entry drift that the vertical-settling phases
    introduce in vertical-flow sections.  Re-aligning the horizontal-flow
    (LR/RL) sections there would instead drag their ports off the positions
    those same phases deliberately settled them into.
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue

        entry_section = graph.sections.get(port.section_id)
        if not entry_section:
            continue

        if vertical_only and lanes_run_along_y(entry_section.direction):
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_entry_port(graph, port_id, port, entry_section, junction_ids)
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            _align_tb_entry_port(graph, port_id, port, entry_section, junction_ids)


def _entry_consumer_y(
    graph: MetroGraph, port_id: str, entry_section: Section
) -> float | None:
    """Y of the first internal station this entry port feeds, nearest the port."""
    ys = [
        st.y
        for edge in graph.edges_from(port_id)
        if (st := graph.stations.get(edge.target)) is not None
        and not st.is_port
        and st.section_id == entry_section.id
    ]
    port_st = graph.stations.get(port_id)
    if not ys or port_st is None:
        return None
    return min(ys, key=lambda y: abs(y - port_st.y))


def _align_lr_entry_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT entry port's Y with its incoming source."""
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue

        # Derive effective source coordinates (computes junction
        # placement on-the-fly so we don't need pre-positioned junctions).
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_x, src_y = src_xy

        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        src_section = graph.sections.get(src_section_id) if src_section_id else None
        if not src_section:
            continue

        if entry_section.grid_row != src_section.grid_row:
            _lift_perp_entry_port_above_stations(graph, entry_section, port, port_id)
            break

        # A source exit whose Y is a structural boundary, not a consumer-aligned
        # trunk row, pins this entry off the consumer's row and forces a diagonal
        # into the first station: a perpendicular exit on any section, or any
        # exit on a vertical-flow (TB/BT) section (whose flow-aligned TOP/BOTTOM
        # exit dips onto the section's bottom/top edge below or above its
        # stations).  Anchor the entry on its own consumer station's Y so the
        # route rises in the inter-section gap and enters horizontally.
        src_port = graph.ports.get(edge.source)
        if (
            src_port is not None
            and not src_port.is_entry
            and _exit_off_consumer_trunk(src_port, src_section)
        ):
            if lanes_run_along_x(entry_section.direction):
                # A vertical-flow LEFT/RIGHT exit feeding this vertical-flow
                # LEFT/RIGHT entry is a horizontal seam between two vertical
                # trunks.  When the two flow in OPPOSITE directions (an upward BT
                # handing off to a downward TB, or the reverse) the feeder's
                # trailing edge and the consumer's leading edge sit on the same
                # side, so the consumer mirrors the feeder across the seam.  Any
                # other feed (same-direction TB->TB, or a perpendicular exit)
                # keeps the level-then-drop lift above the head.
                mirrored = (
                    src_port.side in (PortSide.LEFT, PortSide.RIGHT)
                    and lanes_run_along_x(src_section.direction)
                    and _opposite_vertical_flow(src_section, entry_section)
                    and _mirror_entry_section_to_seam(
                        graph, entry_section, port_id, edge.source
                    )
                )
                if not mirrored:
                    _lift_perp_entry_port_above_stations(
                        graph, entry_section, port, port_id
                    )
                break
            consumer_y = _entry_consumer_y(graph, port_id, entry_section)
            if consumer_y is not None:
                _set_port_y(graph, port_id, consumer_y)
            break

        # Skip alignment if source Y is too far outside entry section bbox.
        # Allow moderate expansion so ports align when adjacent sections
        # have different track counts (#165).
        entry_station = graph.stations.get(port_id)
        if entry_station:
            bbox_top = entry_section.bbox_y
            bbox_bot = entry_section.bbox_y + entry_section.bbox_h
            max_expand = entry_section.bbox_h * MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC
            if src_y < bbox_top - max_expand or src_y > bbox_bot + max_expand:
                break
            # Expand bbox to contain aligned port if needed
            if src_y < bbox_top or src_y > bbox_bot:
                _expand_bbox_for_y(entry_section, src_y)

        target_y = src_y

        # Clamp for TB sections with perpendicular entry
        if entry_section.direction == "TB" and port.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            target_y = _clamp_tb_entry_port(
                graph,
                entry_section,
                target_y,
                edge,
                src,
                junction_ids,
            )

        _set_port_y(graph, port_id, target_y)
        break


def _opposite_vertical_flow(a: Section, b: Section) -> bool:
    """Whether two vertical-flow sections run in opposite directions (TB vs BT)."""
    return AxisFrame.flow_sign(a.direction) != AxisFrame.flow_sign(b.direction)


def _vertical_exit_trailing_y(graph: MetroGraph, exit_port_id: str) -> float | None:
    """Y of the internal station feeding *exit_port_id*, or ``None``.

    The trailing station a vertical-flow section's LEFT/RIGHT exit continues
    from -- the station whose Y a downstream seam should mirror.
    """
    exit_st = graph.stations.get(exit_port_id)
    if exit_st is None:
        return None
    ys = [
        graph.stations[e.source].y
        for e in graph.edges_to(exit_port_id)
        if e.source in graph.stations and not graph.stations[e.source].is_port
    ]
    if not ys:
        return None
    return min(ys, key=lambda y: abs(y - exit_st.y))


def _mirror_entry_section_to_seam(
    graph: MetroGraph,
    entry_section: Section,
    entry_port_id: str,
    exit_port_id: str,
) -> bool:
    """Slide a vertical-flow consumer so it mirrors its feeder across the seam.

    The consumer's leading station (the one its LEFT/RIGHT entry feeds) is
    shifted level with the feeder's trailing station, carrying the whole section
    with it, then the entry port is seated on the feeding exit's Y (the seam).
    The two vertical trunks then occupy one band either side of the gap.
    Returns ``False`` (mirror not applied) when the feeder or consumer station
    can't be resolved.
    """
    exit_st = graph.stations.get(exit_port_id)
    feeder_trailing_y = _vertical_exit_trailing_y(graph, exit_port_id)
    consumer = next(
        (
            graph.stations[e.target]
            for e in graph.edges_from(entry_port_id)
            if e.target in graph.stations and not graph.stations[e.target].is_port
        ),
        None,
    )
    if exit_st is None or feeder_trailing_y is None or consumer is None:
        return False
    delta = feeder_trailing_y - consumer.y
    if abs(delta) > SAME_COORD_TOLERANCE:
        for sid in entry_section.station_ids:
            station = graph.stations.get(sid)
            if station:
                station.y += delta
            p = graph.ports.get(sid)
            if p:
                p.y += delta
        entry_section.bbox_y += delta
    _set_port_y(graph, entry_port_id, exit_st.y)
    if exit_st.y < entry_section.bbox_y:
        _expand_bbox_for_y(entry_section, exit_st.y)
    return True


def _align_tb_entry_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    entry_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a TOP/BOTTOM entry port with its incoming sources."""
    # Collect all incoming sources.  Coordinates are derived via
    # _resolve_source_xy so junctions don't need to be pre-positioned.
    sources: list[tuple[float, float, str | None]] = []
    for edge in graph.edges_to(port_id):
        src = graph.stations.get(edge.source)
        if not src or not (src.is_port or edge.source in junction_ids):
            continue
        src_xy = _resolve_source_xy(graph, edge.source, junction_ids)
        if src_xy is None:
            continue
        src_section_id = _resolve_source_section_id(graph, edge.source, junction_ids)
        sources.append((src_xy[0], src_xy[1], src_section_id))

    if not sources:
        return

    # A TOP/BOTTOM entry into a TB/BT section feeds the head of the vertical
    # trunk.  Place the port on its boundary edge with X on the trunk so the
    # port -> first-station segment is a clean vertical continuation; routing
    # bridges the source across to the trunk X (a right-down-left-down lead-in
    # when the source sits off to the side).  Inheriting the source X instead
    # leaves the port off the trunk, forcing an elbow inside the section and,
    # for cross-column sources, a route that crosses the boundary off-port.
    if entry_section.direction in ("TB", "BT"):
        if port.side == PortSide.TOP:
            boundary_y = entry_section.bbox_y
        else:
            boundary_y = entry_section.bbox_y + entry_section.bbox_h
        _set_port_y(graph, port_id, boundary_y)
        trunk_x = _tb_trunk_x(graph, entry_section)
        if trunk_x is not None:
            _set_port_x(graph, port_id, trunk_x)
        return

    # Check if any source is cross-column
    my_cols = set(
        range(
            entry_section.grid_col,
            entry_section.grid_col + entry_section.grid_col_span,
        )
    )
    is_cross_column = False
    for _, _, src_sid in sources:
        src_sec = graph.sections.get(src_sid) if src_sid else None
        if src_sec:
            src_cols = set(
                range(src_sec.grid_col, src_sec.grid_col + src_sec.grid_col_span)
            )
            if not (src_cols & my_cols):
                is_cross_column = True
                break

    if is_cross_column:
        # Cross-column: snap the port Y to its boundary edge.
        # The source Y must NOT drag the port off its designated boundary —
        # a same-row producer sits at the section's vertical centre, which is
        # strictly inside the bbox, so clamping-within-bbox alone leaves the
        # port off-boundary and trips _guard_ports_on_boundaries.
        if port.side == PortSide.TOP:
            boundary_y = entry_section.bbox_y
        else:
            boundary_y = entry_section.bbox_y + entry_section.bbox_h
        _set_port_y(graph, port_id, boundary_y)
        # A same-column source stacked directly above/below drops in
        # vertically; align X to it (clear of internal stations) so the
        # mixed-source case still gets a straight drop, not a jog.
        drop_x = _vertical_drop_source_x(graph, port, sources, entry_section, my_cols)
        if drop_x is not None:
            _set_port_x(graph, port_id, drop_x)
        # Only nudge X for LR/RL sections where TOP/BOTTOM ports are perpendicular
        elif entry_section.direction in ("LR", "RL"):
            _nudge_port_from_stations(port_id, entry_section, graph)
    else:
        # Same grid column: a stacked source drops in vertically, so align X
        # to it -- but only when that X lands within the section's own box.  A
        # same-column neighbour whose actual X sits outside the box (a wide
        # upstream section sharing the column) would otherwise drag the perp
        # port off the section's columns; keep the port on its own column and
        # let routing bridge the cross-column drop instead.
        src_x, _, _ = sources[0]
        if _drop_x_within_section(entry_section, src_x):
            _set_port_x(graph, port_id, src_x)
        elif entry_section.direction in ("LR", "RL"):
            _nudge_port_from_stations(port_id, entry_section, graph)
            graph._cross_column_perp_bridges.add(entry_section.id)


def _tb_trunk_x(graph: MetroGraph, section: Section) -> float | None:
    """X of a TB/BT section's vertical trunk (its internal stations' column).

    Returns the median internal-station X, or ``None`` when the section has no
    internal stations.  Internal stations in a TB/BT section stack on a single
    column, so the median ignores any port outliers and yields the trunk X.
    """
    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    xs = sorted(
        graph.stations[sid].x
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    )
    if not xs:
        return None
    return xs[len(xs) // 2]


def _drop_x_within_section(section: Section, drop_x: float) -> bool:
    """True when *drop_x* lands within the section's own grid column (bbox).

    A perpendicular entry port aligns its X to a feeding source only for an
    in-column drop -- one whose X falls inside the section's box.  A source X
    outside the box (a wider neighbour sharing the grid column, or a producer
    in a different column entirely) would drag the port -- and the run that
    opens a station-elbow gap from it -- off the section's columns and past
    its bbox; the port stays on its own column and routing bridges the
    cross-column drop with an L-shaped inter-section lead-in instead.
    """
    return section.bbox_x <= drop_x <= section.bbox_x + section.bbox_w


def _vertical_drop_source_x(
    graph: MetroGraph,
    port: Port,
    sources: list[tuple[float, float, str | None]],
    entry_section: Section,
    my_cols: set[int],
    tolerance: float = STATION_ELBOW_TOLERANCE,
) -> float | None:
    """X for a clean vertical drop from a same-column stacked source.

    Returns the source X if a single same-column source sits in the row
    directly above (TOP) / below (BOTTOM) and that X is clear of the entry
    section's internal stations; otherwise None (no straight drop available).
    """
    candidate_xs: set[float] = set()
    for sx, sy, src_sid in sources:
        src_sec = graph.sections.get(src_sid) if src_sid else None
        if not src_sec:
            continue
        src_cols = set(
            range(src_sec.grid_col, src_sec.grid_col + src_sec.grid_col_span)
        )
        if not (src_cols & my_cols):
            continue
        if port.side == PortSide.TOP and src_sec.grid_row >= entry_section.grid_row:
            continue
        if port.side == PortSide.BOTTOM and src_sec.grid_row <= entry_section.grid_row:
            continue
        candidate_xs.add(sx)

    if len(candidate_xs) != 1:
        return None
    drop_x = next(iter(candidate_xs))

    internal_ids = (
        set(entry_section.station_ids)
        - set(entry_section.entry_ports)
        - set(entry_section.exit_ports)
    )
    for sid in internal_ids:
        st = graph.stations.get(sid)
        if st and not st.is_port and abs(st.x - drop_x) < tolerance:
            return None
    return drop_x


def _nudge_port_from_stations(
    port_id: str,
    section: Section,
    graph: MetroGraph,
    tolerance: float = STATION_ELBOW_TOLERANCE,
) -> None:
    """Nudge a TOP/BOTTOM port away from any internal station at the same X.

    Moves the port toward the entry side of the section so it doesn't
    visually pass through a station marker (station-as-elbow).
    """
    station = graph.stations.get(port_id)
    port = graph.ports.get(port_id)
    if not station or not port:
        return

    internal_ids = (
        set(section.station_ids) - set(section.entry_ports) - set(section.exit_ports)
    )
    internal_xs = [
        graph.stations[sid].x
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_xs:
        return

    # Check if port X coincides with any internal station X
    if not any(abs(station.x - ix) < tolerance for ix in internal_xs):
        return

    # Move port toward the entry side of the section
    # For LR: entry is left, so move port left (toward bbox_x)
    # For RL: entry is right, so move port right (toward bbox_x + bbox_w)
    if section.direction == "RL":
        new_x = max(internal_xs) + tolerance
        # Clamp within bbox
        new_x = min(new_x, section.bbox_x + section.bbox_w - tolerance)
    else:
        new_x = min(internal_xs) - tolerance
        # Clamp within bbox
        new_x = max(new_x, section.bbox_x + tolerance)

    station.x = new_x
    port.x = new_x


def _align_ports_to_downstream(graph: MetroGraph) -> None:
    """Pull exit-entry port pairs toward downstream station positions.

    After entry ports are aligned to their source (exit port), the
    exit-entry pair may sit at a Y that is far from the downstream
    section's internal stations, forcing lines to detour vertically
    between sections.  This pass moves both ports toward the downstream
    section's average station Y when that would reduce the detour.

    Only applies to non-fold LR/RL sections without fan-out junctions
    (fold/TB sections are handled by ``_align_exit_ports``).
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue

        # Skip fold sections (handled by _align_exit_ports)
        if _is_fold_section(exit_section):
            continue

        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        # Find the single target entry port (skip fan-out via junctions)
        target_entry_id: str | None = None
        for edge in graph.edges_from(port_id):
            if edge.target in junction_ids:
                # Fan-out to junction -- don't override
                target_entry_id = None
                break
            tgt = graph.stations.get(edge.target)
            if tgt and tgt.is_port:
                tgt_port = graph.ports.get(edge.target)
                if tgt_port and tgt_port.is_entry:
                    target_entry_id = edge.target
                    # Keep scanning to detect junctions on later edges

        if not target_entry_id:
            continue

        # Locate the downstream section and its internal stations
        entry_port_obj = graph.ports.get(target_entry_id)
        if not entry_port_obj:
            continue
        entry_section = graph.sections.get(entry_port_obj.section_id)
        if not entry_section:
            continue

        # Skip cross-row connections (different grid rows)
        if exit_section.grid_row != entry_section.grid_row:
            continue

        # Skip when the line does not enter horizontally at its consumer's Y.
        # A LEFT/RIGHT port on a TB section bends in, and a TOP/BOTTOM port
        # drops in vertically (on any section); aligning the upstream exit to
        # the consumer's internal Y there pulls the exit off the trunk and
        # bends the lead-in.
        if entry_port_obj.side in (PortSide.TOP, PortSide.BOTTOM) or (
            entry_section.direction in ("TB", "BT")
            and entry_port_obj.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            continue

        # A wrapped connection (ports on the same side) gains no straight run
        # from aligning the exit to the downstream row, so leave it on its
        # carrier row.
        if not exit_entry_ports_face(port, entry_port_obj, exit_section, entry_section):
            continue

        internal_ids = (
            set(entry_section.station_ids)
            - set(entry_section.entry_ports)
            - set(entry_section.exit_ports)
        )
        downstream_ys: list[float] = []
        for edge in graph.edges_from(target_entry_id):
            if edge.target in internal_ids:
                downstream_ys.append(graph.stations[edge.target].y)
        if not downstream_ys:
            continue

        if graph.diamond_style == "straight":
            # Snap to the Y that the most lines target, so the majority
            # of lines flow straight.  Ties broken by topmost (smallest Y).
            y_counts: Counter[float] = Counter(downstream_ys)
            target_y = min(y_counts, key=lambda y: (-y_counts[y], y))
        else:
            target_y = sum(downstream_ys) / len(downstream_ys)

        if graph.center_ports:
            # Centre on the shorter section's midpoint.  Skip when
            # centring would create a V-shaped detour (center_y is
            # outside the range spanned by upstream and downstream Ys),
            # but allow it when both Ys match (no existing detour).
            exit_internal_ids = (
                set(exit_section.station_ids)
                - set(exit_section.entry_ports)
                - set(exit_section.exit_ports)
            )
            upstream_ys: list[float] = []
            for edge in graph.edges_to(port_id):
                if edge.source in exit_internal_ids:
                    upstream_ys.append(graph.stations[edge.source].y)
            if upstream_ys:
                upstream_y = sum(upstream_ys) / len(upstream_ys)
                shorter = min(exit_section, entry_section, key=lambda s: s.bbox_h)
                center_y = shorter.bbox_y + shorter.bbox_h / 2
                lo = min(upstream_y, target_y)
                hi = max(upstream_y, target_y)
                if lo <= center_y <= hi or abs(upstream_y - target_y) < 1.0:
                    target_y = center_y

        # Only move if target_y fits within both section bboxes
        exit_top = exit_section.bbox_y
        exit_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (exit_top <= target_y <= exit_bot):
            continue

        entry_top = entry_section.bbox_y
        entry_bot = entry_section.bbox_y + entry_section.bbox_h
        if not (entry_top <= target_y <= entry_bot):
            continue

        _set_port_y(graph, port_id, target_y)
        _set_port_y(graph, target_entry_id, target_y)


def _snap_sole_layer_stations_to_ports(graph: MetroGraph) -> None:
    """Snap port-connected stations to their port Y when alone in their layer.

    After port alignment, a port may sit at a different Y than its
    connected internal station, producing a diagonal.  When that station
    is the only one at its layer within the section, it can safely move
    to the port Y without colliding with layer-siblings.
    """
    # Build set of section IDs that participated in grid alignment.
    # Stage 4.2 must not override the shared Y grid for these sections.
    grid_group_secs = _grid_group_section_ids(graph)

    for section in graph.sections.values():
        # Only applies to horizontal (LR/RL) sections where ports
        # are on LEFT/RIGHT and the free axis is Y.
        if section.direction not in ("LR", "RL"):
            continue

        # Skip grid-group sections: their stations are on a shared Y
        # grid and must not be pulled off-grid by port alignment.
        if section.id in grid_group_secs:
            continue

        port_ids = section.port_ids
        internal_ids = set(section.station_ids) - port_ids

        # Skip single-station sections: the station is already centred
        # in the section, and snapping it to a port just drags it to
        # an extreme position.
        if len(internal_ids) <= 1:
            continue

        # Build layer -> set of station IDs for collision checking.
        layer_groups: dict[int, set[str]] = {}
        for sid in internal_ids:
            st = graph.stations.get(sid)
            if st and not st.is_port and hasattr(st, "layer"):
                layer_groups.setdefault(st.layer, set()).add(sid)

        # For each LEFT/RIGHT port, check its connected internal stations.
        # Skip TOP/BOTTOM ports - snapping to a boundary port Y would
        # pull stations to the section edge rather than aligning them
        # with a horizontal predecessor.
        for pid in port_ids:
            port_obj = graph.ports.get(pid)
            if not port_obj or port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
                continue
            port_st = graph.stations.get(pid)
            if not port_st:
                continue
            port_y = port_st.y

            # Collect the distinct internal stations connected to this port.
            connected: set[str] = set()
            for edge in graph.edges_from(pid):
                if edge.target in internal_ids:
                    connected.add(edge.target)
            for edge in graph.edges_to(pid):
                if edge.source in internal_ids:
                    connected.add(edge.source)

            # Only snap when exactly one station connects to the port
            # (not a fan-in / fan-out bundle).
            if len(connected) != 1:
                continue

            # Snap the port-connected station if it's a sole layer
            # occupant and has no other predecessors/successors on the
            # port side (otherwise snapping would break those connections).
            current = next(iter(connected))
            is_entry = graph.ports[pid].is_entry

            # Skip if the station has internal predecessors (other than
            # the port).  For entry ports this means the station receives
            # from another source inside the section; for exit ports it
            # means internal stations feed into it, so snapping would
            # create a diagonal on those connections.
            has_internal_pred = any(
                edge.source != pid and edge.source in internal_ids
                for edge in graph.edges_to(current)
            )
            if has_internal_pred:
                continue

            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                st = graph.stations[current]

                layer = getattr(st, "layer", None)
                if layer is None:
                    break
                siblings = layer_groups.get(layer, set())
                if len(siblings) > 1:
                    break

                if abs(st.y - port_y) >= 1.0:
                    st.y = port_y

                # Only continue the chain when center_ports is on.
                if not graph.center_ports:
                    break

                # Follow to the next singleton: successors for entry
                # ports (walking inward), predecessors for exit ports.
                nexts: set[str] = set()
                if is_entry:
                    for edge in graph.edges_from(current):
                        if edge.target in internal_ids:
                            nexts.add(edge.target)
                else:
                    for edge in graph.edges_to(current):
                        if edge.source in internal_ids:
                            nexts.add(edge.source)
                if len(nexts) != 1:
                    break
                current = next(iter(nexts))


def _snap_grid_group_entry_ports(graph: MetroGraph) -> None:
    """Snap entry ports of grid-group sections to their connected station Y.

    Stage 3.2 aligns entry ports to the upstream junction Y (e.g. the
    midpoint of two exit stations in the source section).  When Stage 4.2
    is skipped for grid-group sections, the internal station stays on the
    grid but the port keeps the junction-derived Y, creating a diagonal.

    This step corrects that by moving the port to the Y of its first
    connected non-port station inside the section, giving a straight
    horizontal connection.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Find the first non-port station connected to this port.
        target_y = None
        for edge in graph.edges_from(port_id):
            tgt = graph.stations.get(edge.target)
            if tgt and not tgt.is_port and tgt.section_id == section.id:
                target_y = tgt.y
                break

        if target_y is not None and abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _snap_grid_group_exit_ports(graph: MetroGraph) -> None:
    """Snap exit ports of grid-group sections to their connected station Y.

    Mirrors Stage 4.3 (entry port snap) for exit ports.  When a
    grid-group section's exit port is at a midpoint between internal
    stations (the default centering), move it to the Y of the connected
    internal station that feeds into it.  This eliminates midpoint
    detours (e.g. get_reference exit at y=340 midpoint instead of y=320
    where get_pcgr sits).

    When multiple internal stations feed the exit port, picks the one
    whose Y is closest to the downstream entry port (if resolvable),
    otherwise picks the nearest to the current port position.
    """
    grid_group_secs = _grid_group_section_ids(graph)

    if not grid_group_secs:
        return

    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue
        if port.section_id not in grid_group_secs:
            continue
        if port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue

        port_st = graph.stations.get(port_id)
        if not port_st:
            continue

        section = graph.sections.get(port.section_id)
        if not section or section.direction not in ("LR", "RL"):
            continue

        # Collect internal sources that feed this exit port and the
        # line ids carried by the exit edges in a single edge pass.
        source_ys: list[float] = []
        exit_lines: set[str] = set()
        for edge in graph.edges_to(port_id):
            exit_lines.add(edge.line_id)
            src = graph.stations.get(edge.source)
            if src and not src.is_port and src.section_id == section.id:
                source_ys.append(src.y)

        if not source_ys:
            continue

        ds_y = _resolve_downstream_entry_y(graph, port_id, junction_ids)

        unique_source_ys = sorted(set(source_ys))
        spread = unique_source_ys[-1] - unique_source_ys[0]
        n_unique = len(unique_source_ys)
        single_source = n_unique == 1 or spread <= 1.0

        # A flow-aligned exit anchors on its carriers' shared row, so the level
        # change becomes a riser in the inter-section gap rather than a diagonal
        # inside the section.  Anchors for a single carrying station or a
        # parallel bundle (several carriers on one row, one line each), feeding
        # a downstream entry directly or through a fan-out junction.  A bypass
        # bundle, a fan-in, or a merge junction keep the downstream-aligned
        # placement so the inter-section run stays straight (see
        # ``flow_exit_carrier_anchor``).
        keep_downstream_aligned = ds_y is not None and abs(port_st.y - ds_y) < 1.0
        anchors_to_carrier = (
            flow_exit_carrier_anchor(graph, port_id, section, junction_ids) is not None
        )
        if keep_downstream_aligned and not anchors_to_carrier:
            continue

        if single_source:
            target_y = source_ys[0]
        else:
            # Multi-Y sources: snap onto the source that aligns with the
            # downstream entry so the inter-section run stays horizontal.
            # For 3+ sources this overrides the default centered merge
            # only when the exit carries a multi-line bundle (parallel-
            # redundant rather than a true fan-in).
            if ds_y is None or (n_unique >= 3 and len(exit_lines) < 2):
                continue
            match = next((y for y in unique_source_ys if abs(y - ds_y) < 1.0), None)
            if match is None:
                continue
            target_y = match

        if abs(port_st.y - target_y) >= 1.0:
            port_st.y = target_y


def _resolve_downstream_entry_y(
    graph: MetroGraph,
    exit_port_id: str,
    junction_ids: set[str],
) -> float | None:
    """Resolve the downstream entry port Y reachable from an exit port.

    Handles two patterns:
    - Direct: exit_port -> entry_port
    - Via junction: exit_port -> junction -> entry_port(s)

    Returns the nearest downstream entry port Y, or None if not found.
    """
    port_st = graph.stations.get(exit_port_id)
    if not port_st:
        return None

    entry_ys: list[float] = []
    for edge in graph.edges_from(exit_port_id):
        # Direct exit -> entry connection
        dp = graph.ports.get(edge.target)
        if dp and dp.is_entry:
            ds_st = graph.stations.get(edge.target)
            if ds_st:
                entry_ys.append(ds_st.y)
            continue
        # Via junction
        if edge.target in junction_ids:
            for e2 in graph.edges_from(edge.target):
                dp2 = graph.ports.get(e2.target)
                if dp2 and dp2.is_entry:
                    ds_st = graph.stations.get(e2.target)
                    if ds_st:
                        entry_ys.append(ds_st.y)

    if entry_ys:
        return min(entry_ys, key=lambda y: abs(y - port_st.y))
    return None


def _align_exit_ports(graph: MetroGraph) -> None:
    """Align LEFT/RIGHT exit ports on fold sections with their target's Y.

    Applies to fold sections (multi-row span or vertical flow; fold bridges).
    These have exit ports placed near the section bottom, but the target
    section's entry may be at a different Y. Aligning ensures a straight
    horizontal inter-section connection.
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if port.is_entry:
            continue

        exit_section = graph.sections.get(port.section_id)
        if not exit_section:
            continue

        # A TOP/BOTTOM exit on a horizontal-flow section that drops into a TB
        # section's perpendicular entry is positioned past the last station so
        # the trunk curves out after the marker.  A fold's BOTTOM exit (which
        # feeds a sideways entry, not a drop) has no such target and keeps its
        # own placement.  A single-row section places its perpendicular exit
        # past the last station only when it also has a flow-aligned port to
        # anchor the horizontal run; a section with ONLY perpendicular ports is
        # an unsupported shape rejected downstream, so it keeps its placement.
        if (
            exit_section.direction in ("LR", "RL")
            and port.side in (PortSide.TOP, PortSide.BOTTOM)
            and (
                next(_drop_targets(graph, port_id), None) is not None
                or (
                    exit_section.grid_row_span == 1
                    and not _section_lacks_flow_aligned_port(graph, exit_section)
                )
            )
        ):
            _align_perpendicular_exit_port(graph, port_id, port, exit_section)
            continue

        if not _is_fold_section(exit_section):
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_exit_port(graph, port_id, port, exit_section, junction_ids)


def _align_perpendicular_exit_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    exit_section: Section,
) -> None:
    """Place a TOP/BOTTOM exit on an LR/RL section past its last station.

    The trunk runs horizontally; to leave through the top or bottom edge the
    line continues past the trailing station and curves perpendicular.  The
    port sits on the boundary edge, offset along the flow beyond the last
    station by more than the station-elbow tolerance so the curve falls after
    the marker rather than turning the line through it.
    """
    internal_xs = [
        graph.stations[sid].x
        for sid in exit_section.station_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if not internal_xs:
        return

    clearance = STATION_ELBOW_TOLERANCE + CURVE_RADIUS
    if exit_section.direction == "RL":
        # Flow runs right-to-left, so the trailing station is the leftmost.
        exit_x = min(internal_xs) - clearance
        exit_x = max(exit_x, exit_section.bbox_x + CURVE_RADIUS)
    else:
        exit_x = max(internal_xs) + clearance
        exit_x = min(exit_x, exit_section.bbox_x + exit_section.bbox_w - CURVE_RADIUS)

    if port.side == PortSide.TOP:
        exit_y = exit_section.bbox_y
    else:
        exit_y = exit_section.bbox_y + exit_section.bbox_h
    _set_port_x(graph, port_id, exit_x)
    _set_port_y(graph, port_id, exit_y)

    _align_drop_target_trunk(graph, port_id, exit_x)
    _align_horizontal_drop_target(graph, port_id, exit_x)


def _align_drop_target_trunk(graph: MetroGraph, port_id: str, exit_x: float) -> None:
    """Shift a perpendicular-drop target's trunk under the exit X.

    The line leaves the exit port vertically and drops straight into the
    target's TOP/BOTTOM entry, so the target's vertical trunk must align with
    the exit port's X -- the 90-degree rotation of a fold's TB exit aligning to
    its target's Y.  Only the trunk (internal stations and the perpendicular
    drop entry/exit ports) moves; the section box and its flow-side LEFT/RIGHT
    ports stay on the grid column edge, so a source and target stacked in one
    column keep their shared right edge rather than the target jutting out by
    the drop offset.
    """
    for tgt_section in _drop_targets(graph, port_id):
        trunk_x = _tb_trunk_x(graph, tgt_section)
        if trunk_x is None:
            continue
        # Only a same-column drop (the exit X lands within the target's own
        # box) shifts the trunk to meet it.  A cross-column exit far off the
        # box would drag the trunk -- and its stations -- out of the bbox; the
        # trunk stays on its grid column and routing bridges the cross-column
        # drop with an L-shaped inter-section lead-in instead.
        if not _drop_x_within_section(tgt_section, exit_x):
            graph._cross_column_perp_bridges.add(tgt_section.id)
            continue
        delta = exit_x - trunk_x
        if abs(delta) >= SAME_COORD_TOLERANCE:
            _shift_section_trunk(graph, tgt_section, delta, exit_x)


def _align_horizontal_drop_target(
    graph: MetroGraph, port_id: str, exit_x: float
) -> None:
    """Shift a horizontal-flow drop target's trunk so its perp entry aligns to X.

    A perpendicular exit dropping into a horizontal-flow (LR/RL) section's
    TOP/BOTTOM entry needs the descent to run straight: the entry port must sit
    under the exit X.  The whole trunk shifts by the same delta, so the entry port
    lands on the exit X while the first station keeps its station-elbow offset and
    the drop turns into it below.  Unlike a TB target (``_align_drop_target_trunk``)
    the first station cannot sit on the drop column -- a perpendicular port sharing
    a horizontal-flow station's X is a station-as-elbow violation -- so the
    alignment reference is the entry port, not the trunk's first station.
    """
    for tgt_section, entry_port in _horizontal_drop_targets(graph, port_id):
        if not _drop_x_within_section(tgt_section, exit_x):
            graph._cross_column_perp_bridges.add(tgt_section.id)
            continue
        delta = exit_x - entry_port.x
        if abs(delta) >= SAME_COORD_TOLERANCE:
            _shift_section_trunk(graph, tgt_section, delta, exit_x)


def _shift_section_trunk(
    graph: MetroGraph, section: Section, delta: float, exit_x: float
) -> None:
    """Shift a section's trunk by *delta*, growing the box to fit the new extent.

    The trunk -- internal stations and the perpendicular drop entry/exit ports --
    moves; the flow-side LEFT/RIGHT ports stay on the grid column edge so a source
    and target stacked in one column keep their shared right edge.  The box widens
    only if the shifted trunk would overrun ``exit_x`` plus a corner radius.
    """
    bbox_right = section.bbox_x + section.bbox_w
    for sid in section.station_ids:
        port = graph.ports.get(sid)
        if port is not None and port.side in (PortSide.LEFT, PortSide.RIGHT):
            continue
        st = graph.stations.get(sid)
        if st:
            st.x += delta
        if port:
            port.x += delta
    overrun = (exit_x + CURVE_RADIUS) - bbox_right
    if overrun > 0:
        section.bbox_w += overrun


def _drop_targets(graph: MetroGraph, port_id: str) -> Iterator[Section]:
    """Yield vertical-flow (TB/BT) sections reached from a perp exit drop."""
    return (
        sec
        for sec, _port in _perp_drop_targets(graph, port_id)
        if lanes_run_along_x(sec.direction)
    )


def _horizontal_drop_targets(
    graph: MetroGraph, port_id: str
) -> Iterator[tuple[Section, Port]]:
    """Yield (horizontal-flow LR/RL section, its perp entry port) for a drop."""
    return (
        (sec, port)
        for sec, port in _perp_drop_targets(graph, port_id)
        if lanes_run_along_y(sec.direction)
    )


def _perp_drop_targets(
    graph: MetroGraph, port_id: str
) -> Iterator[tuple[Section, Port]]:
    """Yield (section, its TOP/BOTTOM entry port) reached from a perp exit drop.

    Follows the exit port's edges, directly or through a fan-out junction, to any
    TOP/BOTTOM entry port and its owning section.
    """
    for edge in graph.edges_from(port_id):
        targets = [edge.target]
        if edge.target in graph.junction_ids:
            targets = [e.target for e in graph.edges_from(edge.target)]
        for tid in targets:
            tp = graph.ports.get(tid)
            if (
                not tp
                or not tp.is_entry
                or tp.side not in (PortSide.TOP, PortSide.BOTTOM)
            ):
                continue
            sec = graph.sections.get(tp.section_id)
            if sec:
                yield sec, tp


def _align_lr_exit_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    exit_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT exit port's Y with its target entry port."""
    tgt = _lr_exit_aligned_target(graph, port_id, exit_section, junction_ids)
    if tgt is None:
        return

    if lanes_run_along_x(exit_section.direction):
        tgt_y = _resolve_tb_exit_y(graph, port, tgt, exit_section)
    else:
        tgt_y = tgt.y

    _set_port_y(graph, port_id, tgt_y)


def _realign_fold_lr_exit_ports(graph: MetroGraph) -> None:
    """Snap each fold LEFT/RIGHT exit down onto a target settled below it.

    The bbox-contained alignment target keeps the snapped exit inside its
    section; see :func:`iter_fold_lr_exits_short_of_target` for which exits
    qualify.
    """
    for port_id, tgt in iter_fold_lr_exits_short_of_target(graph, SAME_COORD_TOLERANCE):
        _set_port_y(graph, port_id, tgt.y)


def _exit_row_icon_reach(
    graph: MetroGraph,
    exit_section: Section,
    trailing_y: float,
) -> float:
    """How far a terminus icon hangs past the trailing row, along the flow.

    Returns the largest forward distance from ``trailing_y`` to the drawn
    far edge of any internal terminus whose icons extend in the section's
    forward flow (down for TB, up for BT).  ``0.0`` when no such icon
    reaches the exit row, so the exit-port gap is left untouched.
    """
    from nf_metro.layout.phases.single_section import (
        _terminus_icon_flow_overhang,
        _terminus_icons_extend_forward,
    )

    flow = AxisFrame.flow_sign(exit_section.direction)
    reach = 0.0
    for sid in exit_section.station_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or not st.is_terminus:
            continue
        is_source = not graph.edges_to(st.id)
        if not _terminus_icons_extend_forward(is_source, exit_section.direction):
            continue
        overhang = _terminus_icon_flow_overhang(
            len(st.terminus_labels), st.terminus_names
        )
        reach = max(reach, flow * (st.y - trailing_y) + overhang)
    return reach


def _resolve_tb_exit_y(
    graph: MetroGraph,
    port: Port,
    tgt: Station,
    exit_section: Section,
) -> float:
    """Resolve the Y coordinate for a vertical-flow (TB/BT) section's exit port.

    Mirrors the entry-side gap: finds how far the perpendicular entry port sits
    before the leading station (against the flow), and places the exit port the
    same distance beyond the trailing station (along the flow).  Pushes the
    target section along the flow if needed so the inter-section line is
    straight.  The flow sense follows the section's :attr:`primary_sign`: a
    downward (TB) flow trails at the bottom, an upward (BT) one at the top.
    """
    flow = AxisFrame.flow_sign(exit_section.direction)
    internal_ys = [
        graph.stations[sid].y
        for sid in exit_section.station_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    if internal_ys:
        trailing_y = max(internal_ys) if flow > 0 else min(internal_ys)
        leading_y = min(internal_ys) if flow > 0 else max(internal_ys)
    else:
        trailing_y = leading_y = port.y

    # Mirror the entry-side gap (distance from the entry port to the leading
    # station, measured against the flow).
    entry_gap = MIN_PORT_STATION_GAP
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_gap = max(entry_gap, flow * (leading_y - graph.stations[pid].y))
            break

    # Ensure the gap beyond the trailing station is large enough for the exit
    # corner curve (CURVE_RADIUS) plus a straight run so the curve doesn't
    # crowd the station pill.
    min_exit_gap = max(entry_gap, CURVE_RADIUS + MIN_PORT_STATION_GAP)

    # A terminus file icon hangs forward along the flow (below for TB, above
    # for BT), past its marker by more than one row.  When such an icon sits
    # in the exit row, push the exit port (and the corridor that follows it
    # out) clear of the icon's drawn edge so a route leaving the section does
    # not graze the artefact.
    icon_reach = _exit_row_icon_reach(graph, exit_section, trailing_y)
    if icon_reach > 0:
        min_exit_gap = max(min_exit_gap, icon_reach + EXIT_CORRIDOR_ICON_CLEARANCE)

    bound_exit_y = trailing_y + flow * min_exit_gap
    if flow * (tgt.y - bound_exit_y) >= 0:
        tgt_y = tgt.y
    else:
        # Push target section along the flow to align with the exit port.
        tgt_y = bound_exit_y
        delta = tgt_y - tgt.y

        tgt.y = tgt_y
        tgt_port = graph.ports.get(tgt.id)
        if tgt_port:
            tgt_port.y = tgt_y
            tgt_sec = graph.sections.get(tgt_port.section_id)
            if tgt_sec:
                for sid in tgt_sec.station_ids:
                    s = graph.stations.get(sid)
                    if s and s.id != tgt.id:
                        s.y += delta
                        p = graph.ports.get(sid)
                        if p:
                            p.y += delta
                tgt_sec.bbox_y += delta

    # Extend the exit section bbox so the padding beyond the exit port mirrors
    # the padding before the entry port (against the flow).
    entry_port_y = None
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_port_y = graph.stations[pid].y
            break
    if entry_port_y is not None:
        box_top = exit_section.bbox_y
        box_bot = exit_section.bbox_y + exit_section.bbox_h
        leading_edge = box_top if flow > 0 else box_bot
        pad = flow * (entry_port_y - leading_edge)
        desired_trailing_edge = tgt_y + flow * pad
        if flow > 0:
            if desired_trailing_edge > box_bot:
                exit_section.bbox_h = desired_trailing_edge - box_top
        elif desired_trailing_edge < box_top:
            exit_section.bbox_h = box_bot - desired_trailing_edge
            exit_section.bbox_y = desired_trailing_edge

    return tgt_y


def _align_tb_section_bbox_bottoms(graph: MetroGraph, section_y_padding: float) -> None:
    """Extend each TB-section's bbox bottom to match its downstream
    target section's settled bbox bottom.

    A TB (fold) section's exit port sits at the Y of the downstream
    LR/RL section's entry port (placed by ``_resolve_tb_exit_y``).
    When the TB section's bbox bottom equals its exit-port Y, the
    inter-section line runs flush against the section edge.

    For every TB section with an LR/RL exit, find the target sections
    its exit ports feed into (directly or via a junction) and grow the
    TB section's ``bbox_h`` so its bottom reaches the maximum of those
    targets' bottoms.  Bbox tops are preserved; only ``bbox_h`` grows.

    The target bottom is its *settled* content bottom
    (:func:`_predict_section_content_bottom`), not its current live
    ``bbox_h``.  This stage runs upstream of the bbox-shrink phase: a
    target's live ``bbox_h`` can include a padding band that the shrink
    later collapses to content, whereas the settled content bottom is
    where the target edge comes to rest.  Aligning to it keeps the
    inter-section run an equal distance above both section bottoms.

    Skipped for TB sections with BOTTOM-side exit ports (TB->TB flow)
    so the bottom-edge port placement invariant continues to hold.

    Also skipped when the section's LR/RL exit port already sits well
    above the bbox bottom (more than ``MIN_PORT_STATION_GAP``).  That
    happens when the exit leaves near the top of the section toward a
    target in another column/row -- the port is already contained, so
    stretching the bbox down would only add dead space (and can overlap
    a section in the row below).  The stretch is only needed for the
    downward-fold case, where ``_resolve_tb_exit_y`` places the exit
    port flush at the bbox bottom.
    """
    from nf_metro.layout.routing import compute_station_offsets

    junction_ids = graph.junction_ids
    offsets: dict[tuple[str, str], float] | None = None

    def _downstream_section_ids(tb_section: Section) -> set[str]:
        out: set[str] = set()
        for pid in tb_section.exit_ports:
            for edge in graph.edges_from(pid):
                candidates: list[str] = []
                if edge.target in junction_ids:
                    for e2 in graph.edges_from(edge.target):
                        candidates.append(e2.target)
                else:
                    candidates.append(edge.target)
                for tid in candidates:
                    tport = graph.ports.get(tid)
                    if tport is None:
                        continue
                    tsec = graph.sections.get(tport.section_id)
                    if tsec is None or tsec.id == tb_section.id:
                        continue
                    out.add(tsec.id)
        return out

    for section in list(graph.sections.values()):
        if section.direction != "TB" or section.bbox_h <= 0:
            continue
        exit_sides = {
            graph.ports[pid].side for pid in section.exit_ports if pid in graph.ports
        }
        if not exit_sides & {PortSide.LEFT, PortSide.RIGHT}:
            continue
        if PortSide.BOTTOM in exit_sides:
            continue
        current_bot = section.bbox_y + section.bbox_h
        lr_exit_port_ys = [
            graph.stations[pid].y
            for pid in section.exit_ports
            if pid in graph.ports
            and graph.ports[pid].side in (PortSide.LEFT, PortSide.RIGHT)
            and pid in graph.stations
        ]
        # Only stretch the downward-fold case, where the exit port sits
        # flush at (or just above) the bbox bottom.  When every LR/RL
        # exit port is already clear of the bottom, the port is contained
        # and the stretch would add dead space.
        if (
            lr_exit_port_ys
            and current_bot - max(lr_exit_port_ys) > MIN_PORT_STATION_GAP
        ):
            continue
        target_ids = _downstream_section_ids(section)
        if not target_ids:
            continue
        if offsets is None:
            offsets = compute_station_offsets(graph)
        target_bots = [
            bot
            for tid in target_ids
            if tid in graph.sections
            and graph.sections[tid].bbox_h > 0
            and (
                bot := _predict_section_content_bottom(
                    graph, graph.sections[tid], section_y_padding, offsets
                )
            )
            is not None
        ]
        if not target_bots:
            continue
        desired_bot = max(target_bots)
        if desired_bot - current_bot <= SAME_COORD_TOLERANCE:
            continue
        section.bbox_h = desired_bot - section.bbox_y


def _internal_station_ys(graph: MetroGraph, section: Section) -> list[float]:
    """Y of every internal (non-port) station in a section.

    Excludes the section's declared entry/exit ports and any port station,
    leaving the real, layout-bearing stations an entry port must clear.
    """
    ports = set(section.entry_ports) | set(section.exit_ports)
    return [
        st.y
        for sid in section.station_ids
        if sid not in ports
        and (st := graph.stations.get(sid)) is not None
        and not st.is_port
    ]


def _clamp_tb_entry_port(
    graph: MetroGraph,
    entry_section: Section,
    target_y: float,
    edge: Edge,
    src: Station,
    junction_ids: set[str],
) -> float:
    """Clamp a TB section's perpendicular entry port above internal stations.

    The entry port must stay above the first internal station so the
    direction-change curve has room. When clamped, also pulls the source
    station/junction up to maintain a straight horizontal run.

    Returns the (possibly clamped) target_y.
    """
    internal_ys = _internal_station_ys(graph, entry_section)
    if not internal_ys:
        return target_y

    first_y = min(internal_ys)
    max_y = first_y - MIN_PORT_STATION_GAP
    if target_y <= max_y:
        return target_y

    # Prefer the topmost source-side station feeding the exit port
    # so that line exits horizontally.
    exit_pid = edge.source
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    exit_pid = e2.source
                    break

    top_src_y = None
    for e3 in graph.edges:
        if e3.target == exit_pid:
            s3 = graph.stations.get(e3.source)
            if s3 and not s3.is_port and e3.source not in junction_ids:
                if top_src_y is None or s3.y < top_src_y:
                    top_src_y = s3.y

    if top_src_y is not None and top_src_y < max_y:
        target_y = top_src_y
    else:
        target_y = max_y

    # Pull source up to maintain straight horizontal run
    src.y = target_y
    if src.is_port and edge.source in graph.ports:
        graph.ports[edge.source].y = target_y
    # If source is a junction, also pull the exit port feeding it
    if edge.source in junction_ids:
        for e2 in graph.edges:
            if e2.target == edge.source:
                ep = graph.stations.get(e2.source)
                if ep and ep.is_port:
                    ep.y = target_y
                    if e2.source in graph.ports:
                        graph.ports[e2.source].y = target_y

    return target_y


def _lift_perp_entry_port_above_stations(
    graph: MetroGraph, entry_section: Section, port: Port, port_id: str
) -> None:
    """Raise a vertical-flow section's perpendicular entry port above its row.

    When the feeder lives in a different grid row, Y alignment to the feeder
    is skipped, leaving the port on the first internal station's row.  A side
    fan-out from that row runs along it and through a non-consumed sibling's
    marker.  Seating the port a station gap above the topmost internal station
    routes the fan in a channel above the row, dropping into each station from
    outside the row (#1001).  Only the port moves; the cross-row feeder is left
    where it is.

    Applies only to LEFT/RIGHT (perpendicular) entry on a section whose flow
    runs down the Y axis; a horizontal-flow section separates its lines on Y
    and reaches each from its own lane, so it has no shared row to cross.

    The gap matches the room ``_adjust_tb_entry_shifts`` already reserved by
    nudging every internal station down one entry shift, so a cross-row entry
    settles exactly where a same-row one does after ``_clamp_tb_entry_port``.
    """
    if lanes_run_along_y(entry_section.direction):
        return
    if port.side not in (PortSide.LEFT, PortSide.RIGHT):
        return
    port_st = graph.stations.get(port_id)
    if port_st is None:
        return
    internal_ys = _internal_station_ys(graph, entry_section)
    if not internal_ys:
        return
    base_y = graph._base_y_spacing
    gap = base_y * ENTRY_SHIFT_TB if base_y else CURVE_RADIUS + MIN_STATION_FLAT_LENGTH
    target_y = min(internal_ys) - gap
    if port_st.y <= target_y:
        return
    _set_port_y(graph, port_id, target_y)
    if target_y < entry_section.bbox_y:
        _expand_bbox_for_y(entry_section, target_y)


def _space_ports_from_termini(
    graph: MetroGraph,
    y_spacing: float,
) -> None:
    """Push ports away from terminus stations so there is a full row gap.

    After port alignment, an entry or exit port may sit very close to a
    terminus station in the same section.  Lines routed from that port
    then overlap the terminus file icon.

    Only entry ports are checked against entry-side (source) termini, and
    exit ports against exit-side (sink) termini, to avoid displacing
    ports on the opposite side of the section.

    Exit ports on fold sections (grid_row_span > 1 or TB direction) are
    skipped because ``_align_exit_ports`` will overwrite them.
    """
    # Pre-compute edge adjacency (used to identify direct connections
    # and to propagate port moves across section boundaries).
    adjacency: dict[str, set[str]] = {}
    successors: dict[str, set[str]] = {}
    predecessors: dict[str, set[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)
        successors.setdefault(edge.source, set()).add(edge.target)
        predecessors.setdefault(edge.target, set()).add(edge.source)

    for section in graph.sections.values():
        entry_port_ids = set(section.entry_ports)
        exit_port_ids = set(section.exit_ports)
        all_port_ids = entry_port_ids | exit_port_ids
        real_sids = {s for s in section.station_ids if s not in all_port_ids}

        # Skip exit ports on fold sections -- _align_exit_ports handles them.
        is_fold = _is_fold_section(section)

        # Classify termini by side.  A station with no in-section
        # predecessors is an entry-side (source) terminus; one with no
        # in-section successors is an exit-side (sink) terminus.  A
        # station can be both (isolated within the section), but we only
        # add it to entry_termini to avoid conflicting pushes from both
        # the entry and exit port passes.
        entry_termini: list[tuple[str, float]] = []
        exit_termini: list[tuple[str, float]] = []
        for sid in real_sids:
            st = graph.stations.get(sid)
            if not st or not st.is_terminus or st.is_port:
                continue
            # Off-track stations get lifted above the topmost line track
            # later (Stage 5.2), so they no longer share a Y with the
            # inter-section bundle.  Excluding them here prevents ports
            # from being pushed away (and dragging the upstream port via
            # junction propagation) for a conflict that won't exist by
            # render time.
            if st.off_track:
                continue
            preds = predecessors.get(sid, set())
            succs = successors.get(sid, set())
            is_source = not (preds & real_sids)
            is_sink = not (succs & real_sids)
            if is_source:
                entry_termini.append((sid, st.y))
            elif is_sink:
                # Only classify as exit terminus if not already an
                # entry terminus (avoids double-counting isolated nodes).
                exit_termini.append((sid, st.y))

        _push_ports_from_termini(
            graph,
            sorted(entry_port_ids),
            entry_termini,
            section,
            adjacency,
            predecessors,
            y_spacing,
        )
        if not is_fold:
            _push_ports_from_termini(
                graph,
                sorted(exit_port_ids),
                exit_termini,
                section,
                adjacency,
                predecessors,
                y_spacing,
            )


def _push_ports_from_termini(
    graph: MetroGraph,
    port_ids: list[str],
    termini: list[tuple[str, float]],
    section: Section,
    adjacency: dict[str, set[str]],
    predecessors: dict[str, set[str]],
    y_spacing: float,
) -> None:
    """Ensure *y_spacing* between each port and non-connected termini.

    The strategy depends on how the port connects across sections:

    - **Junction link** (fan-out): move the port and propagate through
      the junction to its *upstream* (predecessor) port only, keeping
      the exit-junction-entry chain straight without disturbing other
      fan-out targets.
    - **Direct port-to-port link** (no junction): moving the port would
      cascade to the other section and mis-align its internal stations.
      Instead, push the conflicting *terminus* away from the port.
    - **No cross-section link**: move the port freely.

    *port_ids* must be a sorted list so that results are deterministic
    when multiple ports in the same section conflict with the same
    terminus.
    """
    junction_ids = graph.junction_ids
    section_port_set = set(port_ids)

    for pid in port_ids:
        port_st = graph.stations.get(pid)
        if not port_st:
            continue
        port_obj = graph.ports.get(pid)
        assert port_obj is not None, f"port {pid} missing from graph.ports"
        neighbours = adjacency.get(pid, set())

        # Classify cross-section connection type.
        has_junction = bool(neighbours & junction_ids)
        has_direct_port = False
        if not has_junction:
            for nb in neighbours:
                if nb in graph.ports and nb not in section_port_set:
                    has_direct_port = True
                    break

        # Collect all termini that are too close and not directly
        # connected to this port.
        conflict_ids: list[str] = []
        conflict_ys: list[float] = []
        for tid, ty in termini:
            if tid in neighbours:
                continue
            if abs(port_st.y - ty) < y_spacing:
                conflict_ids.append(tid)
                conflict_ys.append(ty)

        if not conflict_ys:
            continue

        if has_direct_port:
            # Move the terminus instead of the port so the
            # inter-section line stays straight.
            _push_termini_from_port(graph, conflict_ids, port_st.y, section, y_spacing)
            continue

        # Compute the single best Y that satisfies all conflicts.
        above_candidates = [ty - y_spacing for ty in conflict_ys]
        below_candidates = [ty + y_spacing for ty in conflict_ys]

        best_above = min(above_candidates)
        best_below = max(below_candidates)

        dist_above = abs(port_st.y - best_above)
        dist_below = abs(port_st.y - best_below)
        # Ties go above (smaller Y) to keep ports near the top.
        new_y = best_above if dist_above <= dist_below else best_below

        port_st.y = new_y
        port_obj.y = new_y

        # Propagate through junctions so inter-section lines stay straight.
        _propagate_through_junctions(
            graph,
            pid,
            new_y,
            neighbours,
            junction_ids,
            predecessors,
        )

        # Grow this section's bbox to contain the moved port.
        _expand_bbox_for_y(section, new_y)


def _propagate_through_junctions(
    graph: MetroGraph,
    origin_pid: str,
    new_y: float,
    neighbours: set[str],
    junction_ids: set[str],
    predecessors: dict[str, set[str]],
) -> None:
    """Move connected junctions and their upstream exit ports to *new_y*.

    Only propagates to the junction's upstream (predecessor) ports, not
    to other fan-out targets (entry ports to other sections).
    """
    for nb in neighbours:
        if nb not in junction_ids:
            continue
        nb_st = graph.stations.get(nb)
        if not nb_st:
            continue

        nb_st.y = new_y
        for jnb in predecessors.get(nb, set()):
            if jnb == origin_pid:
                continue
            jnb_st = graph.stations.get(jnb)
            if not jnb_st or not jnb_st.is_port:
                continue
            jnb_st.y = new_y
            jnb_obj = graph.ports.get(jnb)
            if jnb_obj:
                jnb_obj.y = new_y
                jnb_sec = graph.sections.get(jnb_obj.section_id)
                if jnb_sec:
                    _expand_bbox_for_y(jnb_sec, new_y)


def _push_termini_from_port(
    graph: MetroGraph,
    terminus_ids: list[str],
    port_y: float,
    section: Section,
    y_spacing: float,
) -> None:
    """Push terminus stations to the nearest station row that clears the port.

    Instead of placing the terminus at the arbitrary ``port_y ± y_spacing``,
    snap it to an existing station Y in the section that satisfies the
    minimum clearance.  This keeps the terminus aligned with an actual
    track row rather than floating at an unrelated Y coordinate.
    """
    # Collect existing station Y values in the section (excluding ports
    # and the termini being moved) as candidate snap targets.
    port_ids = section.port_ids
    tid_set = set(terminus_ids)
    section_ys: set[float] = set()
    for sid in section.station_ids:
        if sid in port_ids or sid in tid_set:
            continue
        st = graph.stations.get(sid)
        if st and not st.is_port:
            section_ys.add(st.y)

    for tid in terminus_ids:
        t_st = graph.stations.get(tid)
        if not t_st:
            continue

        # Determine push direction
        going_down = t_st.y > port_y

        # Find the nearest section row Y that satisfies clearance
        candidates = sorted(
            (y for y in section_ys if abs(y - port_y) >= y_spacing),
            key=lambda y: abs(y - t_st.y),
        )
        if going_down:
            candidates = [y for y in candidates if y >= port_y + y_spacing]
        else:
            candidates = [y for y in candidates if y <= port_y - y_spacing]

        if candidates:
            new_y = candidates[0]
        else:
            # No existing row satisfies clearance; fall back to offset.
            new_y = (port_y + y_spacing) if going_down else (port_y - y_spacing)

        t_st.y = new_y
        _expand_bbox_for_y(section, new_y)
