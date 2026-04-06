"""Station offset computation for per-line Y positioning within bundles."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from nf_metro.layout.constants import OFFSET_STEP
from nf_metro.layout.routing.reversal import detect_reversed_sections
from nf_metro.parser.model import MetroGraph, PortSide


@dataclass
class _OffsetCtx:
    """Shared state threaded through offset computation phases."""

    graph: MetroGraph
    offsets: dict[tuple[str, str], float] = field(default_factory=dict)
    line_priority: dict[str, int] = field(default_factory=dict)
    max_priority: int = 0
    offset_step: float = OFFSET_STEP
    compact: bool = False
    reversed_sections: set[str] = field(default_factory=set)
    tb_sections: set[str] = field(default_factory=set)
    lr_rl_sections: set[str] = field(default_factory=set)
    # Pre-computed per-station inbound/outbound line sets (compact only)
    inbound: dict[str, set[str]] = field(default_factory=dict)
    outbound: dict[str, set[str]] = field(default_factory=dict)


def _build_offset_ctx(graph: MetroGraph, offset_step: float) -> _OffsetCtx:
    """Build shared context for offset computation phases."""
    line_order = list(graph.lines.keys())
    line_priority = {lid: i for i, lid in enumerate(line_order)}
    max_priority = len(line_order) - 1 if line_order else 0
    compact = getattr(graph, "compact_offsets", False)

    inbound: dict[str, set[str]] = {}
    outbound: dict[str, set[str]] = {}
    if compact:
        inbound = {sid: set() for sid in graph.stations}
        outbound = {sid: set() for sid in graph.stations}
        for edge in graph.edges:
            if edge.target in inbound:
                inbound[edge.target].add(edge.line_id)
            if edge.source in outbound:
                outbound[edge.source].add(edge.line_id)

    reversed_sections = detect_reversed_sections(graph)
    tb_sections = {sid for sid, s in graph.sections.items() if s.direction == "TB"}
    lr_rl_sections = {
        sid for sid, s in graph.sections.items() if s.direction in ("LR", "RL")
    }

    return _OffsetCtx(
        graph=graph,
        line_priority=line_priority,
        max_priority=max_priority,
        offset_step=offset_step,
        compact=compact,
        reversed_sections=reversed_sections,
        tb_sections=tb_sections,
        lr_rl_sections=lr_rl_sections,
        inbound=inbound,
        outbound=outbound,
    )


def _compute_base_offsets(ctx: _OffsetCtx) -> None:
    """Assign initial per-station offsets from global line priority.

    In compact mode, only allocates slots for the max lines on either
    side of each station.  In non-compact mode, uses global priority
    directly.
    """
    graph = ctx.graph
    for sid in graph.stations:
        lines = graph.station_lines(sid)
        if not lines:
            continue
        station = graph.stations[sid]
        reverse = station.section_id in ctx.reversed_sections

        if ctx.compact:
            max_side = max(len(ctx.inbound[sid]), len(ctx.outbound[sid]), 1)
            if max_side <= 1:
                for lid in lines:
                    ctx.offsets[(sid, lid)] = 0.0
            else:
                if len(ctx.inbound[sid]) >= len(ctx.outbound[sid]):
                    ref = ctx.inbound[sid]
                else:
                    ref = ctx.outbound[sid]
                ref_sorted = sorted(ref, key=lambda lid: ctx.line_priority.get(lid, 0))
                ref_idx = {lid: i for i, lid in enumerate(ref_sorted)}
                local_max = max_side - 1
                for lid in lines:
                    idx = ref_idx.get(lid, None)
                    if idx is None:
                        ctx.offsets[(sid, lid)] = 0.0
                    elif reverse:
                        ctx.offsets[(sid, lid)] = (local_max - idx) * ctx.offset_step
                    else:
                        ctx.offsets[(sid, lid)] = idx * ctx.offset_step
        else:
            for lid in lines:
                p = ctx.line_priority.get(lid, 0)
                if reverse:
                    ctx.offsets[(sid, lid)] = (ctx.max_priority - p) * ctx.offset_step
                else:
                    ctx.offsets[(sid, lid)] = p * ctx.offset_step


def _reindex_section_local(ctx: _OffsetCtx) -> None:
    """Re-index offsets per-section to close priority gaps (non-compact only).

    Lines absent from a section should not reserve offset slots within it.
    Also applies reconvergence ordering: when multiple upstream sections
    feed into one section, lines from the primary feeder keep their
    relative offsets at the top.
    """
    if ctx.compact:
        return

    graph = ctx.graph

    # --- Section-local priority re-indexing ---
    section_local: dict[str, dict[str, int]] = {}
    for sec_id in graph.sections:
        present: set[str] = set()
        for sid_s, station in graph.stations.items():
            if station.section_id == sec_id:
                present |= set(graph.station_lines(sid_s))
        ordered = sorted(present, key=lambda lid: ctx.line_priority.get(lid, 0))
        global_pris = [ctx.line_priority.get(lid, 0) for lid in ordered]
        has_gap = any(
            global_pris[i + 1] - global_pris[i] > 1 for i in range(len(global_pris) - 1)
        )
        if has_gap:
            section_local[sec_id] = {lid: i for i, lid in enumerate(ordered)}

    for sid_s, station in graph.stations.items():
        sec_id = station.section_id
        if sec_id not in section_local:
            continue
        local_pri = section_local[sec_id]
        local_max = max(local_pri.values()) if local_pri else 0
        reverse = sec_id in ctx.reversed_sections
        for lid in graph.station_lines(sid_s):
            p = local_pri.get(lid, 0)
            if reverse:
                ctx.offsets[(sid_s, lid)] = (local_max - p) * ctx.offset_step
            else:
                ctx.offsets[(sid_s, lid)] = p * ctx.offset_step

    # --- Reconvergence ordering ---
    for sec_id, section in graph.sections.items():
        if not section.entry_ports:
            continue
        line_feeder: dict[str, str] = {}
        for pid in section.entry_ports:
            for edge in graph.edges:
                if edge.target != pid:
                    continue
                src = graph.stations.get(edge.source)
                if not src:
                    continue
                feeder_sec = None
                if src.is_port:
                    feeder_sec = src.section_id
                elif edge.source in graph.junctions:
                    for je in graph.edges:
                        if je.target == edge.source and je.line_id == edge.line_id:
                            js = graph.stations.get(je.source)
                            if js and js.is_port:
                                feeder_sec = js.section_id
                                break
                if feeder_sec is not None:
                    line_feeder[edge.line_id] = feeder_sec
        if not line_feeder:
            continue

        lines_by_feeder: dict[str, list[str]] = {}
        for lid, fid in line_feeder.items():
            lines_by_feeder.setdefault(fid, []).append(lid)
        if len(lines_by_feeder) < 2:
            continue

        primary_fid = max(lines_by_feeder, key=lambda f: len(lines_by_feeder[f]))
        primary_lines = set(lines_by_feeder[primary_fid])
        if len(primary_lines) < 2:
            continue

        primary_order = section_local.get(primary_fid, ctx.line_priority)
        continuing = sorted(primary_lines, key=lambda lid: primary_order.get(lid, 0))

        sec_present: set[str] = set()
        for sid_s, station in graph.stations.items():
            if station.section_id == sec_id:
                sec_present |= set(graph.station_lines(sid_s))

        returning = sorted(
            sec_present - primary_lines,
            key=lambda lid: ctx.line_priority.get(lid, 0),
        )
        new_order = continuing + returning

        global_ordered = sorted(
            sec_present, key=lambda lid: ctx.line_priority.get(lid, 0)
        )
        if new_order == global_ordered:
            continue

        new_local = {lid: i for i, lid in enumerate(new_order)}
        local_max = len(new_order) - 1
        reverse = sec_id in ctx.reversed_sections
        for sid_s, station in graph.stations.items():
            if station.section_id != sec_id:
                continue
            for lid in graph.station_lines(sid_s):
                p = new_local.get(lid, 0)
                if reverse:
                    ctx.offsets[(sid_s, lid)] = (local_max - p) * ctx.offset_step
                else:
                    ctx.offsets[(sid_s, lid)] = p * ctx.offset_step


def _apply_compact_section_consistency(ctx: _OffsetCtx) -> None:
    """Ensure multi-line entry ports have consistent offsets (compact only).

    All lines entering a section should maintain consistent relative
    offsets at every multi-line station, including hidden pass-throughs.
    """
    if not ctx.compact:
        return

    graph = ctx.graph
    for sec_id, section in graph.sections.items():
        sec_entry_lines: list[str] = []
        for pid in section.entry_ports:
            sec_entry_lines.extend(graph.station_lines(pid))
        seen: set[str] = set()
        unique_entry: list[str] = []
        for lid in sorted(
            set(sec_entry_lines), key=lambda x: ctx.line_priority.get(x, 0)
        ):
            if lid not in seen:
                seen.add(lid)
                unique_entry.append(lid)
        if len(unique_entry) < 2:
            continue
        sec_reverse = sec_id in ctx.reversed_sections
        sec_offs: dict[str, float] = {}
        for i, lid in enumerate(unique_entry):
            if sec_reverse:
                sec_offs[lid] = (len(unique_entry) - 1 - i) * ctx.offset_step
            else:
                sec_offs[lid] = i * ctx.offset_step
        for sid_s, station in graph.stations.items():
            if station.section_id != sec_id:
                continue
            slines = graph.station_lines(sid_s)
            present = [lid for lid in slines if lid in sec_offs]
            if len(slines) >= 2 and present:
                for lid in present:
                    ctx.offsets[(sid_s, lid)] = sec_offs[lid]
            elif station.is_hidden and len(slines) == 1 and slines[0] in sec_offs:
                ctx.offsets[(sid_s, slines[0])] = sec_offs[slines[0]]


def _compute_exit_port_offsets(ctx: _OffsetCtx) -> None:
    """Compute exit port offsets for TB and LR/RL sections.

    TB sections with LEFT/RIGHT exits: reverse internal offsets so the
    concentric arc ordering is correct.

    LR/RL sections with LEFT/RIGHT exits: use spatial Y ordering of
    feeding stations to prevent visual crossings, and propagate to
    upstream hub stations.
    """
    graph = ctx.graph

    # TB section LEFT/RIGHT exit ports
    for port_id, port_obj in graph.ports.items():
        if port_obj.is_entry or port_obj.section_id not in ctx.tb_sections:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        internal_offs: dict[str, float] = {}
        for edge in graph.edges:
            if edge.target == port_id:
                src_st = graph.stations.get(edge.source)
                if src_st and not src_st.is_port:
                    internal_offs[edge.line_id] = ctx.offsets.get(
                        (edge.source, edge.line_id), 0.0
                    )
        if internal_offs:
            max_int = max(internal_offs.values())
            for lid, ioff in internal_offs.items():
                ctx.offsets[(port_id, lid)] = max_int - ioff

    # LR/RL section LEFT/RIGHT exit ports: spatial Y ordering
    for port_id, port_obj in graph.ports.items():
        if port_obj.is_entry or port_obj.section_id not in ctx.lr_rl_sections:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        line_ys: dict[str, list[float]] = {}
        for edge in graph.edges:
            if edge.target == port_id:
                src_st = graph.stations.get(edge.source)
                if src_st and not src_st.is_port:
                    line_ys.setdefault(edge.line_id, []).append(src_st.y)
        if len(line_ys) < 2:
            continue
        line_avg_y = {lid: sum(ys) / len(ys) for lid, ys in line_ys.items()}
        unique_ys = set(line_avg_y.values())
        if len(unique_ys) < 2:
            continue
        sorted_lines = sorted(
            line_avg_y,
            key=lambda lid: (line_avg_y[lid], ctx.line_priority.get(lid, 0)),
        )
        spatial_offs = {lid: i * ctx.offset_step for i, lid in enumerate(sorted_lines)}
        for lid, off in spatial_offs.items():
            ctx.offsets[(port_id, lid)] = off

        # Propagate to upstream hub stations
        feeder_ids: set[str] = set()
        for edge in graph.edges:
            if edge.target == port_id:
                src_st = graph.stations.get(edge.source)
                if src_st and not src_st.is_port:
                    feeder_ids.add(edge.source)
        if len(feeder_ids) >= 2:
            hub_candidates: set[str] = set()
            for edge in graph.edges:
                if edge.target in feeder_ids:
                    hub_candidates.add(edge.source)
            for hub_id in hub_candidates:
                hub_lines = graph.station_lines(hub_id)
                overlap = [lid for lid in hub_lines if lid in spatial_offs]
                if len(overlap) >= 2:
                    for lid in overlap:
                        ctx.offsets[(hub_id, lid)] = spatial_offs[lid]


def _propagate_to_junctions(ctx: _OffsetCtx) -> None:
    """Inherit offsets from upstream exit ports to junctions.

    Junctions have section_id=None so they get default line-priority
    ordering, which may not match the exit port feeding them.
    """
    graph = ctx.graph
    for jid in graph.junctions:
        for edge in graph.edges:
            if edge.target == jid:
                src = graph.stations.get(edge.source)
                port_obj = graph.ports.get(edge.source)
                if src and src.is_port and port_obj and not port_obj.is_entry:
                    for lid in graph.station_lines(jid):
                        port_off = ctx.offsets.get((edge.source, lid))
                        if port_off is not None:
                            ctx.offsets[(jid, lid)] = port_off
                    break


def _compute_entry_port_offsets(ctx: _OffsetCtx) -> None:
    """Compute entry port offsets and propagate to downstream stations.

    Handles three cases:
    1. TOP entry ports fed by TB BOTTOM exits: match the reversed offset
       scheme used by inter-section routing.
    2. LEFT/RIGHT entry ports fed by a single LR/RL exit: propagate
       spatial ordering to prevent bundle crossings.
    3. Compact mode: ensure multi-line entry ports have separated offsets
       and propagate to upstream exit ports.
    """
    graph = ctx.graph

    # --- TOP entry ports fed by TB BOTTOM exits ---
    tb_right_entry: set[str] = set()
    for port_obj in graph.ports.values():
        if (
            port_obj.is_entry
            and port_obj.side == PortSide.RIGHT
            and port_obj.section_id in ctx.tb_sections
        ):
            tb_right_entry.add(port_obj.section_id)

    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry or port_obj.side != PortSide.TOP:
            continue
        for edge in graph.edges:
            if edge.target != port_id:
                continue
            src = graph.stations.get(edge.source)
            if not src or not src.is_port:
                continue
            src_port = graph.ports.get(edge.source)
            if not (
                src_port
                and not src_port.is_entry
                and src_port.side == PortSide.BOTTOM
                and src.section_id in ctx.tb_sections
            ):
                continue
            exit_port_id = edge.source
            all_exit_offs = [
                ctx.offsets.get((exit_port_id, lid), 0.0)
                for lid in graph.station_lines(exit_port_id)
            ]
            max_exit_off = max(all_exit_offs) if all_exit_offs else 0.0
            if src.section_id in tb_right_entry:
                for lid in graph.station_lines(port_id):
                    ctx.offsets[(port_id, lid)] = ctx.offsets.get(
                        (exit_port_id, lid), 0.0
                    )
            else:
                for lid in graph.station_lines(port_id):
                    exit_off = ctx.offsets.get((exit_port_id, lid), 0.0)
                    ctx.offsets[(port_id, lid)] = max_exit_off - exit_off
            break

    # --- LR/RL exit-to-entry port propagation ---
    for port_id, port_obj in graph.ports.items():
        if not port_obj.is_entry:
            continue
        if port_obj.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        feeding_exit_ports: set[str] = set()
        for edge in graph.edges:
            if edge.target != port_id:
                continue
            src = graph.stations.get(edge.source)
            if not src or not src.is_port:
                continue
            src_port = graph.ports.get(edge.source)
            if src_port and not src_port.is_entry:
                feeding_exit_ports.add(edge.source)
        if len(feeding_exit_ports) != 1:
            continue
        exit_port_id = next(iter(feeding_exit_ports))
        src_port = graph.ports.get(exit_port_id)
        if not (src_port and src_port.section_id in ctx.lr_rl_sections):
            continue
        exit_lines = set(graph.station_lines(exit_port_id))
        entry_lines = set(graph.station_lines(port_id))
        if exit_lines != entry_lines:
            continue
        entry_offs: dict[str, float] = {}
        for lid in graph.station_lines(port_id):
            exit_off = ctx.offsets.get((exit_port_id, lid))
            if exit_off is not None:
                ctx.offsets[(port_id, lid)] = exit_off
                entry_offs[lid] = exit_off
        if len(entry_offs) >= 2:
            for e2 in graph.edges:
                if e2.source == port_id:
                    tgt_st = graph.stations.get(e2.target)
                    if tgt_st and not tgt_st.is_port:
                        tgt_lines = graph.station_lines(e2.target)
                        overlap = [lid for lid in tgt_lines if lid in entry_offs]
                        if len(overlap) >= 2:
                            for lid in overlap:
                                ctx.offsets[(e2.target, lid)] = entry_offs[lid]

    # --- Compact entry port offset separation ---
    if ctx.compact:
        for sec_id, section in graph.sections.items():
            entry_lines: list[str] = []
            for pid in section.entry_ports:
                entry_lines.extend(graph.station_lines(pid))
            unique = sorted(set(entry_lines), key=lambda x: ctx.line_priority.get(x, 0))
            if len(unique) < 2:
                continue
            existing = [
                ctx.offsets.get((pid, lid), 0.0)
                for pid in section.entry_ports
                for lid in unique
                if lid in graph.station_lines(pid)
            ]
            if len(set(existing)) >= 2:
                continue
            sec_reverse = sec_id in ctx.reversed_sections
            for i, lid in enumerate(unique):
                if sec_reverse:
                    off = (len(unique) - 1 - i) * ctx.offset_step
                else:
                    off = i * ctx.offset_step
                for pid in section.entry_ports:
                    if lid in graph.station_lines(pid):
                        ctx.offsets[(pid, lid)] = off
                        for edge in graph.edges:
                            if edge.target == pid and edge.line_id == lid:
                                src_port = graph.ports.get(edge.source)
                                if src_port and not src_port.is_entry:
                                    ctx.offsets[(edge.source, lid)] = off


def _compact_station_gaps(ctx: _OffsetCtx) -> None:
    """Close offset gaps at stations where intermediate lines are absent.

    When a station carries two non-adjacent lines (e.g. star_salmon and
    bowtie2_salmon with hisat2 absent), the gap for the missing line is
    wasted space.  This phase detects such gaps and compacts the offsets
    so present lines use consecutive slots.

    To avoid near-diagonal edges (lines transitioning between stations
    on the same base Y with different offsets), the compaction is
    propagated along same-Y edges within the section.  The entire
    compaction is abandoned if propagation would hit a station where
    the reordering conflicts with existing offset assignments (e.g. a
    multi-line hub where swapping slots would collide).

    Only triggers when gaps are actually found; no-op otherwise.
    """
    if ctx.compact:
        return

    graph = ctx.graph

    # Bidirectional same-Y adjacency per section.  Both directions are
    # needed so propagation can walk upstream and downstream from the seed.
    # Uses direct section_id comparison (excludes junctions, which have
    # section_id=None and are handled by port/junction offset phases).
    same_y_adj: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if not src or not tgt:
            continue
        if not src.section_id or src.section_id != tgt.section_id:
            continue
        if abs(src.y - tgt.y) > 0.1:
            continue
        sec_id = src.section_id
        if sec_id not in same_y_adj:
            same_y_adj[sec_id] = {}
        same_y_adj[sec_id].setdefault(edge.source, []).append(
            (edge.target, edge.line_id)
        )
        same_y_adj[sec_id].setdefault(edge.target, []).append(
            (edge.source, edge.line_id)
        )

    # Pre-build layer index per section for same-layer checks
    sec_layer_stations: dict[str, dict[int, list[str]]] = {}
    for sid, st in graph.stations.items():
        if st.section_id and not st.is_port:
            sec_layer_stations.setdefault(st.section_id, {}).setdefault(
                st.layer, []
            ).append(sid)

    for sec_id, section in graph.sections.items():
        sec_stations = [
            sid for sid in section.station_ids if not graph.stations[sid].is_port
        ]
        if not sec_stations:
            continue

        # Prevent a later seed from re-processing stations already
        # touched by an earlier compaction in this section.
        already_compacted: set[str] = set()

        for seed_sid in sec_stations:
            if seed_sid in already_compacted:
                continue
            seed_lines = graph.station_lines(seed_sid)
            if len(seed_lines) < 2:
                continue

            current = {lid: ctx.offsets.get((seed_sid, lid), 0.0) for lid in seed_lines}
            sorted_by_off = sorted(current.items(), key=lambda x: x[1])
            base_off = sorted_by_off[0][1]
            expected = [
                base_off + i * ctx.offset_step for i in range(len(sorted_by_off))
            ]
            actual = [off for _, off in sorted_by_off]
            if actual == expected:
                continue

            compacted: dict[str, float] = {}
            for i, (lid, _) in enumerate(sorted_by_off):
                compacted[lid] = base_off + i * ctx.offset_step

            changed_lids = {
                lid for lid in seed_lines if abs(compacted[lid] - current[lid]) > 0.001
            }
            if not changed_lids:
                continue

            # BFS to collect all stations needing consistent updates.
            # Map: station_id -> {line_id: new_offset}
            pending: dict[str, dict[str, float]] = {seed_sid: compacted}
            visited: set[tuple[str, str]] = set()
            queue: deque[tuple[str, str]] = deque(
                (seed_sid, lid) for lid in changed_lids
            )
            safe = True
            max_steps = len(sec_stations) * len(graph.lines)

            while queue and safe and max_steps > 0:
                max_steps -= 1
                cur_sid, lid = queue.popleft()
                if (cur_sid, lid) in visited:
                    continue
                visited.add((cur_sid, lid))

                new_off = pending[cur_sid][lid]

                adj = same_y_adj.get(sec_id, {}).get(cur_sid, [])
                for nbr_sid, edge_lid in adj:
                    if edge_lid != lid:
                        continue
                    if (nbr_sid, lid) in visited:
                        continue

                    # Read pending value if a prior BFS step already
                    # scheduled a change, otherwise use current offset.
                    nbr_cur = pending.get(nbr_sid, {}).get(
                        lid, ctx.offsets.get((nbr_sid, lid), 0.0)
                    )
                    if abs(nbr_cur - new_off) < 0.001:
                        continue

                    nbr_lines = graph.station_lines(nbr_sid)

                    # Bail if a visible same-layer peer also carries
                    # this line - compaction can't guarantee consistency
                    # without cascading into unrelated stations.
                    nbr_st = graph.stations[nbr_sid]
                    layer_peers = sec_layer_stations.get(sec_id, {}).get(
                        nbr_st.layer, []
                    )
                    for peer_sid in layer_peers:
                        if peer_sid == nbr_sid:
                            continue
                        if graph.stations[peer_sid].is_hidden:
                            continue
                        if lid in graph.station_lines(peer_sid):
                            safe = False
                            break
                    if not safe:
                        break

                    if len(nbr_lines) == 1:
                        pending.setdefault(nbr_sid, {})[lid] = new_off
                        queue.append((nbr_sid, lid))
                        continue

                    # Check for collision with another line's offset
                    collision_lid = None
                    for other_lid in nbr_lines:
                        if other_lid == lid:
                            continue
                        other_off = pending.get(nbr_sid, {}).get(
                            other_lid,
                            ctx.offsets.get((nbr_sid, other_lid), 0.0),
                        )
                        if abs(other_off - new_off) < 0.001:
                            collision_lid = other_lid
                            break

                    nbr_pending = pending.setdefault(nbr_sid, {})
                    nbr_pending[lid] = new_off
                    queue.append((nbr_sid, lid))
                    if collision_lid is not None:
                        # Swap: move collider to the slot we're vacating
                        nbr_pending[collision_lid] = nbr_cur
                        queue.append((nbr_sid, collision_lid))

            if not safe or max_steps <= 0:
                continue

            for sid, line_offsets in pending.items():
                for lid, off in line_offsets.items():
                    ctx.offsets[(sid, lid)] = off
                already_compacted.add(sid)


def _same_section(graph: MetroGraph, id_a: str, id_b: str) -> bool:
    """Check if two stations/ports belong to the same section."""
    sa = graph.stations[id_a]
    sb = graph.stations[id_b]
    sec_a = sa.section_id
    sec_b = sb.section_id
    if sec_a and sec_b and sec_a == sec_b:
        return True
    # Junctions (section_id=None): check via port lookup
    if sec_a is None and id_a in graph.ports:
        sec_a = graph.ports[id_a].section_id
    if sec_b is None and id_b in graph.ports:
        sec_b = graph.ports[id_b].section_id
    return bool(sec_a and sec_b and sec_a == sec_b)


def _would_collide(
    ctx: _OffsetCtx, station_id: str, line_id: str, value: float
) -> bool:
    """Check if setting (station_id, line_id) to value collides with another line."""
    return any(
        ctx.offsets.get((station_id, lid), 0.0) == value
        for lid in ctx.graph.station_lines(station_id)
        if lid != line_id
    )


def _reconcile_horizontal_offsets(ctx: _OffsetCtx, max_iterations: int = 10) -> None:
    """Snap offsets for same-section edges where endpoints share base Y.

    Only processes edges where both endpoints belong to the same
    section. Inter-section offset mismatches are handled by routing
    (L-shaped paths with vertical segments), so they must not be
    reconciled here - doing so cascades offsets across section
    boundaries and breaks per-section reindexing.

    For each qualifying edge, tries snapping both stations to the
    larger-magnitude offset first, then the smaller. A candidate is
    rejected if it would collide with another line at the same
    station. If neither simple snap works, shifts the entire bundle
    at the station with fewer lines (preserving relative spacing).

    Iterates until stable, since fixing one edge can propagate
    through port -> station chains within the same section.
    """
    # Pre-filter to edges where both endpoints share the same Y and
    # section. These properties are immutable during reconciliation.
    candidates = [
        edge
        for edge in ctx.graph.edges
        if abs(ctx.graph.stations[edge.source].y - ctx.graph.stations[edge.target].y)
        <= 0.1
        and _same_section(ctx.graph, edge.source, edge.target)
    ]

    for _ in range(max_iterations):
        changed = False
        for edge in candidates:
            lid = edge.line_id
            src_off = ctx.offsets.get((edge.source, lid), 0.0)
            tgt_off = ctx.offsets.get((edge.target, lid), 0.0)
            if src_off == tgt_off:
                continue

            larger = src_off if abs(src_off) >= abs(tgt_off) else tgt_off
            smaller = tgt_off if larger == src_off else src_off

            applied = False
            for candidate in (larger, smaller):
                src_ok = src_off == candidate or not _would_collide(
                    ctx, edge.source, lid, candidate
                )
                tgt_ok = tgt_off == candidate or not _would_collide(
                    ctx, edge.target, lid, candidate
                )
                if src_ok and tgt_ok:
                    ctx.offsets[(edge.source, lid)] = candidate
                    ctx.offsets[(edge.target, lid)] = candidate
                    applied = True
                    changed = True
                    break

            if not applied:
                # Both candidates collide; shift the bundle at the
                # station with fewer lines (least disruption).
                src_n = len(ctx.graph.station_lines(edge.source))
                tgt_n = len(ctx.graph.station_lines(edge.target))
                if src_n <= tgt_n:
                    move_sid, target_val = edge.source, tgt_off
                else:
                    move_sid, target_val = edge.target, src_off
                cur = ctx.offsets.get((move_sid, lid), 0.0)
                delta = target_val - cur
                for other_lid in ctx.graph.station_lines(move_sid):
                    old = ctx.offsets.get((move_sid, other_lid), 0.0)
                    ctx.offsets[(move_sid, other_lid)] = old + delta
                changed = True

        if not changed:
            break


def compute_station_offsets(
    graph: MetroGraph,
    offset_step: float = OFFSET_STEP,
) -> dict[tuple[str, str], float]:
    """Compute per-station Y offsets for each line.

    Each line gets a globally consistent offset based on its declaration
    order (priority). This ensures lines maintain their position within
    bundles across all sections - when a line splits off and later
    rejoins, it returns to its reserved slot rather than shifting.

    Runs in eight phases:

    1. **Base offsets** - global priority (or compact-mode) assignment.
    2. **Section-local re-indexing** - closes priority gaps within
       sections and applies reconvergence ordering (non-compact only).
    3. **Compact section consistency** - ensures entry lines have
       consistent offsets across multi-line stations (compact only).
    4. **Station gap compaction** - closes per-station offset gaps
       where intermediate lines are absent, propagating along same-Y
       edges with conservative safety checks (non-compact only).
    5. **Exit port offsets** - TB reversed offsets and LR/RL spatial
       Y ordering with hub propagation.
    6. **Junction inheritance** - copies exit port offsets to junctions.
    7. **Entry port offsets** - TOP entry override for TB BOTTOM exits,
       LR/RL exit-to-entry propagation, compact entry separation.
    8. **Horizontal reconciliation** - snaps mismatched offsets on
       same-Y edges to eliminate almost-horizontal slopes.

    Returns dict mapping (station_id, line_id) -> y_offset.
    """
    ctx = _build_offset_ctx(graph, offset_step)
    _compute_base_offsets(ctx)
    _reindex_section_local(ctx)
    _apply_compact_section_consistency(ctx)
    _compact_station_gaps(ctx)
    _compute_exit_port_offsets(ctx)
    _propagate_to_junctions(ctx)
    _compute_entry_port_offsets(ctx)
    _reconcile_horizontal_offsets(ctx)
    return ctx.offsets
