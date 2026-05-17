"""Constraint-solver layout pass (spike for issue #351).

SPIKE CODE - draft-PR wiring for the render-diff demo on the spike
branch. Not for merge as-is. The verdict and migration plan live in
docs/constraint-solver-spike.md.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import kiwisolver as kiwi

from nf_metro.layout.constants import (
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.parser.model import MetroGraph, PortSide


@dataclass
class Topology:
    station_layer: dict[str, int] = field(default_factory=dict)
    station_track: dict[str, int] = field(default_factory=dict)
    station_x: dict[str, float] = field(default_factory=dict)
    station_section: dict[str, str] = field(default_factory=dict)
    station_is_port: dict[str, bool] = field(default_factory=dict)
    station_off_track: dict[str, bool] = field(default_factory=dict)
    station_is_terminus: dict[str, bool] = field(default_factory=dict)
    port_side: dict[str, PortSide] = field(default_factory=dict)
    section_bbox_x: dict[str, float] = field(default_factory=dict)
    section_bbox_w: dict[str, float] = field(default_factory=dict)
    section_direction: dict[str, str] = field(default_factory=dict)
    section_grid_row: dict[str, int] = field(default_factory=dict)
    section_grid_col: dict[str, int] = field(default_factory=dict)
    section_grid_row_span: dict[str, int] = field(default_factory=dict)
    section_grid_col_span: dict[str, int] = field(default_factory=dict)
    section_stations: dict[str, list[str]] = field(default_factory=dict)
    section_entry_ports: dict[str, list[str]] = field(default_factory=dict)
    section_exit_ports: dict[str, list[str]] = field(default_factory=dict)
    engine_station_y: dict[str, float] = field(default_factory=dict)
    engine_bbox_y: dict[str, float] = field(default_factory=dict)
    engine_bbox_h: dict[str, float] = field(default_factory=dict)
    edges_source: list[str] = field(default_factory=list)
    edges_target: list[str] = field(default_factory=list)
    center_ports: bool = False
    pitch: float = 40.0
    origin: float = 0.0


def _harvest(g: MetroGraph, y_spacing: float) -> Topology:
    t = Topology()
    t.center_ports = bool(getattr(g, "center_ports", False))
    for sid, st in g.stations.items():
        t.station_x[sid] = st.x
        t.station_is_port[sid] = bool(st.is_port)
        t.station_off_track[sid] = bool(getattr(st, "off_track", False))
        t.station_is_terminus[sid] = bool(getattr(st, "is_terminus", False))
        layer = getattr(st, "layer", None)
        track = getattr(st, "track", None)
        if layer is not None:
            t.station_layer[sid] = layer
        if track is not None:
            t.station_track[sid] = track
        t.engine_station_y[sid] = st.y
    for pid, port in g.ports.items():
        t.port_side[pid] = port.side
    for sec in g.sections.values():
        t.section_bbox_x[sec.id] = sec.bbox_x
        t.section_bbox_w[sec.id] = sec.bbox_w
        t.section_direction[sec.id] = sec.direction
        t.section_grid_row[sec.id] = sec.grid_row
        t.section_grid_col[sec.id] = sec.grid_col
        t.section_grid_row_span[sec.id] = getattr(sec, "grid_row_span", 1)
        t.section_grid_col_span[sec.id] = getattr(sec, "grid_col_span", 1)
        t.section_stations[sec.id] = list(sec.station_ids)
        t.section_entry_ports[sec.id] = list(sec.entry_ports)
        t.section_exit_ports[sec.id] = list(sec.exit_ports)
        for sid in sec.station_ids:
            t.station_section[sid] = sec.id
        t.engine_bbox_y[sec.id] = sec.bbox_y
        t.engine_bbox_h[sec.id] = sec.bbox_h
    for edge in g.edges:
        t.edges_source.append(edge.source)
        t.edges_target.append(edge.target)
    pitch = y_spacing
    if hasattr(g, "_row_y_grid_info") and g._row_y_grid_info:
        pitch = max(
            (
                info.get("slot_spacing", y_spacing)
                for info in g._row_y_grid_info.values()
            ),
            default=y_spacing,
        )
    residues: list[float] = []
    for sec_id in t.section_stations:
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        for sid in t.section_stations[sec_id]:
            if sid in port_ids or t.station_off_track.get(sid):
                continue
            y = t.engine_station_y.get(sid)
            if y is None:
                continue
            residues.append(round(y % pitch, 3))
    t.pitch = pitch
    t.origin = Counter(residues).most_common(1)[0][0] if residues else 0.0
    return t


@dataclass
class Classification:
    symfan_sections: dict[str, tuple[str, str]] = field(default_factory=dict)
    fanout_columns: list[tuple[float, list[str], str]] = field(default_factory=list)
    sparse_loop_stations: dict[str, int] = field(default_factory=dict)
    no_full_snap_stations: set[str] = field(default_factory=set)


def _classify(t: Topology, g: MetroGraph) -> Classification:
    c = Classification()
    section_lines: dict[str, set[str]] = {}
    for sec_id, sids in t.section_stations.items():
        bundle: set[str] = set()
        for sid in sids:
            for lid in g.station_lines(sid):
                bundle.add(lid)
        section_lines[sec_id] = bundle
    pred: dict[str, list[str]] = defaultdict(list)
    succ: dict[str, list[str]] = defaultdict(list)
    for src, tgt in zip(t.edges_source, t.edges_target):
        succ[src].append(tgt)
        pred[tgt].append(src)

    for sec_id, sids in t.section_stations.items():
        if t.section_direction.get(sec_id) not in ("LR", "RL"):
            continue
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        internal = [
            sid
            for sid in sids
            if sid not in port_ids
            and not t.station_is_port.get(sid)
            and not t.station_off_track.get(sid)
            and not sid.startswith("__")
            and not t.station_is_terminus.get(sid)
        ]
        if len(internal) != 2:
            continue
        a, b = internal[0], internal[1]
        if abs(t.station_x[a] - t.station_x[b]) >= 0.5:
            continue
        per_col: dict[float, int] = defaultdict(int)
        for sid in sids:
            if sid in port_ids or sid.startswith("__"):
                continue
            per_col[round(t.station_x[sid], 1)] += 1
        if any(count > 2 for count in per_col.values()):
            continue
        c.symfan_sections[sec_id] = (a, b)
        c.no_full_snap_stations.add(a)
        c.no_full_snap_stations.add(b)

    for sec_id, sids in t.section_stations.items():
        if t.section_direction.get(sec_id) not in ("LR", "RL"):
            continue
        if not t.center_ports:
            continue
        bundle = section_lines[sec_id]
        if not bundle:
            continue
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        by_col: dict[float, list[str]] = defaultdict(list)
        for sid in sids:
            if sid in port_ids or sid.startswith("__") or t.station_off_track.get(sid):
                continue
            by_col[round(t.station_x[sid], 1)].append(sid)
        for col_x, col_sids in by_col.items():
            if len(col_sids) < 2:
                continue
            full_bundle = [
                sid
                for sid in col_sids
                if frozenset(g.station_lines(sid)) == frozenset(bundle)
            ]
            sub_bundle_with_pred = [
                sid
                for sid in col_sids
                if frozenset(g.station_lines(sid)) < frozenset(bundle) and pred.get(sid)
            ]
            if len(full_bundle) == 1 and len(sub_bundle_with_pred) >= 2:
                c.fanout_columns.append(
                    (col_x, [full_bundle[0]] + sub_bundle_with_pred, "trunk_station_y")
                )
                continue
            if len(full_bundle) >= 2 and len(full_bundle) == len(col_sids):
                c.fanout_columns.append((col_x, col_sids, "row_trunk_y"))

    for sec_id, sids in t.section_stations.items():
        if t.section_direction.get(sec_id) not in ("LR", "RL"):
            continue
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        internal = [
            sid
            for sid in sids
            if sid not in port_ids
            and not sid.startswith("__")
            and not t.station_off_track.get(sid)
        ]
        per_col: dict[float, list[str]] = defaultdict(list)
        for sid in internal:
            per_col[round(t.station_x[sid], 1)].append(sid)
        for col_x, col_sids in per_col.items():
            if len(col_sids) < 2:
                continue
            line_counts = {sid: len(g.station_lines(sid)) for sid in col_sids}
            max_lines = max(line_counts.values())
            min_lines = min(line_counts.values())
            if max_lines == min_lines:
                continue
            for sid in col_sids:
                if line_counts[sid] != min_lines or line_counts[sid] != 1:
                    continue
                ins = [s for s in pred.get(sid, []) if s not in port_ids]
                outs = [s for s in succ.get(sid, []) if s not in port_ids]
                if len(ins) != 1 or len(outs) != 1:
                    continue
                engine_y = t.engine_station_y.get(sid)
                trunk_hint_y = None
                for pid in port_ids:
                    if pid in t.port_side and t.port_side[pid] in (
                        PortSide.LEFT,
                        PortSide.RIGHT,
                    ):
                        trunk_hint_y = t.engine_station_y.get(pid)
                        break
                if engine_y is None or trunk_hint_y is None:
                    continue
                c.sparse_loop_stations[sid] = 1 if (engine_y - trunk_hint_y) > 0 else -1
                c.no_full_snap_stations.add(sid)
    return c


def _row_contig_groups(t: Topology) -> list[list[str]]:
    by_row: dict[int, list[str]] = defaultdict(list)
    for sec_id, row in t.section_grid_row.items():
        if row is None or row < 0:
            continue
        by_row[row].append(sec_id)
    out: list[list[str]] = []
    for sections in by_row.values():
        sections.sort(key=lambda s: t.section_grid_col[s])
        cur = [sections[0]]
        for s in sections[1:]:
            if t.section_grid_col[s] - t.section_grid_col[cur[-1]] <= 1:
                cur.append(s)
            else:
                if len(cur) >= 2:
                    out.append(cur)
                cur = [s]
        if len(cur) >= 2:
            out.append(cur)
    return out


def _section_trunk_station(g: MetroGraph, t: Topology, sec_id: str) -> Optional[str]:
    if t.section_direction.get(sec_id) not in ("LR", "RL"):
        return None
    port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
    internal = [
        sid
        for sid in t.section_stations[sec_id]
        if sid not in port_ids
        and not sid.startswith("__")
        and not t.station_off_track.get(sid)
    ]
    bundle: Optional[frozenset] = None
    for src, tgt in zip(t.edges_source, t.edges_target):
        if src in port_ids and tgt in internal:
            lines = frozenset(g.station_lines(tgt))
            if bundle is None or len(lines) > len(bundle):
                bundle = lines
        elif tgt in port_ids and src in internal:
            lines = frozenset(g.station_lines(src))
            if bundle is None or len(lines) > len(bundle):
                bundle = lines
    if not bundle:
        return None
    for sid in internal:
        if frozenset(g.station_lines(sid)) == bundle:
            return sid
    return None


def _off_track_consumers(t: Topology) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    by_consumer: dict[str, list[str]] = defaultdict(list)
    succ: dict[str, list[str]] = defaultdict(list)
    for src, tgt in zip(t.edges_source, t.edges_target):
        succ[src].append(tgt)
    for sid in t.station_off_track:
        if not t.station_off_track[sid]:
            continue
        succs = succ.get(sid, [])
        if len(succs) == 1:
            by_consumer[succs[0]].append(sid)
    for cons, inputs in by_consumer.items():
        cy = t.engine_station_y.get(cons, 0.0)
        inputs.sort(key=lambda i: abs(t.engine_station_y.get(i, 0.0) - cy))
        for rank, inp in enumerate(inputs, start=1):
            out[inp] = (cons, rank)
    return out


def apply_to_graph(graph: MetroGraph, y_spacing: float) -> dict:
    """Override Y geometry on `graph` using the constraint model.

    Precondition: `graph` is fully laid out by the imperative engine.
    Postcondition: every station / port / junction / section bbox Y is
    overridden by the solver. X coords and bbox widths are untouched.
    """
    t = _harvest(graph, y_spacing)
    c = _classify(t, graph)

    solver = kiwi.Solver()
    y_vars: dict[str, kiwi.Variable] = {}
    by_vars: dict[str, kiwi.Variable] = {}
    bh_vars: dict[str, kiwi.Variable] = {}
    row_trunk_vars: dict[int, kiwi.Variable] = {}

    for sid in t.station_section:
        y_vars[sid] = kiwi.Variable(f"y_{sid}")
    for sec_id in t.section_stations:
        by_vars[sec_id] = kiwi.Variable(f"by_{sec_id}")
        bh_vars[sec_id] = kiwi.Variable(f"bh_{sec_id}")

    bbox_pad = SECTION_Y_PADDING
    port_pad = STATION_RADIUS_APPROX + 2.0
    ELBOW_GAP = STATION_RADIUS_APPROX + 7.0

    # C12: weak anchors to engine Ys
    for sid, y in t.engine_station_y.items():
        if sid in y_vars:
            solver.addConstraint((y_vars[sid] == y) | "weak")
    for sec_id in t.section_stations:
        solver.addConstraint((by_vars[sec_id] == t.engine_bbox_y[sec_id]) | "weak")
        engine_h = max(t.engine_bbox_h.get(sec_id, 1.0), 1.0)
        solver.addConstraint((bh_vars[sec_id] == engine_h) | "weak")

    # C11: bbox_h contains stations
    for sec_id, sids in t.section_stations.items():
        by = by_vars[sec_id]
        bh = bh_vars[sec_id]
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        for sid in sids:
            if sid not in y_vars:
                continue
            v = y_vars[sid]
            if sid in port_ids or t.station_is_port.get(sid):
                continue
            solver.addConstraint((v >= by + bbox_pad) | "required")
            solver.addConstraint((bh >= v - by + bbox_pad) | "required")

    # C7: port on bbox edge
    for sec_id, sids in t.section_stations.items():
        by = by_vars[sec_id]
        bh = bh_vars[sec_id]
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        for pid in port_ids:
            if pid not in y_vars:
                continue
            v = y_vars[pid]
            side = t.port_side.get(pid)
            if side == PortSide.TOP:
                solver.addConstraint((v == by) | "required")
            elif side == PortSide.BOTTOM:
                solver.addConstraint((v == by + bh) | "required")
            else:
                solver.addConstraint((v >= by + port_pad) | "required")
                solver.addConstraint((v <= by + bh - port_pad) | "required")

    # C2b: perp-port slot vs internal stations (TB sections, LEFT/RIGHT ports)
    for sec_id, sids in t.section_stations.items():
        if t.section_direction.get(sec_id, "LR") not in ("TB", "BT"):
            continue
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        side_ports = [
            pid
            for pid in port_ids
            if pid in y_vars and t.port_side.get(pid) in (PortSide.LEFT, PortSide.RIGHT)
        ]
        if not side_ports:
            continue
        internal = [
            sid
            for sid in sids
            if sid not in port_ids
            and not sid.startswith("__")
            and not t.station_is_port.get(sid)
        ]
        if not internal:
            continue
        internal_sorted = sorted(internal, key=lambda s: t.engine_station_y.get(s, 0.0))
        for pid in side_ports:
            pe = t.engine_station_y.get(pid)
            if pe is None:
                continue
            for sid in internal_sorted:
                se = t.engine_station_y.get(sid, 0.0)
                if pe + 0.5 < se:
                    solver.addConstraint(
                        (y_vars[pid] + ELBOW_GAP <= y_vars[sid]) | "required"
                    )
                elif pe - 0.5 > se:
                    solver.addConstraint(
                        (y_vars[pid] >= y_vars[sid] + ELBOW_GAP) | "required"
                    )

    # C2: same-layer ordering (LR/RL by track in layer; TB/BT by layer in track)
    for sec_id, sids in t.section_stations.items():
        is_tb = t.section_direction.get(sec_id, "LR") in ("TB", "BT")
        groups: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for sid in sids:
            if sid not in y_vars or t.station_is_port.get(sid) or sid.startswith("__"):
                continue
            layer = t.station_layer.get(sid)
            track = t.station_track.get(sid)
            if layer is None or track is None:
                continue
            if is_tb:
                groups[track].append((layer, sid))
            else:
                groups[layer].append((track, sid))
        for items in groups.values():
            if len(items) < 2:
                continue
            items.sort()
            for (_, a), (_, b) in zip(items, items[1:]):
                solver.addConstraint((y_vars[b] >= y_vars[a] + y_spacing) | "required")

    # C3/C5: edge straightness
    for src, tgt in zip(t.edges_source, t.edges_target):
        if src not in y_vars or tgt not in y_vars:
            continue
        same = t.station_section.get(src) == t.station_section.get(tgt)
        strength = "strong" if same else "medium"
        solver.addConstraint((y_vars[src] == y_vars[tgt]) | strength)

    # C8: same-row bbox_y
    groups = _row_contig_groups(t)
    for group in groups:
        for a, b in zip(group, group[1:]):
            solver.addConstraint((by_vars[a] == by_vars[b]) | "strong")

    # C9: same-row trunk Y
    for group in groups:
        row = t.section_grid_row[group[0]]
        if row not in row_trunk_vars:
            row_trunk_vars[row] = kiwi.Variable(f"row_trunk_{row}")
        rv = row_trunk_vars[row]
        for sec_id in group:
            trunk = _section_trunk_station(graph, t, sec_id)
            if trunk and trunk in y_vars:
                solver.addConstraint((y_vars[trunk] == rv) | "medium")

    # C10: off-track stack
    for inp, (cons, rank) in _off_track_consumers(t).items():
        if inp in y_vars and cons in y_vars:
            solver.addConstraint(
                (y_vars[inp] == y_vars[cons] - rank * y_spacing) | "required"
            )

    # C13: row gap
    sec_ids = list(t.section_stations.keys())
    for su in sec_ids:
        ru = t.section_grid_row[su]
        if ru is None or ru < 0:
            continue
        ru_end = ru + max(1, t.section_grid_row_span[su]) - 1
        for sl in sec_ids:
            if sl == su:
                continue
            rl = t.section_grid_row[sl]
            if rl is None or rl != ru_end + 1:
                continue
            solver.addConstraint(
                (by_vars[sl] >= by_vars[su] + bh_vars[su] + SECTION_Y_GAP) | "required"
            )

    # C6: grid snap (weak)
    for sid, v in y_vars.items():
        if sid in c.no_full_snap_stations:
            continue
        if t.station_is_port.get(sid) or sid.startswith("__"):
            continue
        if t.station_off_track.get(sid):
            continue
        hint = t.engine_station_y.get(sid)
        if hint is None:
            continue
        target = t.origin + round((hint - t.origin) / t.pitch) * t.pitch
        solver.addConstraint((v == target) | "weak")

    # C14: half-grid 2-branch symfan
    for sec_id, (a, b) in c.symfan_sections.items():
        if t.engine_station_y.get(a, 0) > t.engine_station_y.get(b, 0):
            a, b = b, a
        port_ids = set(t.section_entry_ports[sec_id]) | set(
            t.section_exit_ports[sec_id]
        )
        anchor_var = None
        for pid in port_ids:
            if (
                t.port_side.get(pid) in (PortSide.LEFT, PortSide.RIGHT)
                and pid in y_vars
            ):
                anchor_var = y_vars[pid]
                break
        if anchor_var is None:
            row = t.section_grid_row.get(sec_id)
            anchor_var = row_trunk_vars.get(row)
        if anchor_var is None:
            continue
        half = y_spacing / 2.0
        solver.addConstraint((y_vars[a] == anchor_var - half) | "strong")
        solver.addConstraint((y_vars[b] == anchor_var + half) | "strong")

    # C15: fan-out symmetric around trunk
    for col_x, participants, anchor_kind in c.fanout_columns:
        if anchor_kind == "trunk_station_y":
            trunk = participants[0]
            others_sorted = sorted(
                participants[1:], key=lambda s: t.engine_station_y.get(s, 0.0)
            )
            for i, sid in enumerate(others_sorted, start=1):
                if sid not in y_vars or trunk not in y_vars:
                    continue
                k = (i + 1) // 2
                sign = 1 if i % 2 == 1 else -1
                solver.addConstraint(
                    (y_vars[sid] == y_vars[trunk] + sign * k * y_spacing) | "medium"
                )
        elif anchor_kind == "row_trunk_y":
            sec_for_col = next(
                (
                    sec_id
                    for sec_id, sids in t.section_stations.items()
                    if participants[0] in sids
                ),
                None,
            )
            if sec_for_col is None:
                continue
            row = t.section_grid_row.get(sec_for_col)
            anchor_var = row_trunk_vars.get(row)
            if anchor_var is None:
                continue
            participants_sorted = sorted(
                participants, key=lambda s: t.engine_station_y.get(s, 0.0)
            )
            n = len(participants_sorted)
            offsets = (
                list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
                if n % 2 == 0
                else list(range(-(n // 2), n // 2 + 1))
            )
            for sid, off in zip(participants_sorted, offsets):
                if sid in y_vars:
                    solver.addConstraint(
                        (y_vars[sid] == anchor_var + off * y_spacing) | "medium"
                    )

    # C16: sparse loop full-pitch shift
    for sid, direction in c.sparse_loop_stations.items():
        sec_id = t.station_section.get(sid)
        if sec_id is None:
            continue
        row = t.section_grid_row.get(sec_id)
        anchor_var = row_trunk_vars.get(row)
        if anchor_var is None:
            continue
        solver.addConstraint(
            (y_vars[sid] == anchor_var + direction * y_spacing) | "strong"
        )

    solver.updateVariables()

    for sec_id, var in by_vars.items():
        graph.sections[sec_id].bbox_y = var.value()
        graph.sections[sec_id].bbox_h = bh_vars[sec_id].value()
    for sid, var in y_vars.items():
        if sid in graph.stations:
            graph.stations[sid].y = var.value()

    return {
        "y_spacing": y_spacing,
        "pitch": t.pitch,
        "origin": t.origin,
        "symfan_count": len(c.symfan_sections),
        "fanout_count": len(c.fanout_columns),
        "sparse_loop_count": len(c.sparse_loop_stations),
    }
