"""Constraint-solver feasibility spike for nf-metro layout.

Track F of the issue #323 fragility-reduction project. Time-boxed spike.

Encodes the layout problem for a small fixture as a constraint set and
solves it via kiwisolver (Cassowary simplex, LP-style) and scipy
least-squares. Compares output to nf_metro.layout.engine.compute_layout.

Run:
    PYTHONPATH=$PWD/src python scratch/solver_spike.py

The script writes a JSON comparison file alongside it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import kiwisolver as kiwi
import numpy as np
from scipy.optimize import minimize

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph, PortSide

# Layout parameters (must mirror engine defaults to make comparison meaningful)
X_SPACING = 60.0
Y_SPACING = 40.0
X_OFFSET = 30.0
Y_OFFSET = 30.0
SECTION_X_PADDING = 50.0
SECTION_Y_PADDING = 50.0
SECTION_X_GAP = 50.0
SECTION_Y_GAP = 50.0
STATION_HALF_W = 18.0  # rough marker half-width (pill stations vary)
STATION_HALF_H = 12.0


# --------------------------------------------------------------------------
# Section/layer/track precomputation
# --------------------------------------------------------------------------
# We mimic the pre-solver pipeline: take the parsed graph, infer the section
# DAG (col, row), do per-section longest-path layering (layer index per
# station), and per-section line-track ordering (track index per station).
# Then the solver assigns coordinates given those discrete indices.
#
# For the spike, we'll cheat and read layer/track from compute_layout's
# output - the spike's question is "given discrete topology indices, can a
# solver assign coordinates as well as the engine?" - not "can the solver
# replace the topology phases too?".


@dataclass
class SolverInput:
    """Topology indices + section grid, prepared for a coordinate solver."""

    sections: dict[str, dict] = field(default_factory=dict)
    # station_id -> {layer, track, section_id, is_port}
    stations: dict[str, dict] = field(default_factory=dict)
    # port_id -> {section_id, side, line_ids, is_entry, anchor_station_ids}
    ports: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)


def prepare(graph: MetroGraph) -> SolverInput:
    """Capture topology indices from a freshly-laid-out graph.

    We take section grid_col/grid_row, station layer/track, and port side
    from the engine output. The solver's job is then "given these discrete
    indices, produce coordinates".
    """
    compute_layout(graph, validate=False)  # populates layer/track/section grid

    out = SolverInput()
    for sid, s in graph.sections.items():
        # Count layers and tracks within section
        layers = []
        tracks = []
        for stid in s.station_ids:
            st = graph.stations[stid]
            layers.append(st.layer)
            tracks.append(st.track)
        max_layer = max(layers) if layers else 0
        max_track = max(tracks) if tracks else 0
        min_track = min(tracks) if tracks else 0
        out.sections[sid] = {
            "grid_col": s.grid_col,
            "grid_row": s.grid_row,
            "direction": s.direction,
            "station_ids": list(s.station_ids),
            "entry_ports": list(s.entry_ports),
            "exit_ports": list(s.exit_ports),
            "max_layer": max_layer,
            "max_track": max_track,
            "min_track": min_track,
            "n_layers": max_layer + 1,
            "n_tracks": int(max_track - min_track + 1),
        }
    for stid, st in graph.stations.items():
        out.stations[stid] = {
            "label": st.label,
            "layer": st.layer,
            "track": st.track,
            "section_id": st.section_id,
            "is_port": st.is_port,
        }
    for pid, p in graph.ports.items():
        out.ports[pid] = {
            "section_id": p.section_id,
            "side": p.side.value,
            "line_ids": list(p.line_ids),
            "is_entry": p.is_entry,
        }
    for e in graph.edges:
        out.edges.append({"source": e.source, "target": e.target, "line_id": e.line_id})
    return out


# --------------------------------------------------------------------------
# Kiwisolver formulation
# --------------------------------------------------------------------------
# Kiwisolver implements the Cassowary linear-arithmetic constraint solver.
# It supports linear equalities/inequalities with strengths (REQUIRED,
# STRONG, MEDIUM, WEAK). We use one variable per station x/y and per
# section bbox edge, plus stay constraints to bias toward grid positions.

def solve_kiwi(si: SolverInput) -> dict[str, dict]:
    s = kiwi.Solver()
    vars_x: dict[str, kiwi.Variable] = {}
    vars_y: dict[str, kiwi.Variable] = {}
    sec_x: dict[str, kiwi.Variable] = {}  # bbox left edge
    sec_y: dict[str, kiwi.Variable] = {}  # bbox top edge
    sec_w: dict[str, kiwi.Variable] = {}
    sec_h: dict[str, kiwi.Variable] = {}

    # 1) Section variables, ordered by grid_col / grid_row
    by_col: dict[int, list[str]] = {}
    by_row: dict[int, list[str]] = {}
    for sid, sec in si.sections.items():
        sec_x[sid] = kiwi.Variable(f"{sid}_x")
        sec_y[sid] = kiwi.Variable(f"{sid}_y")
        sec_w[sid] = kiwi.Variable(f"{sid}_w")
        sec_h[sid] = kiwi.Variable(f"{sid}_h")
        by_col.setdefault(sec["grid_col"], []).append(sid)
        by_row.setdefault(sec["grid_row"], []).append(sid)

    # 2) Section width / height from station counts (treat as fixed for the
    # spike: n_layers stations along flow axis, n_tracks across).
    for sid, sec in si.sections.items():
        if sec["direction"] == "LR":
            w = 2 * SECTION_X_PADDING + (sec["n_layers"] - 1) * X_SPACING + 2 * STATION_HALF_W
            h = 2 * SECTION_Y_PADDING + (sec["n_tracks"] - 1) * Y_SPACING + 2 * STATION_HALF_H
        else:  # TB
            w = 2 * SECTION_X_PADDING + (sec["n_tracks"] - 1) * X_SPACING + 2 * STATION_HALF_W
            h = 2 * SECTION_Y_PADDING + (sec["n_layers"] - 1) * Y_SPACING + 2 * STATION_HALF_H
        s.addConstraint((sec_w[sid] == w) | "required")
        s.addConstraint((sec_h[sid] == h) | "required")

    # 3) Per-column: all sections share left edge; columns separated by gap.
    cols = sorted(by_col.keys())
    col_lefts: dict[int, kiwi.Variable] = {}
    col_rights: dict[int, kiwi.Variable] = {}
    for col in cols:
        col_lefts[col] = kiwi.Variable(f"col{col}_left")
        col_rights[col] = kiwi.Variable(f"col{col}_right")
        for sid in by_col[col]:
            s.addConstraint((sec_x[sid] == col_lefts[col]) | "required")
            # col_right is max of (sec_x + sec_w) over column. We enforce
            # >= here, then push col_right down weakly to make it tight.
            s.addConstraint((col_rights[col] >= sec_x[sid] + sec_w[sid]) | "required")
        s.addConstraint((col_rights[col] >= col_lefts[col]) | "required")
        # Make col_right tight (minimize)
        s.addEditVariable(col_rights[col], "weak")
        s.suggestValue(col_rights[col], 0.0)

    # Anchor leftmost column
    s.addConstraint((col_lefts[cols[0]] == X_OFFSET) | "required")
    # Inter-column gap
    for a, b in zip(cols, cols[1:]):
        s.addConstraint((col_lefts[b] == col_rights[a] + SECTION_X_GAP) | "required")

    # 4) Per-row: top edge shared, rows separated by gap.
    rows = sorted(by_row.keys())
    row_tops: dict[int, kiwi.Variable] = {}
    row_bottoms: dict[int, kiwi.Variable] = {}
    for row in rows:
        row_tops[row] = kiwi.Variable(f"row{row}_top")
        row_bottoms[row] = kiwi.Variable(f"row{row}_bot")
        for sid in by_row[row]:
            s.addConstraint((sec_y[sid] == row_tops[row]) | "required")
            s.addConstraint((row_bottoms[row] >= sec_y[sid] + sec_h[sid]) | "required")
        s.addConstraint((row_bottoms[row] >= row_tops[row]) | "required")
        s.addEditVariable(row_bottoms[row], "weak")
        s.suggestValue(row_bottoms[row], 0.0)

    s.addConstraint((row_tops[rows[0]] == 70.0) | "required")  # engine y_offset+title
    for a, b in zip(rows, rows[1:]):
        s.addConstraint((row_tops[b] == row_bottoms[a] + SECTION_Y_GAP) | "required")

    # 5) Stations: x = section_left + padding + layer * x_spacing;
    #             y = section_top  + padding + track * y_spacing.
    # Engine convention: trunk track sits at section_top + h/2 for n_tracks=1.
    for stid, st in si.stations.items():
        if st["is_port"]:
            continue  # ports handled below
        sid = st["section_id"]
        if sid is None:
            continue  # bare junctions (no section)
        sec = si.sections[sid]
        vars_x[stid] = kiwi.Variable(f"{stid}_x")
        vars_y[stid] = kiwi.Variable(f"{stid}_y")
        if sec["direction"] == "LR":
            x_expr = sec_x[sid] + SECTION_X_PADDING + st["layer"] * X_SPACING
            # Center track at section middle; offset per track
            # Engine puts single-track stations at bbox vertical center.
            if sec["n_tracks"] == 1:
                y_expr = sec_y[sid] + sec_h[sid] / 2.0
            else:
                # Tracks start near top padding
                y_expr = sec_y[sid] + SECTION_Y_PADDING + (st["track"] - sec["min_track"]) * Y_SPACING
        else:
            y_expr = sec_y[sid] + SECTION_Y_PADDING + st["layer"] * Y_SPACING
            if sec["n_tracks"] == 1:
                x_expr = sec_x[sid] + sec_w[sid] / 2.0
            else:
                x_expr = sec_x[sid] + SECTION_X_PADDING + (st["track"] - sec["min_track"]) * X_SPACING
        s.addConstraint((vars_x[stid] == x_expr) | "required")
        s.addConstraint((vars_y[stid] == y_expr) | "required")

    # 6) Ports: anchored to section boundary on declared side.
    for pid, p in si.ports.items():
        sid = p["section_id"]
        sec = si.sections[sid]
        vars_x[pid] = kiwi.Variable(f"{pid}_x")
        vars_y[pid] = kiwi.Variable(f"{pid}_y")
        if p["side"] == "left":
            s.addConstraint((vars_x[pid] == sec_x[sid]) | "required")
            # Port Y: match dominant feeder/consumer Y (soft).
            # Default: section vertical center.
            s.addConstraint((vars_y[pid] == sec_y[sid] + sec_h[sid] / 2.0) | "strong")
        elif p["side"] == "right":
            s.addConstraint((vars_x[pid] == sec_x[sid] + sec_w[sid]) | "required")
            s.addConstraint((vars_y[pid] == sec_y[sid] + sec_h[sid] / 2.0) | "strong")
        elif p["side"] == "top":
            s.addConstraint((vars_y[pid] == sec_y[sid]) | "required")
            s.addConstraint((vars_x[pid] == sec_x[sid] + sec_w[sid] / 2.0) | "strong")
        elif p["side"] == "bottom":
            s.addConstraint((vars_y[pid] == sec_y[sid] + sec_h[sid]) | "required")
            s.addConstraint((vars_x[pid] == sec_x[sid] + sec_w[sid] / 2.0) | "strong")

    # 7) Bare junctions (section_id None): no section anchor. Position at
    # mid-channel between source-section right and target-section left.
    # For the spike, we accept the engine's placement implicitly by leaving
    # them unconstrained and using suggested values.
    for stid, st in si.stations.items():
        if not st["is_port"]:
            continue
        if st["section_id"] is not None:
            continue
        if stid in vars_x:
            continue
        vars_x[stid] = kiwi.Variable(f"{stid}_x")
        vars_y[stid] = kiwi.Variable(f"{stid}_y")
        # Find incoming and outgoing edges
        in_src = [e["source"] for e in si.edges if e["target"] == stid]
        out_tgt = [e["target"] for e in si.edges if e["source"] == stid]
        # Anchor X near upstream source-section's right edge + small offset.
        anchored = False
        for src_id in in_src:
            src = si.stations.get(src_id)
            if src and src.get("is_port") and src.get("section_id"):
                src_sec = src["section_id"]
                s.addConstraint(
                    (vars_x[stid] == sec_x[src_sec] + sec_w[src_sec] + 10.0) | "strong"
                )
                # Y matches source port Y (soft)
                if src_id in vars_y:
                    s.addConstraint((vars_y[stid] == vars_y[src_id]) | "strong")
                anchored = True
                break
        if not anchored:
            s.addConstraint((vars_y[stid] == 120.0) | "weak")
            s.addConstraint((vars_x[stid] == 200.0) | "weak")

    s.updateVariables()

    result: dict[str, dict] = {"sections": {}, "stations": {}}
    for sid in si.sections:
        result["sections"][sid] = {
            "bbox_x": sec_x[sid].value(),
            "bbox_y": sec_y[sid].value(),
            "bbox_w": sec_w[sid].value(),
            "bbox_h": sec_h[sid].value(),
        }
    for stid, v in vars_x.items():
        result["stations"][stid] = {"x": v.value(), "y": vars_y[stid].value()}
    return result


# --------------------------------------------------------------------------
# Scipy least-squares formulation
# --------------------------------------------------------------------------
# Treats every position as a free variable, sums weighted squared residuals.
# Equality "constraints" become high-weight residuals; soft preferences low.
# Reference for comparison; not a serious replacement.

def solve_scipy(si: SolverInput) -> dict[str, dict]:
    # Index variables
    sec_ids = list(si.sections.keys())
    n_sec = len(sec_ids)
    # 4 vars per section (x, y, w, h)
    var_offset_sec = {sid: 4 * i for i, sid in enumerate(sec_ids)}
    # Stations and ports: x, y per
    pos_ids = list(si.stations.keys())
    pos_offset = {sid: 4 * n_sec + 2 * i for i, sid in enumerate(pos_ids)}
    n_vars = 4 * n_sec + 2 * len(pos_ids)

    def residuals(v: np.ndarray) -> float:
        r = 0.0
        # Section dimensions fixed by topology
        for sid, sec in si.sections.items():
            o = var_offset_sec[sid]
            if sec["direction"] == "LR":
                w = 2 * SECTION_X_PADDING + (sec["n_layers"] - 1) * X_SPACING + 2 * STATION_HALF_W
                h = 2 * SECTION_Y_PADDING + (sec["n_tracks"] - 1) * Y_SPACING + 2 * STATION_HALF_H
            else:
                w = 2 * SECTION_X_PADDING + (sec["n_tracks"] - 1) * X_SPACING + 2 * STATION_HALF_W
                h = 2 * SECTION_Y_PADDING + (sec["n_layers"] - 1) * Y_SPACING + 2 * STATION_HALF_H
            r += 1e3 * (v[o + 2] - w) ** 2
            r += 1e3 * (v[o + 3] - h) ** 2

        # Column grouping
        by_col: dict[int, list[str]] = {}
        by_row: dict[int, list[str]] = {}
        for sid, sec in si.sections.items():
            by_col.setdefault(sec["grid_col"], []).append(sid)
            by_row.setdefault(sec["grid_row"], []).append(sid)
        cols = sorted(by_col.keys())
        rows = sorted(by_row.keys())

        # Anchor first column/row
        r += 1e3 * (v[var_offset_sec[by_col[cols[0]][0]]] - X_OFFSET) ** 2
        r += 1e3 * (v[var_offset_sec[by_row[rows[0]][0]] + 1] - 70.0) ** 2

        # Same-column sections share x
        for col, sids in by_col.items():
            ref_x = v[var_offset_sec[sids[0]]]
            for sid in sids[1:]:
                r += 1e3 * (v[var_offset_sec[sid]] - ref_x) ** 2

        # Same-row sections share y
        for row, sids in by_row.items():
            ref_y = v[var_offset_sec[sids[0]] + 1]
            for sid in sids[1:]:
                r += 1e3 * (v[var_offset_sec[sid] + 1] - ref_y) ** 2

        # Column gaps: max-right + gap = next-col-left
        for a, b in zip(cols, cols[1:]):
            max_right = max(
                v[var_offset_sec[sid]] + v[var_offset_sec[sid] + 2]
                for sid in by_col[a]
            )
            left_b = v[var_offset_sec[by_col[b][0]]]
            r += 1e3 * (left_b - max_right - SECTION_X_GAP) ** 2

        # Row gaps
        for a, b in zip(rows, rows[1:]):
            max_bot = max(
                v[var_offset_sec[sid] + 1] + v[var_offset_sec[sid] + 3]
                for sid in by_row[a]
            )
            top_b = v[var_offset_sec[by_row[b][0]] + 1]
            r += 1e3 * (top_b - max_bot - SECTION_Y_GAP) ** 2

        # Stations positioned on grid relative to section
        for stid, st in si.stations.items():
            if st["is_port"] or st["section_id"] is None:
                continue
            sid = st["section_id"]
            sec = si.sections[sid]
            o = var_offset_sec[sid]
            sx, sy, sw, sh = v[o], v[o + 1], v[o + 2], v[o + 3]
            po = pos_offset[stid]
            px, py = v[po], v[po + 1]
            if sec["direction"] == "LR":
                target_x = sx + SECTION_X_PADDING + st["layer"] * X_SPACING
                if sec["n_tracks"] == 1:
                    target_y = sy + sh / 2.0
                else:
                    target_y = sy + SECTION_Y_PADDING + (st["track"] - sec["min_track"]) * Y_SPACING
            else:
                target_y = sy + SECTION_Y_PADDING + st["layer"] * Y_SPACING
                if sec["n_tracks"] == 1:
                    target_x = sx + sw / 2.0
                else:
                    target_x = sx + SECTION_X_PADDING + (st["track"] - sec["min_track"]) * X_SPACING
            r += 1e3 * (px - target_x) ** 2
            r += 1e3 * (py - target_y) ** 2

        # Ports anchored to section boundary
        for pid, p in si.ports.items():
            sid = p["section_id"]
            sec = si.sections[sid]
            o = var_offset_sec[sid]
            sx, sy, sw, sh = v[o], v[o + 1], v[o + 2], v[o + 3]
            po = pos_offset[pid]
            px, py = v[po], v[po + 1]
            if p["side"] == "left":
                r += 1e3 * (px - sx) ** 2
                r += 10 * (py - (sy + sh / 2.0)) ** 2
            elif p["side"] == "right":
                r += 1e3 * (px - (sx + sw)) ** 2
                r += 10 * (py - (sy + sh / 2.0)) ** 2
            elif p["side"] == "top":
                r += 1e3 * (py - sy) ** 2
                r += 10 * (px - (sx + sw / 2.0)) ** 2
            elif p["side"] == "bottom":
                r += 1e3 * (py - (sy + sh)) ** 2
                r += 10 * (px - (sx + sw / 2.0)) ** 2
        return r

    x0 = np.zeros(n_vars)
    # Warm-start: seed section x by column, y by row
    sec_xs_by_col = {}
    cur_x = X_OFFSET
    for col in sorted({sec["grid_col"] for sec in si.sections.values()}):
        sec_xs_by_col[col] = cur_x
        cur_x += 250.0
    cur_y = 70.0
    sec_ys_by_row = {}
    for row in sorted({sec["grid_row"] for sec in si.sections.values()}):
        sec_ys_by_row[row] = cur_y
        cur_y += 150.0
    for sid, sec in si.sections.items():
        o = var_offset_sec[sid]
        x0[o] = sec_xs_by_col[sec["grid_col"]]
        x0[o + 1] = sec_ys_by_row[sec["grid_row"]]
        x0[o + 2] = 200.0
        x0[o + 3] = 100.0
    for pid, p in si.ports.items():
        po = pos_offset[pid]
        sec = si.sections[p["section_id"]]
        x0[po] = sec_xs_by_col[sec["grid_col"]]
        x0[po + 1] = sec_ys_by_row[sec["grid_row"]] + 50
    for stid, st in si.stations.items():
        if st["is_port"]:
            continue
        po = pos_offset[stid]
        sid = st.get("section_id")
        if sid is None:
            x0[po] = 200
            x0[po + 1] = 120
            continue
        sec = si.sections[sid]
        x0[po] = sec_xs_by_col[sec["grid_col"]] + 50 + st["layer"] * X_SPACING
        x0[po + 1] = sec_ys_by_row[sec["grid_row"]] + 50

    res = minimize(residuals, x0, method="L-BFGS-B", options={"maxiter": 5000, "ftol": 1e-10})

    result: dict[str, dict] = {
        "sections": {},
        "stations": {},
        "scipy_final_loss": float(res.fun),
        "scipy_iters": int(res.nit),
    }
    for sid in si.sections:
        o = var_offset_sec[sid]
        result["sections"][sid] = {
            "bbox_x": float(res.x[o]),
            "bbox_y": float(res.x[o + 1]),
            "bbox_w": float(res.x[o + 2]),
            "bbox_h": float(res.x[o + 3]),
        }
    for stid in pos_offset:
        po = pos_offset[stid]
        result["stations"][stid] = {"x": float(res.x[po]), "y": float(res.x[po + 1])}
    return result


# --------------------------------------------------------------------------
# Comparison driver
# --------------------------------------------------------------------------
def run_fixture(path: Path) -> dict:
    text = path.read_text()

    # Engine baseline
    g1 = parse_metro_mermaid(text)
    si = prepare(g1)  # also runs compute_layout to fill layer/track
    engine = {
        "sections": {
            sid: {
                "bbox_x": s.bbox_x,
                "bbox_y": s.bbox_y,
                "bbox_w": s.bbox_w,
                "bbox_h": s.bbox_h,
            }
            for sid, s in g1.sections.items()
        },
        "stations": {sid: {"x": st.x, "y": st.y} for sid, st in g1.stations.items()},
    }

    kiwi_result = solve_kiwi(si)
    scipy_result = solve_scipy(si)

    def diff(a: dict, b: dict, keys: list[str]) -> dict:
        out = {}
        for k in keys:
            out[k] = abs(a.get(k, 0.0) - b.get(k, 0.0))
        return out

    comparison = {
        "fixture": str(path),
        "engine": engine,
        "kiwi": kiwi_result,
        "scipy": {k: v for k, v in scipy_result.items() if k not in {"scipy_final_loss", "scipy_iters"}},
        "scipy_final_loss": scipy_result.get("scipy_final_loss"),
        "scipy_iters": scipy_result.get("scipy_iters"),
        "section_diffs_kiwi": {},
        "station_diffs_kiwi": {},
        "section_diffs_scipy": {},
        "station_diffs_scipy": {},
    }
    for sid in engine["sections"]:
        if sid in kiwi_result["sections"]:
            comparison["section_diffs_kiwi"][sid] = diff(
                engine["sections"][sid],
                kiwi_result["sections"][sid],
                ["bbox_x", "bbox_y", "bbox_w", "bbox_h"],
            )
        if sid in scipy_result["sections"]:
            comparison["section_diffs_scipy"][sid] = diff(
                engine["sections"][sid],
                scipy_result["sections"][sid],
                ["bbox_x", "bbox_y", "bbox_w", "bbox_h"],
            )
    for stid in engine["stations"]:
        if stid in kiwi_result["stations"]:
            comparison["station_diffs_kiwi"][stid] = diff(
                engine["stations"][stid],
                kiwi_result["stations"][stid],
                ["x", "y"],
            )
        if stid in scipy_result["stations"]:
            comparison["station_diffs_scipy"][stid] = diff(
                engine["stations"][stid],
                scipy_result["stations"][stid],
                ["x", "y"],
            )

    # Summaries
    def summarise(diffs: dict) -> dict:
        all_vals = []
        for d in diffs.values():
            all_vals.extend(d.values())
        if not all_vals:
            return {"n": 0}
        return {
            "n": len(all_vals),
            "max": max(all_vals),
            "mean": sum(all_vals) / len(all_vals),
            "exact_match_pct": 100.0 * sum(1 for v in all_vals if v < 0.5) / len(all_vals),
        }

    comparison["summary"] = {
        "kiwi_sections": summarise(comparison["section_diffs_kiwi"]),
        "kiwi_stations": summarise(comparison["station_diffs_kiwi"]),
        "scipy_sections": summarise(comparison["section_diffs_scipy"]),
        "scipy_stations": summarise(comparison["station_diffs_scipy"]),
    }
    return comparison


if __name__ == "__main__":
    here = Path(__file__).parent
    repo_root = here.parent

    fixtures = [
        repo_root / "examples" / "topologies" / "single_section.mmd",
        repo_root / "examples" / "topologies" / "section_diamond.mmd",
    ]

    results = []
    for fx in fixtures:
        print(f"\n=== {fx.name} ===")
        try:
            r = run_fixture(fx)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            results.append({"fixture": str(fx), "error": f"{type(e).__name__}: {e}"})
            continue
        sm = r["summary"]
        print(f"  kiwi sections  : {sm['kiwi_sections']}")
        print(f"  kiwi stations  : {sm['kiwi_stations']}")
        print(f"  scipy sections : {sm['scipy_sections']}")
        print(f"  scipy stations : {sm['scipy_stations']}")
        print(f"  scipy final_loss = {r['scipy_final_loss']:.4g}, iters = {r['scipy_iters']}")
        results.append(r)

    out_path = here / "solver_spike_results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {out_path}")
