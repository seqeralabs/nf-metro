"""Entry/exit port positioning and alignment on section boundaries."""

from __future__ import annotations

from collections import Counter

from nf_metro.layout.constants import (
    CURVE_RADIUS,
    MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC,
    MIN_PORT_STATION_GAP,
    STATION_ELBOW_TOLERANCE,
)
from nf_metro.layout.phases._common import _expand_bbox_for_y, _grid_group_section_ids
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


def _align_entry_ports(graph: MetroGraph, tb_only: bool = False) -> None:
    """Align entry ports with their incoming connection's coordinates.

    LEFT/RIGHT ports: align Y for straight horizontal runs.
    TOP/BOTTOM ports: align X for vertical drops or Y for cross-column.

    With ``tb_only`` set, only ports on TB/BT sections are re-aligned --
    used by the late re-alignment pass (Stage 6.16), which targets the
    perpendicular-entry drift that vertical settling introduces in
    TB sections without disturbing settled LR/RL geometry.
    """
    junction_ids = graph.junction_ids

    for port_id, port in graph.ports.items():
        if not port.is_entry:
            continue

        entry_section = graph.sections.get(port.section_id)
        if not entry_section:
            continue

        if tb_only and entry_section.direction not in ("TB", "BT"):
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_entry_port(graph, port_id, port, entry_section, junction_ids)
        elif port.side in (PortSide.TOP, PortSide.BOTTOM):
            _align_tb_entry_port(graph, port_id, port, entry_section, junction_ids)


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
        # Cross-column: set Y to the closest source level
        src_ys = [y for _, y, _ in sources]
        if port.side == PortSide.TOP:
            target_y = min(src_ys)
        else:
            target_y = max(src_ys)
        # Clamp within bbox
        target_y = max(target_y, entry_section.bbox_y)
        target_y = min(target_y, entry_section.bbox_y + entry_section.bbox_h)
        _set_port_y(graph, port_id, target_y)
        # Only nudge X for LR/RL sections where TOP/BOTTOM ports are perpendicular
        if entry_section.direction in ("LR", "RL"):
            _nudge_port_from_stations(port_id, entry_section, graph)
    else:
        # Same-column: align X with source for vertical drop
        src_x, _, _ = sources[0]
        _set_port_x(graph, port_id, src_x)


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

        # Skip fold/TB sections (handled by _align_exit_ports)
        if exit_section.grid_row_span > 1 or exit_section.direction == "TB":
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

        # Skip when entry port is perpendicular to its section's flow.
        # A LEFT port on a TB section must bend, so aligning it with an
        # internal station's Y would route the line through that station.
        _perp = False
        if entry_section.direction == "TB" and entry_port_obj.side in (
            PortSide.LEFT,
            PortSide.RIGHT,
        ):
            _perp = True
        elif entry_section.direction in ("LR", "RL") and entry_port_obj.side in (
            PortSide.TOP,
            PortSide.BOTTOM,
        ):
            _perp = True
        if _perp:
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

        # Already aligned with downstream entry: straight run is correct
        # even if internal sources sit at a different Y.
        if ds_y is not None and abs(port_st.y - ds_y) < 1.0:
            continue

        unique_source_ys = sorted(set(source_ys))
        spread = unique_source_ys[-1] - unique_source_ys[0]
        n_unique = len(unique_source_ys)

        if n_unique == 1 or spread <= 1.0:
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

    Applies to sections with grid_row_span > 1 OR TB direction (fold bridges).
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
        if exit_section.grid_row_span <= 1 and exit_section.direction != "TB":
            continue

        if port.side in (PortSide.LEFT, PortSide.RIGHT):
            _align_lr_exit_port(graph, port_id, port, exit_section, junction_ids)


def _align_lr_exit_port(
    graph: MetroGraph,
    port_id: str,
    port: Port,
    exit_section: Section,
    junction_ids: set[str],
) -> None:
    """Align a LEFT/RIGHT exit port's Y with its target entry port."""
    for edge in graph.edges_from(port_id):
        tgt = graph.stations.get(edge.target)
        if not tgt:
            continue

        # Don't align with fan-out junctions
        if edge.target in junction_ids:
            break

        if not tgt.is_port:
            continue

        # Don't align with perpendicular target ports (cross-axis)
        tgt_port_obj = graph.ports.get(tgt.id)
        if tgt_port_obj and tgt_port_obj.side in (PortSide.TOP, PortSide.BOTTOM):
            break

        # Don't pull exit port outside its section bbox
        bbox_top = exit_section.bbox_y
        bbox_bot = exit_section.bbox_y + exit_section.bbox_h
        if not (bbox_top <= tgt.y <= bbox_bot):
            break

        if exit_section.direction == "TB":
            tgt_y = _resolve_tb_exit_y(graph, port, tgt, exit_section)
        else:
            tgt_y = tgt.y

        _set_port_y(graph, port_id, tgt_y)
        break


def _resolve_tb_exit_y(
    graph: MetroGraph,
    port: Port,
    tgt: Station,
    exit_section: Section,
) -> float:
    """Resolve the Y coordinate for a TB section's exit port.

    Mirrors the entry-side gap: finds how far the perpendicular entry
    port sits above the first internal station, and places the exit port
    the same distance below the last internal station. Pushes the target
    section down if needed so the inter-section line is straight.
    """
    internal_ys = [
        graph.stations[sid].y
        for sid in exit_section.station_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
    last_y = max(internal_ys) if internal_ys else port.y
    first_y = min(internal_ys) if internal_ys else port.y

    # Mirror the entry-side gap (distance from entry port to first station)
    entry_gap = MIN_PORT_STATION_GAP
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_gap = max(entry_gap, first_y - graph.stations[pid].y)
            break

    # Ensure the gap below the last station is large enough for the
    # exit corner curve (CURVE_RADIUS) plus a straight run so the
    # curve doesn't crowd the station pill.
    min_exit_gap = max(entry_gap, CURVE_RADIUS + MIN_PORT_STATION_GAP)
    min_exit_y = last_y + min_exit_gap
    if tgt.y >= min_exit_y:
        tgt_y = tgt.y
    else:
        # Push target section down to align with exit port
        tgt_y = min_exit_y
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

    # Extend exit section bbox so padding below the exit port
    # mirrors the padding above the entry port.
    entry_port_y = None
    for pid in exit_section.entry_ports:
        ep = graph.ports.get(pid)
        if ep and ep.side in (PortSide.LEFT, PortSide.RIGHT):
            entry_port_y = graph.stations[pid].y
            break
    if entry_port_y is not None:
        top_pad = entry_port_y - exit_section.bbox_y
        desired_bot = tgt_y + top_pad
        current_bot = exit_section.bbox_y + exit_section.bbox_h
        if desired_bot > current_bot:
            exit_section.bbox_h = desired_bot - exit_section.bbox_y

    return tgt_y


def _align_tb_section_bbox_bottoms(graph: MetroGraph) -> None:
    """Extend each TB-section's bbox bottom to match its downstream
    target section's bbox bottom.

    A TB (fold) section's exit port sits at the Y of the downstream
    LR/RL section's entry port (placed by ``_resolve_tb_exit_y``).
    When the TB section's bbox bottom equals its exit-port Y, the
    inter-section line runs flush against the section edge.

    For every TB section with an LR/RL exit, find the target sections
    its exit ports feed into (directly or via a junction) and grow the
    TB section's ``bbox_h`` so its bottom reaches the maximum of those
    targets' bbox bottoms.  Bbox tops are preserved; only ``bbox_h``
    grows.

    Skipped for TB sections with BOTTOM-side exit ports (TB->TB flow)
    so the bottom-edge port placement invariant continues to hold.
    """
    junction_ids = graph.junction_ids

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
        target_ids = _downstream_section_ids(section)
        if not target_ids:
            continue
        target_bots = [
            graph.sections[tid].bbox_y + graph.sections[tid].bbox_h
            for tid in target_ids
            if tid in graph.sections and graph.sections[tid].bbox_h > 0
        ]
        if not target_bots:
            continue
        desired_bot = max(target_bots)
        current_bot = section.bbox_y + section.bbox_h
        if desired_bot - current_bot <= 0.5:
            continue
        section.bbox_h = desired_bot - section.bbox_y


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
    internal_ids = (
        set(entry_section.station_ids)
        - set(entry_section.entry_ports)
        - set(entry_section.exit_ports)
    )
    internal_ys = [
        graph.stations[sid].y
        for sid in internal_ids
        if sid in graph.stations and not graph.stations[sid].is_port
    ]
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
        is_fold = section.grid_row_span > 1 or section.direction == "TB"

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
