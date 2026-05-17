"""Constraint-solver spike v2 for issue #345.

v1 (constraint_spike.py) held section bboxes fixed and got within 5px
on genomeassembly (24/26 stations) but missed badly on variantbenchmarking.
Inspection showed the misses came from:

  - the engine's row pitches vary per row (40 vs 47), so a single
    y_spacing-based grid snap is wrong;
  - the engine's grid origin is the mode of fractional residues across
    the row, not zero;
  - bbox_y itself shifts during the row-Y phases (Phase 9 top-align,
    Phase 13b compaction), so treating it as a constant decouples it
    from station Ys in a way the real engine never does.

v2 promotes section bbox_y to a kiwisolver variable and adds:

  - C8 (SOFT, strong): row-bbox-top equality.  Sections in the same
    contiguous column run share bbox_y (Phase 9 / Phase 13b).
  - C9 (SOFT, medium): row-trunk equality.  Trunk-carrying stations
    across same-row sections share Y (Phase 11ca).
  - C10 (HARD): off-track input above consumer.  off-track inputs sit
    `n * y_spacing` above their consumer (Phase 13).
  - C6': grid snap now uses the engine's per-row pitch and origin.

bbox_h is left as a constant (the engine grows it via _expand_bbox_for_y
in Phase 11 - encoding that interaction would explode the model and is
exactly the "no-go" the spike was supposed to evaluate).
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import kiwisolver as kiwi

from nf_metro.layout import compute_layout
from nf_metro.layout.engine import compute_min_y_spacing
from nf_metro.parser import parse_metro_mermaid

FIXTURES = [
    Path("examples/genomeassembly.mmd"),
    Path("examples/variantbenchmarking.mmd"),
]


@dataclass
class Snapshot:
    station_y: dict[str, float]
    port_y: dict[str, float]
    section_bbox_y: dict[str, float]
    section_bbox_h: dict[str, float]
    y_spacing: float


def engine_snapshot(graph) -> Snapshot:
    g = deepcopy(graph)
    ys = compute_min_y_spacing(g)
    compute_layout(g, y_spacing=ys, validate=False)
    return Snapshot(
        station_y={
            sid: st.y
            for sid, st in g.stations.items()
            if not st.is_port and not sid.startswith("__")
        },
        port_y={pid: g.stations[pid].y for pid in g.ports if pid in g.stations},
        section_bbox_y={s.id: s.bbox_y for s in g.sections.values()},
        section_bbox_h={s.id: s.bbox_h for s in g.sections.values()},
        y_spacing=ys,
    )


def _unified_pitch_origin(g, y_spacing: float) -> tuple[float, float]:
    """Single grid pitch + origin used everywhere.

    The current engine computes a per-row ``effective_y_spacing`` that
    can vary across rows when stations carry many lines (40 vs 47 in
    variantbenchmarking).  The spike adopts the design target of a
    unified grid: one pitch, one origin, derived from the engine's
    post-run output by:

      pitch  = max of all per-row pitches (or y_spacing if absent)
      origin = mode of (y % pitch) across all non-off-track non-port
               stations
    """
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
    for sec in g.sections.values():
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = g.stations.get(sid)
            if st is None or getattr(st, "off_track", False):
                continue
            residues.append(round(st.y % pitch, 3))
    origin = Counter(residues).most_common(1)[0][0] if residues else 0.0
    return pitch, origin


def _off_track_consumers(g) -> dict[str, tuple[str, int]]:
    """For each off-track station, return (consumer_id, stack_rank).

    Stack rank is 1 for the input nearest its consumer, 2 for the next,
    etc.  Matches the engine's _lift_off_track_stations contract.
    """
    out: dict[str, tuple[str, int]] = {}
    consumer_inputs: dict[str, list[str]] = defaultdict(list)
    for st in g.stations.values():
        if not getattr(st, "off_track", False):
            continue
        # find the unique downstream consumer
        succ = [e.target for e in g.edges if e.source == st.id]
        if len(succ) == 1:
            consumer_inputs[succ[0]].append(st.id)
    for cons, inputs in consumer_inputs.items():
        # Sort by engine's final Y descending (closest to consumer first)
        c_y = g.stations[cons].y
        inputs.sort(key=lambda i: abs(g.stations[i].y - c_y))
        for rank, inp in enumerate(inputs, start=1):
            out[inp] = (cons, rank)
    return out


def _row_contig_groups(g) -> list[list[str]]:
    by_row: dict[int, list] = defaultdict(list)
    for s in g.sections.values():
        if s.bbox_h <= 0 or s.grid_row < 0:
            continue
        by_row[s.grid_row].append(s)
    groups: list[list[str]] = []
    for sections in by_row.values():
        sections.sort(key=lambda s: s.grid_col)
        cur = [sections[0]]
        for s in sections[1:]:
            if s.grid_col - cur[-1].grid_col <= 1:
                cur.append(s)
            else:
                if len(cur) >= 2:
                    groups.append([s.id for s in cur])
                cur = [s]
        if len(cur) >= 2:
            groups.append([s.id for s in cur])
    return groups


def _section_trunk_station(g, section) -> str | None:
    """Pick a representative trunk station: full-bundle, LR-port-connected."""
    if section.direction not in ("LR", "RL"):
        return None
    port_ids = set(section.entry_ports) | set(section.exit_ports)
    internal = set(section.station_ids) - port_ids
    bundle = None
    for pid in port_ids:
        for edge in g.edges:
            other = (
                edge.target
                if edge.source == pid and edge.target in internal
                else edge.source
                if edge.target == pid and edge.source in internal
                else None
            )
            if other is None:
                continue
            lines = frozenset(g.station_lines(other))
            if bundle is None or len(lines) > len(bundle):
                bundle = lines
    if not bundle:
        return None
    for sid in internal:
        if frozenset(g.station_lines(sid)) == bundle and not sid.startswith("__"):
            return sid
    return None


def build_model(g, y_spacing: float):
    solver = kiwi.Solver()
    variables: dict[str, kiwi.Variable] = {}
    bbox_y_vars: dict[str, kiwi.Variable] = {}

    pad = y_spacing / 2.0

    for sid, st in g.stations.items():
        if sid.startswith("__"):
            continue
        variables[sid] = kiwi.Variable(f"y_{sid}")

    for sec_id in g.sections:
        bbox_y_vars[sec_id] = kiwi.Variable(f"by_{sec_id}")

    # Anchor bbox_y_vars to engine's input (soft, weak baseline so model
    # has a starting point but row-align can pull it).
    for sec in g.sections.values():
        solver.addConstraint((bbox_y_vars[sec.id] == sec.bbox_y) | "weak")

    # C1 + C7: containment with variable bbox_y
    for sec in g.sections.values():
        if sec.bbox_h <= 0:
            continue
        by = bbox_y_vars[sec.id]
        h = sec.bbox_h
        for sid in sec.station_ids:
            if sid not in variables:
                continue
            st = g.stations.get(sid)
            if st is None:
                continue
            v = variables[sid]
            if st.is_port:
                side = st.side if hasattr(st, "side") else None
                side_name = side.name if side and hasattr(side, "name") else str(side)
                port = g.ports.get(sid)
                if port:
                    side_name = port.side.name
                if side_name == "TOP":
                    solver.addConstraint((v == by) | "required")
                elif side_name == "BOTTOM":
                    solver.addConstraint((v == by + h) | "required")
                else:
                    solver.addConstraint((v >= by + pad) | "required")
                    solver.addConstraint((v <= by + h - pad) | "required")
            else:
                solver.addConstraint((v >= by + pad) | "required")
                solver.addConstraint((v <= by + h - pad) | "required")

    # C2: same-layer non-overlap (linearised by engine's ordering)
    for sec in g.sections.values():
        by_layer: dict[int, list[tuple[float, str]]] = defaultdict(list)
        for sid in sec.station_ids:
            st = g.stations.get(sid)
            if st is None or st.is_port or sid.startswith("__"):
                continue
            layer = getattr(st, "layer", None)
            if layer is None:
                continue
            by_layer[layer].append((st.y, sid))
        for items in by_layer.values():
            if len(items) < 2:
                continue
            items.sort()
            for (_, a), (_, b) in zip(items, items[1:]):
                solver.addConstraint(
                    (variables[b] >= variables[a] + y_spacing) | "required"
                )

    # C3 + C5: edge straightness
    for edge in g.edges:
        if edge.source not in variables or edge.target not in variables:
            continue
        src_sec = next(
            (s for s in g.sections.values() if edge.source in s.station_ids), None
        )
        tgt_sec = next(
            (s for s in g.sections.values() if edge.target in s.station_ids), None
        )
        strength = "strong" if src_sec is tgt_sec else "medium"
        solver.addConstraint(
            (variables[edge.source] == variables[edge.target]) | strength
        )

    # C8: same-row bbox_y equality (soft strong)
    for group in _row_contig_groups(g):
        for a, b in zip(group, group[1:]):
            solver.addConstraint((bbox_y_vars[a] == bbox_y_vars[b]) | "strong")

    # C9: row-trunk equality
    for group in _row_contig_groups(g):
        trunks = [_section_trunk_station(g, g.sections[sid]) for sid in group]
        trunks = [t for t in trunks if t and t in variables]
        for a, b in zip(trunks, trunks[1:]):
            solver.addConstraint((variables[a] == variables[b]) | "medium")

    # C10: off-track input above consumer
    for inp, (cons, rank) in _off_track_consumers(g).items():
        if inp not in variables or cons not in variables:
            continue
        solver.addConstraint(
            (variables[inp] == variables[cons] - rank * y_spacing) | "required"
        )

    # C6': grid snap using a UNIFIED pitch + origin across the whole graph
    pitch, origin = _unified_pitch_origin(g, y_spacing)
    for sec in g.sections.values():
        for sid in sec.station_ids:
            if sid not in variables:
                continue
            st = g.stations.get(sid)
            if st is None:
                continue
            target = origin + round((st.y - origin) / pitch) * pitch
            solver.addConstraint((variables[sid] == target) | "weak")

    return solver, variables, bbox_y_vars


def solve_and_compare(fixture: Path) -> dict:
    text = fixture.read_text()
    graph = parse_metro_mermaid(text)
    eng = engine_snapshot(graph)

    # Run engine once on a copy to get post-engine topology (bbox_h,
    # layer/track assignment, off-track flags).  The solver is then
    # given this state and asked to RE-solve the Ys.
    g_post = deepcopy(graph)
    compute_layout(g_post, validate=False)

    solver, variables, _bbox_y_vars = build_model(g_post, eng.y_spacing)
    solver.updateVariables()

    deltas: list = []
    for sid, v in variables.items():
        if sid.startswith("__"):
            continue
        if sid in eng.station_y:
            engine_y = eng.station_y[sid]
            label = "station"
        elif sid in eng.port_y:
            engine_y = eng.port_y[sid]
            label = "port"
        else:
            continue
        solver_y = v.value()
        deltas.append((sid, label, engine_y, solver_y, solver_y - engine_y))

    n = len(deltas)
    exact = sum(1 for d in deltas if abs(d[4]) < 0.5)
    near = sum(1 for d in deltas if abs(d[4]) < 5.0)
    big = sum(1 for d in deltas if abs(d[4]) >= 10.0)
    max_delta = max((abs(d[4]) for d in deltas), default=0.0)

    return {
        "fixture": fixture.name,
        "n": n,
        "exact": exact,
        "near5": near,
        "big10": big,
        "max": max_delta,
        "y_spacing": eng.y_spacing,
        "worst": sorted(deltas, key=lambda d: -abs(d[4]))[:10],
    }


def main():
    for fx in FIXTURES:
        if not fx.exists():
            print(f"SKIP {fx}", file=sys.stderr)
            continue
        print(f"\n===== {fx.name} =====")
        try:
            r = solve_and_compare(fx)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
            continue
        n = r["n"]
        print(
            f"  ports+stations: {n:4d}    "
            f"exact<0.5: {r['exact']:4d} ({100 * r['exact'] / n:.0f}%)    "
            f"near<5: {r['near5']:4d} ({100 * r['near5'] / n:.0f}%)    "
            f"big>=10: {r['big10']:4d}    max: {r['max']:.1f}px"
        )
        print("  worst 10:")
        for sid, label, ey, sy, d in r["worst"]:
            print(
                f"    {label:7s} {sid:35s} engine={ey:7.1f} solver={sy:7.1f} d={d:+6.1f}"
            )


if __name__ == "__main__":
    main()
