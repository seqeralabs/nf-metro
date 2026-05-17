"""Comprehensive constraint-solver model for Phase 4-13m replacement.

Issue: https://github.com/pinin4fjords/nf-metro/issues/351
Supersedes the partial-replacement spike in PR #350.

Architecture
------------
Phases 2/3 (discrete combinatorial) stay imperative. The solver replaces
everything from Phase 4 (local-to-global translation) through Phase 13m
(stacked-icon padding). One declarative kiwisolver pass.

Variables (Y axis only; X is precomputed):
- station.y for every station (real, port, junction, off-track)
- section.bbox_y and section.bbox_h for every section

Inputs (precomputed from Phase 2/3 output, fed as constants):
- station local x + section.offset_x => global x (deterministic)
- section.bbox_w (fixed by section_placement)
- layer / track / grid_row / grid_col / direction
- port.side
- station.off_track flag
- topology (edges, station_lines)

Constraint families
-------------------
The 10 families from the PR #350 catalogue (C1-C10) plus six new families
(C11-C16) covering the six "Verify" items from the linearity audit:

C1  R       station-in-section containment           (Phase 2 / Phase 13 invariant)
C2  R       same-layer track-sorted non-overlap      (Phase 2 layering)
C3  S       intra-section edge straightness          (Phase 2/10b)
C4  S       LR/RL port-to-station snap               (Phase 10b/10c/10d)
C5  M       inter-section edge straightness          (Phase 6/8/13c)
C6  W       grid snap                                (Phase 2.5/13e)
C7  R       port-on-bbox-edge                        (Phase 5)
C8  S       same-row bbox_y equality                 (Phase 9/13b)
C9  M       same-row trunk-Y equality                (Phase 11ca)
C10 R       off-track stack at consumer.y - n*ys     (Phase 13/13g)
C11 R       bbox_h grows to contain station.y        (Phase 11 _expand_bbox_for_y)
C12 W       bbox_h tightness attractor               (Phase 13j shrink)
C13 R       row-gap inequality                       (Phase 13k/13l)
C14 R*      half-grid 2-branch symfan offset         (Phase 13d3, conditional)
C15 M*      fan-out symmetric around trunk           (Phase 11d/11da/13h, conditional)
C16 R*      sparse-loop full-pitch shift             (Phase 13k2, conditional)

R=REQUIRED, S=STRONG, M=MEDIUM, W=WEAK. Asterisk = conditional (only applied
when a topology-based classifier predicate fires).

The conditional families (C14/C15/C16) are pre-classified from post-Phase-3
topology (no Y-dependence in the predicate), then their constraints are
added unconditionally for the marked stations. Sparse-loop's runtime
"shares row Y with busier sibling" check is reinterpreted as a static
"shares grid_row + grid_col with a sibling that carries more lines" check.

Usage
-----
    from nf_metro.parser import parse_metro_mermaid
    from scratch.constraint_spike_v3_model import solve

    g = parse_metro_mermaid(open("examples/rnaseq_sections.mmd").read())
    solve(g)   # mutates g.stations / g.sections / g.ports in place

`solve()` runs the engine on a deepcopy to harvest topology, then builds
and solves the kiwisolver model, then writes Ys back to `g`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

import kiwisolver as kiwi

from nf_metro.layout import compute_layout
from nf_metro.layout.constants import (
    JUNCTION_MARGIN,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    STATION_RADIUS_APPROX,
)
from nf_metro.layout.engine import compute_min_y_spacing
from nf_metro.parser.model import MetroGraph, PortSide


# ---------------------------------------------------------------------------
# Topology harvest
# ---------------------------------------------------------------------------

@dataclass
class Topology:
    """Everything the solver needs that isn't a Y variable.

    Harvested by running the engine on a deepcopy of the input graph,
    then reading off the topology-stable attributes. The engine's Y
    choices are discarded; only X, grid layout, side assignments,
    classification flags and bbox widths survive.
    """

    # Per-station: layer, track, off_track flag, is_port, side, section id, X
    station_layer: dict[str, int] = field(default_factory=dict)
    station_track: dict[str, int] = field(default_factory=dict)
    station_x: dict[str, float] = field(default_factory=dict)
    station_section: dict[str, str] = field(default_factory=dict)
    station_is_port: dict[str, bool] = field(default_factory=dict)
    station_off_track: dict[str, bool] = field(default_factory=dict)
    station_is_terminus: dict[str, bool] = field(default_factory=dict)
    port_side: dict[str, PortSide] = field(default_factory=dict)

    # Per-section: bbox_x, bbox_w, direction, grid_row, grid_col, spans
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

    # Hint Ys from the engine's run, used as soft attractors so the solver
    # has a starting point in regions of slack (e.g. weak grid-snap target).
    # These are NOT hard inputs - the spike is allowed to disagree.
    engine_station_y: dict[str, float] = field(default_factory=dict)
    engine_bbox_y: dict[str, float] = field(default_factory=dict)

    # Edge graph
    edges_source: list[str] = field(default_factory=list)
    edges_target: list[str] = field(default_factory=list)

    # center_ports global gate
    center_ports: bool = False

    # Pitch / origin for grid-snap attractor
    pitch: float = 40.0
    origin: float = 0.0


def harvest_topology(g: MetroGraph, y_spacing: float) -> Topology:
    """Run the engine on a deepcopy of `g` and pull every topology-stable
    attribute the solver needs.

    The engine's chosen Ys are kept only as weak attractors (and for
    diagnostics); the spike's whole point is to ignore them as final values.
    """
    snap = deepcopy(g)
    compute_layout(snap, y_spacing=y_spacing, validate=False)

    t = Topology()
    t.center_ports = bool(getattr(snap, "center_ports", False))

    for sid, st in snap.stations.items():
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

    for pid, port in snap.ports.items():
        t.port_side[pid] = port.side

    for sec in snap.sections.values():
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

    for edge in snap.edges:
        t.edges_source.append(edge.source)
        t.edges_target.append(edge.target)

    # Compute grid pitch + origin from the engine's residues. We use a
    # unified pitch (max across all rows) for the spike; per-row pitch
    # variation can come later if needed.
    pitch = y_spacing
    if hasattr(snap, "_row_y_grid_info") and snap._row_y_grid_info:
        pitch = max(
            (info.get("slot_spacing", y_spacing) for info in snap._row_y_grid_info.values()),
            default=y_spacing,
        )
    residues: list[float] = []
    for sec_id in t.section_stations:
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
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


# ---------------------------------------------------------------------------
# Classification (item 4 / item 5 / item 6 preconditions)
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    """Pre-classified topology features that drive conditional constraints.

    All classifiers run from post-Phase-3 state only; no Y dependence.
    Sparse-loop's "shares row Y with busier sibling" is reinterpreted as
    "shares grid_row with a sibling in the same section that has strictly
    more lines."
    """

    # Item 4: section_id -> (branch_a_id, branch_b_id) for sections that
    # qualify as 2-branch symfans (half-pitch offsets around trunk_y).
    symfan_sections: dict[str, tuple[str, str]] = field(default_factory=dict)

    # Item 5: list of (column_xs, participant_station_ids, anchor_kind)
    # for fan-out / full-bundle columns where participants sit symmetric
    # around a trunk Y. anchor_kind is "trunk_station_y" or "row_trunk_y".
    fanout_columns: list[tuple[float, list[str], str]] = field(default_factory=list)

    # Item 6: station_id -> direction (+1 / -1), full-pitch shift away
    # from trunk Y. Stations classified here are also excluded from
    # full-grid snap.
    sparse_loop_stations: dict[str, int] = field(default_factory=dict)

    # Stations excluded from C6 grid snap because they're on half-pitch
    # offsets or have a different snap rule.
    no_full_snap_stations: set[str] = field(default_factory=set)


def classify(t: Topology, g: MetroGraph) -> Classification:
    """Run all conditional-constraint classifiers from topology alone."""
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
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
        internal = [
            sid for sid in sids
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
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
        by_col: dict[float, list[str]] = defaultdict(list)
        for sid in sids:
            if sid in port_ids or sid.startswith("__") or t.station_off_track.get(sid):
                continue
            by_col[round(t.station_x[sid], 1)].append(sid)

        for col_x, col_sids in by_col.items():
            if len(col_sids) < 2:
                continue
            full_bundle = [
                sid for sid in col_sids
                if frozenset(g.station_lines(sid)) == frozenset(bundle)
            ]
            sub_bundle_with_pred = [
                sid for sid in col_sids
                if frozenset(g.station_lines(sid)) < frozenset(bundle)
                and pred.get(sid)
            ]

            if len(full_bundle) == 1 and len(sub_bundle_with_pred) >= 2:
                trunk = full_bundle[0]
                others = sub_bundle_with_pred
                participants = [trunk] + others
                c.fanout_columns.append((col_x, participants, "trunk_station_y"))
                continue

            if len(full_bundle) >= 2 and len(full_bundle) == len(col_sids):
                c.fanout_columns.append((col_x, col_sids, "row_trunk_y"))
                continue

    for sec_id, sids in t.section_stations.items():
        if t.section_direction.get(sec_id) not in ("LR", "RL"):
            continue
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
        internal = [
            sid for sid in sids
            if sid not in port_ids and not sid.startswith("__")
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
                if line_counts[sid] != min_lines:
                    continue
                if line_counts[sid] != 1:
                    continue
                ins = [s for s in pred.get(sid, []) if s not in port_ids]
                outs = [s for s in succ.get(sid, []) if s not in port_ids]
                if len(ins) != 1 or len(outs) != 1:
                    continue
                engine_y = t.engine_station_y.get(sid)
                trunk_hint_y = None
                for pid in port_ids:
                    if pid in t.port_side and t.port_side[pid] in (PortSide.LEFT, PortSide.RIGHT):
                        trunk_hint_y = t.engine_station_y.get(pid)
                        break
                if engine_y is None or trunk_hint_y is None:
                    continue
                direction = 1 if (engine_y - trunk_hint_y) > 0 else -1
                c.sparse_loop_stations[sid] = direction
                c.no_full_snap_stations.add(sid)

    return c


# ---------------------------------------------------------------------------
# Helpers used by build_model
# ---------------------------------------------------------------------------

def _row_contig_groups(t: Topology) -> list[list[str]]:
    """Group sections in each row by contiguous grid_col runs."""
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
    """Pick a representative trunk station for a section.

    Trunk = the full-bundle internal station connected to an LR/RL port.
    Used for C9 (same-row trunk Y equality) and as anchor for C15.
    """
    if t.section_direction.get(sec_id) not in ("LR", "RL"):
        return None
    port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
    internal = [
        sid for sid in t.section_stations[sec_id]
        if sid not in port_ids and not sid.startswith("__")
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
    """For each off-track station, return (consumer_id, stack_rank).

    Stack rank is 1 for the input nearest its consumer, 2 for the next.
    Matches Phase 13's _lift_off_track_stations contract.
    """
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


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------

@dataclass
class Model:
    solver: kiwi.Solver
    y_vars: dict[str, kiwi.Variable]     # station_id -> Y variable
    by_vars: dict[str, kiwi.Variable]    # section_id -> bbox_y variable
    bh_vars: dict[str, kiwi.Variable]    # section_id -> bbox_h variable
    row_trunk_vars: dict[int, kiwi.Variable]  # grid_row -> shared trunk Y variable


def build_model(g: MetroGraph, t: Topology, c: Classification, y_spacing: float) -> Model:
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

    # bbox_pad is the engine's max_y_pad / SECTION_Y_PADDING - the outer
    # padding between real stations and the bbox top/bottom edge. Engine's
    # Phase 11b sets bbox to symmetric padding around station content.
    bbox_pad = SECTION_Y_PADDING
    # port_pad is the tolerance for LR/RL ports sitting on the bbox side
    # edge - they need to be within the bbox but can be near the top/bottom.
    port_pad = STATION_RADIUS_APPROX + 2.0

    # --- C12: weak attractors so unconstrained Ys have a sensible target.
    # These give Cassowary a starting point near the engine's Ys; any harder
    # constraint overrides them. Without an attractor, the solver may pick
    # arbitrary Ys in degenerate sub-models.
    #
    # NOTE: anchoring bbox_h to engine_bbox_h (not to 0) is important. A
    # weak "bbox_h == 0" attractor drags bbox_h below its lower bound
    # whenever station Ys themselves are free, propagating shrinkage through
    # the C13 row-gap inequalities. The "tightness" the engine achieves via
    # Phase 13j shrink is reproduced here by the combination of (a) weak
    # bbox_h anchor at engine's value and (b) the hard C11 lower bound, so
    # bbox_h sits at the larger of "engine's height" and "fit my stations".
    for sid, y in t.engine_station_y.items():
        if sid in y_vars:
            solver.addConstraint((y_vars[sid] == y) | "weak")
    for sec_id in t.section_stations:
        solver.addConstraint((by_vars[sec_id] == t.engine_bbox_y[sec_id]) | "weak")
        engine_h = max(g.sections[sec_id].bbox_h, 1.0)
        solver.addConstraint((bh_vars[sec_id] == engine_h) | "weak")

    # --- C11: bbox_h grows to contain every contained station's Y.
    # bbox_h >= station.y - bbox_y + pad   (top side, with padding)
    # bbox_h >= station.y - bbox_y         (bottom side, content must fit)
    # bbox_y <= station.y - pad            (top padding around station)
    for sec_id, sids in t.section_stations.items():
        by = by_vars[sec_id]
        bh = bh_vars[sec_id]
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
        for sid in sids:
            if sid not in y_vars:
                continue
            v = y_vars[sid]
            if sid in port_ids or t.station_is_port.get(sid):
                # C7: ports sit on bbox edge (handled below)
                continue
            # Real station: top + bbox_pad <= y <= bottom - bbox_pad
            solver.addConstraint((v >= by + bbox_pad) | "required")
            solver.addConstraint((bh >= v - by + bbox_pad) | "required")

    # --- C1: section bbox contains its stations. Already enforced by C11.
    # (Listed separately in the catalogue for documentation; mechanically
    # subsumed by C11's two inequalities.)

    # --- C7: port-on-bbox-edge.
    # TOP/BOTTOM: y == bbox_y / bbox_y + bbox_h
    # LEFT/RIGHT: bbox_y + pad <= y <= bbox_y + bbox_h - pad
    for sec_id, sids in t.section_stations.items():
        by = by_vars[sec_id]
        bh = bh_vars[sec_id]
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
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
                # LR/RL ports sit on the side edge, can be near top/bottom.
                solver.addConstraint((v >= by + port_pad) | "required")
                solver.addConstraint((v <= by + bh - port_pad) | "required")

    # --- C2: same-layer / same-track ordering. Direction-aware.
    # LR/RL: layer drives X, track drives Y. Within a layer (same X column),
    #        stations ordered by track must satisfy y_a + y_spacing <= y_b.
    # TB/BT: layer drives Y, track drives X. Within a track (same X column),
    #        stations ordered by layer must satisfy y_a + y_spacing <= y_b.
    for sec_id, sids in t.section_stations.items():
        direction = t.section_direction.get(sec_id, "LR")
        is_tb = direction in ("TB", "BT")
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

    # --- C3 + C5: edge straightness. Intra-section: strong. Inter-section: medium.
    for src, tgt in zip(t.edges_source, t.edges_target):
        if src not in y_vars or tgt not in y_vars:
            continue
        same = t.station_section.get(src) == t.station_section.get(tgt)
        strength = "strong" if same else "medium"
        solver.addConstraint((y_vars[src] == y_vars[tgt]) | strength)

    # --- C4: subsumed by C3 (port-to-station edges go through C3/C5)

    # --- C8: same-row contiguous bbox_y equality.
    # Strong, not required, because rowspan sections may override.
    groups = _row_contig_groups(t)
    for group in groups:
        for a, b in zip(group, group[1:]):
            solver.addConstraint((by_vars[a] == by_vars[b]) | "strong")

    # --- C9: same-row trunk-Y equality.
    # Find each section's trunk and pin trunks within a row to a shared variable.
    for group in groups:
        row = t.section_grid_row[group[0]]
        if row not in row_trunk_vars:
            row_trunk_vars[row] = kiwi.Variable(f"row_trunk_{row}")
        rv = row_trunk_vars[row]
        for sec_id in group:
            trunk = _section_trunk_station(g, t, sec_id)
            if trunk and trunk in y_vars:
                solver.addConstraint((y_vars[trunk] == rv) | "medium")

    # --- C10: off-track stacking.
    # station.y == consumer.y - rank * y_spacing
    for inp, (cons, rank) in _off_track_consumers(t).items():
        if inp in y_vars and cons in y_vars:
            solver.addConstraint((y_vars[inp] == y_vars[cons] - rank * y_spacing) | "required")

    # --- C13: row-gap inequality between adjacent rows.
    # Engine Phase 13k (_tighten_lower_rows_after_shrink) enforces this as a
    # GLOBAL row property regardless of column overlap: every section in
    # row N+1 sits at least `section_y_gap` below every section in row N
    # that ends at row N (i.e. row + row_span - 1 == N). Pairwise required
    # constraints reproduce this: each B in row N+1 has min_bbox_y ==
    # max_over_A_in_row_N(A.bbox_y + A.bbox_h) + gap.
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

    # --- C6: grid snap (weak), excluding stations on half-pitch or sparse-loop offsets.
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

    # --- C14: half-grid 2-branch symfan offsets.
    # For each symfan section, branch_a.y = trunk_y - pitch/2; branch_b.y = trunk_y + pitch/2.
    # Trunk_y is taken from the section's LR/RL port; falls back to the row trunk variable.
    for sec_id, (a, b) in c.symfan_sections.items():
        # Sort by engine Y to keep the assignment stable across runs
        if t.engine_station_y.get(a, 0) > t.engine_station_y.get(b, 0):
            a, b = b, a
        port_ids = set(t.section_entry_ports[sec_id]) | set(t.section_exit_ports[sec_id])
        anchor_var = None
        for pid in port_ids:
            if t.port_side.get(pid) in (PortSide.LEFT, PortSide.RIGHT) and pid in y_vars:
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

    # --- C15: fan-out symmetric offsets around trunk.
    # For "trunk_station_y" anchor: trunk is the full-bundle station in the column.
    # For "row_trunk_y" anchor: anchor is the row's shared trunk variable.
    for col_x, participants, anchor_kind in c.fanout_columns:
        if anchor_kind == "trunk_station_y":
            trunk = participants[0]
            others = participants[1:]
            others_sorted = sorted(others, key=lambda s: t.engine_station_y.get(s, 0.0))
            n = len(others_sorted)
            for i, sid in enumerate(others_sorted, start=1):
                if sid not in y_vars or trunk not in y_vars:
                    continue
                k = (i + 1) // 2
                sign = 1 if i % 2 == 1 else -1
                solver.addConstraint(
                    (y_vars[sid] == y_vars[trunk] + sign * k * y_spacing) | "medium"
                )
        elif anchor_kind == "row_trunk_y":
            sec_for_col = None
            for sec_id, sids in t.section_stations.items():
                if participants[0] in sids:
                    sec_for_col = sec_id
                    break
            if sec_for_col is None:
                continue
            row = t.section_grid_row.get(sec_for_col)
            anchor_var = row_trunk_vars.get(row)
            if anchor_var is None:
                continue
            participants_sorted = sorted(participants, key=lambda s: t.engine_station_y.get(s, 0.0))
            n = len(participants_sorted)
            if n % 2 == 0:
                offsets = list(range(-(n // 2), 0)) + list(range(1, n // 2 + 1))
            else:
                offsets = list(range(-(n // 2), n // 2 + 1))
            for sid, off in zip(participants_sorted, offsets):
                if sid in y_vars:
                    solver.addConstraint(
                        (y_vars[sid] == anchor_var + off * y_spacing) | "medium"
                    )

    # --- C16: sparse-loop full-pitch shift.
    # station.y == row_trunk_y + direction * y_spacing  (one row off the trunk)
    # The direction was captured at classification time from the engine hint;
    # in a pure-solver world we'd compute it from the topology (e.g. always
    # shift downward), but for the spike we honour the engine's sign so the
    # render-diff isolates the shift magnitude rather than direction flip.
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

    return Model(
        solver=solver,
        y_vars=y_vars,
        by_vars=by_vars,
        bh_vars=bh_vars,
        row_trunk_vars=row_trunk_vars,
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def solve(g: MetroGraph, y_spacing: Optional[float] = None) -> dict:
    """Solve `g`'s Y geometry via the constraint model. Mutates `g` in place.

    Approach: run the engine on `g` itself to set X coordinates, section
    bbox widths, port positions and topology metadata. Then override every
    Y value (station, port, junction, section bbox_y, section bbox_h)
    with the solver's choice. This isolates the solver to "what should the
    Y values be?" while reusing the imperative engine for X-axis layout
    (which item 6 of #351 explicitly leaves alone).
    """
    if y_spacing is None:
        y_spacing = compute_min_y_spacing(g)

    # Run the engine on g to populate X coords + bbox widths + ports.
    # Y values will be overwritten by the solver below.
    compute_layout(g, y_spacing=y_spacing, validate=False)

    t = harvest_topology(g, y_spacing)
    c = classify(t, g)
    m = build_model(g, t, c, y_spacing)
    m.solver.updateVariables()

    for sec_id, var in m.by_vars.items():
        g.sections[sec_id].bbox_y = var.value()
        g.sections[sec_id].bbox_h = m.bh_vars[sec_id].value()
    for sid, var in m.y_vars.items():
        if sid in g.stations:
            g.stations[sid].y = var.value()

    return {
        "y_spacing": y_spacing,
        "pitch": t.pitch,
        "origin": t.origin,
        "symfan_count": len(c.symfan_sections),
        "fanout_count": len(c.fanout_columns),
        "sparse_loop_count": len(c.sparse_loop_stations),
    }
