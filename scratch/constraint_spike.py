"""Constraint-solver spike for issue #345.

Goal: encode the row-Y alignment region of nf-metro's layout engine
(roughly Phases 8 / 9 / 10b-d / 11ca / 13c / 13e in the CONTRACT doc) as a
kiwisolver constraint system, then see how closely it reproduces engine
output on a couple of gallery fixtures.

This is a SPIKE.  Output goes in scratch/, not src/.  The deliverable is
docs/constraint-solver-spike.md with the land-or-shelve verdict.

How it works:

1. Parse a fixture and run the engine up through Phase 5 (port
   positioning).  At that point every section's bbox is in global
   coordinates and every port sits on a bbox edge at the section's
   nominal centre.  Stations are at their Phase-2 local Ys + section
   offset_y.  All later phases are exactly the row-Y alignment region
   the spike wants to replace.
2. Build a kiwisolver Solver with one Variable per station / port /
   section bbox_y.
3. Add the row-Y constraints (catalogued in docs/constraint-solver-spike.md).
4. Solve.  Compare the solver's Ys with what the full engine produced.
5. Print per-station deltas and a coarse verdict.

Two fixtures: genomeassembly.mmd (issue #208) and variantbenchmarking.mmd
(issues #221, #223).  These are the ones the hysteresis PRs explicitly
fixed; if a solver can reproduce the engine's Ys on them, it has a
fighting chance.  If not, we know why.
"""

from __future__ import annotations

import sys
from collections import defaultdict
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


def _load_graph(path: Path):
    text = path.read_text()
    g = parse_metro_mermaid(text)
    return g


@dataclass
class StationYSnapshot:
    """Y of every station / port / section after a full engine run."""

    station_y: dict[str, float]
    port_y: dict[str, float]
    section_bbox_y: dict[str, float]
    section_bbox_h: dict[str, float]
    y_spacing: float


def engine_snapshot(graph) -> StationYSnapshot:
    """Run the full engine on a deep-copy and snapshot Ys."""

    g = deepcopy(graph)
    y_spacing = compute_min_y_spacing(g)
    compute_layout(g, y_spacing=y_spacing, validate=False)

    return StationYSnapshot(
        station_y={
            sid: st.y
            for sid, st in g.stations.items()
            if not st.is_port and not sid.startswith("__")
        },
        port_y={pid: g.stations[pid].y for pid in g.ports if pid in g.stations},
        section_bbox_y={s.id: s.bbox_y for s in g.sections.values()},
        section_bbox_h={s.id: s.bbox_h for s in g.sections.values()},
        y_spacing=y_spacing,
    )


def initial_snapshot(graph) -> StationYSnapshot:
    """Snapshot after Phase 5 (ports on bbox edges) but before any row-Y phases.

    This is the state the solver is supposed to consume.  We get it by
    running the engine on a deep-copy with validate=False, then... ugh.
    There's no public entry point that stops at Phase 5.  For the spike,
    we'll just use the post-Phase-5 internals by monkey-patching: copy
    the graph, run only through Phase 5.  That's invasive enough that
    for the spike we'll instead use a cruder approach: run the full
    engine, but use the section bbox / port-side info as input
    "topology" and let the solver re-derive Ys from scratch given the
    layer/track assignment.
    """
    # Layer/track is set by Phase 2.  bbox_y/h are set by Phase 4 (after
    # section_placement).  We need both, so we run the full engine and
    # then "forget" the Ys.  Solver gets:
    #   - section bbox_y / bbox_h (canvas geometry)
    #   - section grid_row, grid_col, direction
    #   - station.layer, station.track (assignment)
    #   - edge topology
    #   - port sides
    # and must produce station Ys.
    g = deepcopy(graph)
    compute_layout(g, validate=False)
    return g


def _collect_row_groups(graph) -> dict[tuple[int, str], list[str]]:
    """Group sections by (row, direction), as Phase 2.5 / 9 do."""
    out = defaultdict(list)
    for s in graph.sections.values():
        if s.bbox_h <= 0 or s.grid_row < 0:
            continue
        out[(s.grid_row, s.direction)].append(s.id)
    return out


def build_constraint_model(graph_post_engine, y_spacing: float):
    """Build a kiwisolver model trying to reproduce the engine's Ys.

    Returns (solver, variables_by_id).  variables_by_id maps station and
    port IDs to kiwi.Variable.  Section bboxes are NOT variables here:
    bbox_y can shift due to top-align / off-track lift, but for the
    spike we treat bbox_y as input (final engine value) and only solve
    for station/port Ys.  This isolates the "where do stations go within
    a row?" question from the orthogonal "how tall does each bbox have
    to be?" question.

    Constraints encoded (numbered to match the catalogue in the docs):

    C1 (HARD): stations-in-section.  bbox_y + pad <= y <= bbox_y + h - pad
        for every internal (non-port) station.  pad ~= y_spacing/2.

    C2 (HARD): same-layer ordering.  Within a section, stations whose
        Phase-2 track-order is t_a < t_b must satisfy
        y_a + y_spacing <= y_b.  This linearises the disjunctive
        "no-overlap" constraint by using the engine's layer/track
        ordering as ground truth.

    C3 (SOFT, strong): edge straightness.  For every intra-section edge
        between non-port stations both at the same Phase-2 track,
        prefer y_source == y_target.  (Solves issue #209.)

    C4 (SOFT, strong): port aligns with neighbour station Y.  For each
        LEFT/RIGHT port with exactly one connected internal station,
        prefer port.y == station.y.

    C5 (SOFT, medium): inter-section straight bundle.  For each
        connected LEFT-port / RIGHT-port pair across sections, prefer
        equal Y.

    C6 (SOFT, weak): grid snap.  For each station, prefer
        y % y_spacing == 0 (rounded to the canvas).  Linearised as
        soft equality to the nearest grid slot of the original engine's
        Y, so the solver has a target to drift toward.

    C7 (HARD): port-on-bbox-edge.  For TOP / BOTTOM ports, y is fixed
        to bbox_y or bbox_y + h.  For LEFT / RIGHT ports, y is bounded
        within [bbox_y + pad, bbox_y + h - pad].

    Soft-constraint priorities are chosen with kiwisolver's strengths:
      - REQUIRED for hard constraints
      - STRONG for "must be straight" (C3, C4)
      - MEDIUM for cross-section bundle alignment (C5)
      - WEAK for grid snap (C6)
    """
    solver = kiwi.Solver()
    g = graph_post_engine

    vars_by_id: dict[str, kiwi.Variable] = {}

    pad = y_spacing / 2.0  # rough station_y_padding

    # --- Variables ---
    for sid, st in g.stations.items():
        if sid.startswith("__"):
            continue  # bypass / junction helpers
        v = kiwi.Variable(f"y_{sid}")
        vars_by_id[sid] = v

    # --- C1: stations-in-section ---
    for sec in g.sections.values():
        if sec.bbox_h <= 0:
            continue
        top = sec.bbox_y
        bot = sec.bbox_y + sec.bbox_h
        for sid in sec.station_ids:
            if sid not in vars_by_id:
                continue
            st = g.stations.get(sid)
            if st is None or st.is_port:
                # Ports handled in C7
                continue
            v = vars_by_id[sid]
            solver.addConstraint((v >= top + pad) | "required")
            solver.addConstraint((v <= bot - pad) | "required")

    # --- C2: same-layer ordering (linearise no-overlap) ---
    for sec in g.sections.values():
        # Group internal non-port stations by layer
        by_layer: dict[int, list[tuple[float, str]]] = defaultdict(list)
        for sid in sec.station_ids:
            st = g.stations.get(sid)
            if st is None or st.is_port:
                continue
            if sid.startswith("__"):
                continue
            layer = getattr(st, "layer", None)
            if layer is None:
                continue
            by_layer[layer].append((st.y, sid))

        for layer, items in by_layer.items():
            if len(items) < 2:
                continue
            items.sort()  # use engine's Y as ground-truth ordering
            for (_, a), (_, b) in zip(items, items[1:]):
                va = vars_by_id[a]
                vb = vars_by_id[b]
                solver.addConstraint((vb >= va + y_spacing) | "required")

    # --- C7: port-on-bbox-edge ---
    for pid, port in g.ports.items():
        if pid not in vars_by_id:
            continue
        sec = next((s for s in g.sections.values() if pid in s.station_ids), None)
        if sec is None:
            continue
        v = vars_by_id[pid]
        side = port.side.name if hasattr(port.side, "name") else str(port.side)
        if side == "TOP":
            solver.addConstraint((v == sec.bbox_y) | "required")
        elif side == "BOTTOM":
            solver.addConstraint((v == sec.bbox_y + sec.bbox_h) | "required")
        else:  # LEFT / RIGHT
            solver.addConstraint((v >= sec.bbox_y + pad) | "required")
            solver.addConstraint((v <= sec.bbox_y + sec.bbox_h - pad) | "required")

    # --- C3 + C5: edge straightness preferences ---
    # We treat all edges uniformly: prefer source.y == target.y, strong
    # for intra-section, medium for cross-section.
    for edge in g.edges:
        if edge.source not in vars_by_id or edge.target not in vars_by_id:
            continue
        vs = vars_by_id[edge.source]
        vt = vars_by_id[edge.target]
        src_sec = next(
            (s for s in g.sections.values() if edge.source in s.station_ids), None
        )
        tgt_sec = next(
            (s for s in g.sections.values() if edge.target in s.station_ids), None
        )
        strength = "strong" if src_sec is tgt_sec else "medium"
        solver.addConstraint((vs == vt) | strength)

    # --- C6: grid snap (weak) ---
    # Use the row's pitch info if available, else y_spacing.
    row_pitch: dict[int, float] = {}
    if hasattr(g, "_row_y_grid_info") and g._row_y_grid_info:
        for row, info in g._row_y_grid_info.items():
            row_pitch[row] = info.get("slot_spacing", y_spacing)

    for sec in g.sections.values():
        pitch = row_pitch.get(sec.grid_row, y_spacing)
        for sid in sec.station_ids:
            if sid not in vars_by_id:
                continue
            st = g.stations.get(sid)
            if st is None:
                continue
            # Find the nearest grid slot to the engine's Y, snap toward
            # it as a soft preference.
            origin = 0.0  # canvas origin
            target = origin + round((st.y - origin) / pitch) * pitch
            solver.addConstraint((vars_by_id[sid] == target) | "weak")

    return solver, vars_by_id


def solve_and_compare(fixture: Path) -> dict:
    graph = _load_graph(fixture)
    engine = engine_snapshot(graph)

    g_post = initial_snapshot(graph)
    solver, variables = build_constraint_model(g_post, engine.y_spacing)
    solver.updateVariables()

    deltas = []
    on_grid_solver = 0
    on_grid_engine = 0

    for sid, v in variables.items():
        if sid.startswith("__"):
            continue
        if sid in engine.station_y:
            engine_y = engine.station_y[sid]
            label = "station"
        elif sid in engine.port_y:
            engine_y = engine.port_y[sid]
            label = "port"
        else:
            continue
        solver_y = v.value()
        deltas.append((sid, label, engine_y, solver_y, solver_y - engine_y))

        pitch = engine.y_spacing
        if abs(round(solver_y / pitch) * pitch - solver_y) < 0.5:
            on_grid_solver += 1
        if abs(round(engine_y / pitch) * pitch - engine_y) < 0.5:
            on_grid_engine += 1

    n = len(deltas)
    exact = sum(1 for d in deltas if abs(d[4]) < 0.5)
    near = sum(1 for d in deltas if abs(d[4]) < 5.0)
    big = [d for d in deltas if abs(d[4]) >= 10.0]
    max_delta = max((abs(d[4]) for d in deltas), default=0.0)

    return {
        "fixture": fixture.name,
        "n_stations": n,
        "exact_match": exact,
        "near_match_5px": near,
        "big_deltas_10px": len(big),
        "max_delta_px": max_delta,
        "on_grid_solver": on_grid_solver,
        "on_grid_engine": on_grid_engine,
        "y_spacing": engine.y_spacing,
        "worst_examples": sorted(deltas, key=lambda d: -abs(d[4]))[:10],
    }


def main():
    for fx in FIXTURES:
        if not fx.exists():
            print(f"  SKIP: {fx} (not found)", file=sys.stderr)
            continue
        print(f"\n===== {fx.name} =====")
        try:
            r = solve_and_compare(fx)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
            continue
        print(
            f"  stations+ports:   {r['n_stations']:4d}    "
            f"y_spacing: {r['y_spacing']:.1f}"
        )
        print(f"  exact (<0.5px):   {r['exact_match']:4d}")
        print(f"  near  (<5px):     {r['near_match_5px']:4d}")
        print(f"  big   (>=10px):   {r['big_deltas_10px']:4d}")
        print(f"  max abs delta:    {r['max_delta_px']:.1f}px")
        print(
            f"  on-grid (solver): {r['on_grid_solver']:4d}    "
            f"on-grid (engine): {r['on_grid_engine']:4d}"
        )
        print("  worst 10:")
        for sid, label, ey, sy, d in r["worst_examples"]:
            print(
                f"    {label:7s} {sid:35s} engine={ey:7.1f}  solver={sy:7.1f}  d={d:+6.1f}"
            )


if __name__ == "__main__":
    main()
