#!/usr/bin/env python3
"""Build the nf-metro layout-invariant triage HTML page.

For each (fixture, invariant) pair flagged by the failing/xfailing layout
invariants in ``tests/test_layout_invariants.py``:

1. Renders the fixture to SVG (cached per fixture).
2. Runs the layout engine and the invariant's geometric check to extract
   the violating coordinate(s).
3. Produces an SVG containing the base render plus a red bbox overlay
   for the offending element(s).
4. Embeds everything into a single self-contained HTML page with
   localStorage triage state (bug / not-a-bug / ambiguous + notes).

CLI:

    python build_review.py \\
        --worktree /path/to/nf-metro \\
        --output-dir /path/to/triage-output \\
        [--fail-list /path/to/pytest-output.log]

If ``--fail-list`` is omitted, the script runs pytest in
``--collect-only`` / ``-rfX`` mode against
``tests/test_layout_invariants.py`` inside ``--worktree`` and parses
the FAILED / XFAIL lines itself.

Two escape hatches let the tool triage *ad-hoc* checks that aren't
committed invariants yet, without touching the test suite or this file:

* ``--violations <file.json>`` ingests pre-computed violations of shape
  ``[{fixture, invariant, rects:[{x,y,w,h,note}], issue, check}]`` and
  renders cards straight from them, bypassing pytest discovery and the
  ``INVARIANT_FINDERS`` registry entirely. Per-entry ``issue`` / ``check``
  strings become the explanation block (falling back to the built-in
  generic block keyed by ``invariant`` if omitted).
* ``--finder-module <path-or-dotted-name>`` registers extra finders and
  explanations at runtime. The module may expose a ``FINDERS`` dict
  (``{invariant: callable(graph, engine) -> list[violator-dict]}``) and/or
  an ``EXPLANATIONS`` dict (``{invariant: (issue_html, check_html)}``)
  that are merged over the built-in registries.

Invariants with no tailored explanation fall back to the generic block;
ad-hoc checks rely on that path.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import traceback
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Build a self-contained HTML page for triaging failing/xfailing "
            "layout invariants in nf-metro."
        ),
    )
    p.add_argument(
        "--worktree",
        type=Path,
        required=True,
        help="Path to the nf-metro checkout (worktree) whose layout engine "
        "and fixtures should be used.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write index.html plus renders/ and renders/annotated/.",
    )
    p.add_argument(
        "--fail-list",
        type=Path,
        default=None,
        help="Optional path to a pytest output log containing FAILED/XFAIL "
        "lines for tests/test_layout_invariants.py. If omitted, pytest is "
        "invoked automatically.",
    )
    p.add_argument(
        "--pytest-target",
        default="tests/test_layout_invariants.py",
        help="Pytest target (relative to worktree) used when --fail-list "
        "is not given. Default: tests/test_layout_invariants.py.",
    )
    p.add_argument(
        "--violations",
        type=Path,
        default=None,
        help="Optional path to a JSON file of pre-computed violations of "
        "shape [{fixture, invariant, rects:[{x,y,w,h,note}], issue, check}]. "
        "When given, cards are built directly from this file, bypassing "
        "pytest discovery and the INVARIANT_FINDERS registry. Useful for "
        "triaging an ad-hoc / one-off check without adding a committed test.",
    )
    p.add_argument(
        "--finder-module",
        default=None,
        help="Optional Python module (dotted name or path to a .py file) "
        "exposing a FINDERS dict {invariant: callable(graph, engine)} and/or "
        "an EXPLANATIONS dict {invariant: (issue_html, check_html)}. These "
        "are merged over the built-in registries so a one-off finder can be "
        "triaged without editing this script.",
    )
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Engine glue (worktree-specific imports loaded after sys.path is set up)
# ---------------------------------------------------------------------------


def _setup_imports(worktree: Path) -> dict:
    """Insert ``worktree/src`` and ``worktree/tests`` on sys.path and import
    the engine symbols we need. Returns a dict of the symbols by name so
    the rest of the module doesn't have to do delayed-import dances."""
    src = worktree / "src"
    tests = worktree / "tests"
    if not src.is_dir():
        raise SystemExit(f"--worktree {worktree} has no src/ directory")
    sys.path.insert(0, str(src))
    sys.path.insert(0, str(tests))

    # Late imports so the worktree's source tree is authoritative.
    from nf_metro.layout.engine import (  # noqa: E402
        _station_marker_bbox,
        compute_layout,
    )
    from nf_metro.layout.labels import place_labels  # noqa: E402
    from nf_metro.layout.routing import (  # noqa: E402
        compute_station_offsets,
        route_edges,
    )
    from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402
    from nf_metro.parser.model import PortSide  # noqa: E402
    from nf_metro.render.svg import apply_route_offsets  # noqa: E402

    return {
        "compute_layout": compute_layout,
        "_station_marker_bbox": _station_marker_bbox,
        "place_labels": place_labels,
        "compute_station_offsets": compute_station_offsets,
        "route_edges": route_edges,
        "parse_metro_mermaid": parse_metro_mermaid,
        "PortSide": PortSide,
        "apply_route_offsets": apply_route_offsets,
    }


_Y_TOL = 1.0
_LABEL_DRIFT_TOL = 10.0


# ---------------------------------------------------------------------------
# Runtime finder-module loading
# ---------------------------------------------------------------------------


def load_finder_module(spec: str):
    """Import a finder module given either a dotted name or a path to a .py
    file, and return it. The module may expose ``FINDERS`` and/or
    ``EXPLANATIONS`` dicts (both optional)."""
    path = Path(spec)
    if path.suffix == ".py" or path.exists():
        path = path.resolve()
        if not path.is_file():
            raise SystemExit(f"--finder-module {spec} is not a file")
        mod_spec = importlib.util.spec_from_file_location(path.stem, path)
        if mod_spec is None or mod_spec.loader is None:
            raise SystemExit(f"--finder-module {spec} could not be loaded")
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)
        return module
    return importlib.import_module(spec)


def merge_finder_module(module, finders: dict, explanations: dict) -> None:
    """Merge a finder module's ``FINDERS`` / ``EXPLANATIONS`` over the given
    registries (mutating them in place). Either attribute may be absent."""
    extra_finders = getattr(module, "FINDERS", None)
    if extra_finders:
        if not isinstance(extra_finders, dict):
            raise SystemExit("finder-module FINDERS must be a dict")
        finders.update(extra_finders)
    extra_explanations = getattr(module, "EXPLANATIONS", None)
    if extra_explanations:
        if not isinstance(extra_explanations, dict):
            raise SystemExit("finder-module EXPLANATIONS must be a dict")
        explanations.update(extra_explanations)


# ---------------------------------------------------------------------------
# Fixture resolution
# ---------------------------------------------------------------------------


def make_resolve_fixture(worktree: Path):
    fixtures_dir = worktree / "tests" / "fixtures"
    examples_dir = worktree / "examples"

    def resolve(name: str) -> Path:
        p = Path(name)
        candidates = [
            fixtures_dir / p,
            examples_dir / p,
            examples_dir / "topologies" / p,
            examples_dir / "guide" / p,
            fixtures_dir / "topologies" / p,
            worktree / p,
        ]
        for c in candidates:
            if c.is_file():
                return c
        raise FileNotFoundError(name)

    return resolve, fixtures_dir


def make_load_layout(engine, resolve_fixture, fixtures_dir: Path):
    def load(fixture: str):
        path = resolve_fixture(fixture)
        text = path.read_text()
        graph = engine["parse_metro_mermaid"](text)
        if path.is_relative_to(fixtures_dir):
            graph.center_ports = True
        engine["compute_layout"](graph)
        return graph

    return load


def make_render_svg(resolve_fixture, renders_dir: Path):
    def render(fixture: str) -> tuple[Path | None, str | None]:
        safe = fixture.replace("/", "__")
        out = renders_dir / f"{safe}.svg"
        if out.exists():
            return out, None
        path = resolve_fixture(fixture)
        try:
            proc = subprocess.run(
                ["nf-metro", "render", str(path), "-o", str(out)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return None, f"render failed: {proc.stderr.strip()[:500]}"
        except Exception as e:  # noqa: BLE001
            return None, f"render exception: {e!r}"
        return (out, None) if out.exists() else (None, "render produced no output")

    return render


# ---------------------------------------------------------------------------
# Helpers shared with test_layout_invariants.py
# ---------------------------------------------------------------------------


def _row_lr_sections(graph) -> dict[int, list]:
    rows: dict[int, list] = defaultdict(list)
    for sec in graph.sections.values():
        if (
            sec.bbox_h <= 0
            or sec.grid_row < 0
            or sec.direction not in ("LR", "RL")
            or sec.grid_row_span > 1
        ):
            continue
        rows[sec.grid_row].append(sec)
    return rows


def _section_lr_port_ys(graph, section, PortSide) -> list[float]:
    ys: list[float] = []
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        st = graph.stations.get(pid)
        if (
            port is not None
            and st is not None
            and port.side in (PortSide.LEFT, PortSide.RIGHT)
        ):
            ys.append(st.y)
    return ys


def _section_full_bundle(graph, section, PortSide) -> set[str] | None:
    port_lines: set[str] = set()
    has_lr_port = False
    for pid in list(section.entry_ports) + list(section.exit_ports):
        port = graph.ports.get(pid)
        if port is None or port.side not in (PortSide.LEFT, PortSide.RIGHT):
            continue
        has_lr_port = True
        port_lines.update(graph.station_lines(pid))
    return port_lines if (has_lr_port and port_lines) else None


def _section_trunk_marker_cy(graph, section, offsets, PortSide):
    port_ys = _section_lr_port_ys(graph, section, PortSide)
    if not port_ys:
        return None
    port_y = port_ys[0]
    bundle = _section_full_bundle(graph, section, PortSide)
    if not bundle:
        return None
    port_set = set(section.entry_ports) | set(section.exit_ports)
    best = None
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden:
            continue
        lines = graph.station_lines(sid)
        if set(lines) != bundle:
            continue
        line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
        if not line_offs:
            continue
        cy = st.y + (min(line_offs) + max(line_offs)) / 2
        dist = abs(cy - port_y)
        if best is None or dist < best[0]:
            best = (dist, cy, sid, st.x)
    return best  # (dist, cy, sid, st.x) or None


# ---------------------------------------------------------------------------
# Per-invariant violator detection
# ---------------------------------------------------------------------------


def find_row_trunk_violators(graph, engine) -> list[dict]:
    PortSide = engine["PortSide"]
    offsets = engine["compute_station_offsets"](graph)
    rows = _row_lr_sections(graph)
    violators: list[dict] = []
    for row, sections in rows.items():
        cys = []
        for sec in sections:
            v = _section_trunk_marker_cy(graph, sec, offsets, PortSide)
            if v is not None:
                cys.append((sec, v))
        if len(cys) < 2:
            continue
        anchor_sec_id = cys[0][0].id
        target = cys[0][1][1]
        for sec, v in cys[1:]:
            if abs(v[1] - target) >= _Y_TOL:
                _, cy, sid, sx = v
                violators.append(
                    {
                        "kind": "marker",
                        "x": sx,
                        "y": cy,
                        "w": 24,
                        "h": 24,
                        "note": (
                            f"row {row}: trunk {sid} cy={cy:.1f} drifts "
                            f"from row anchor cy={target:.1f} "
                            f"(delta={cy - target:+.1f}px)"
                        ),
                        "row_trunk_info": {
                            "row": row,
                            "section_id": sec.id,
                            "trunk_station": sid,
                            "section_cy": cy,
                            "anchor_section_id": anchor_sec_id,
                            "anchor_cy": target,
                            "delta": cy - target,
                        },
                    }
                )
    return violators


def find_no_kink_violators(graph, engine) -> list[dict]:
    PortSide = engine["PortSide"]
    offsets = engine["compute_station_offsets"](graph)
    rows = _row_lr_sections(graph)
    violators = []
    for row, sections in rows.items():
        sorted_secs = sorted(sections, key=lambda s: s.grid_col)
        for sec, nxt in zip(sorted_secs, sorted_secs[1:]):
            if nxt.grid_col - sec.grid_col != 1:
                continue
            for pid in sec.exit_ports:
                port = graph.ports.get(pid)
                if port is None or port.side != PortSide.RIGHT:
                    continue
                exit_lines = graph.station_lines(pid)
                if not exit_lines:
                    continue
                exit_offs = [offsets.get((pid, lid), 0.0) for lid in exit_lines]
                exit_cy = graph.stations[pid].y + (min(exit_offs) + max(exit_offs)) / 2
                exit_x = graph.stations[pid].x
                for npid in nxt.entry_ports:
                    nport = graph.ports.get(npid)
                    if nport is None or nport.side != PortSide.LEFT:
                        continue
                    entry_lines = graph.station_lines(npid)
                    entry_offs = [offsets.get((npid, lid), 0.0) for lid in entry_lines]
                    entry_cy = (
                        graph.stations[npid].y + (min(entry_offs) + max(entry_offs)) / 2
                    )
                    entry_x = graph.stations[npid].x
                    if abs(exit_cy - entry_cy) >= _Y_TOL:
                        lo_x = min(exit_x, entry_x) - 12
                        hi_x = max(exit_x, entry_x) + 12
                        lo_y = min(exit_cy, entry_cy) - 12
                        hi_y = max(exit_cy, entry_cy) + 12
                        violators.append(
                            {
                                "kind": "rect",
                                "x": lo_x,
                                "y": lo_y,
                                "w": hi_x - lo_x,
                                "h": hi_y - lo_y,
                                "note": (
                                    f"row {row}: exit port {pid} cy={exit_cy:.1f} "
                                    f"!= entry port {npid} cy={entry_cy:.1f} "
                                    f"(delta={exit_cy - entry_cy:+.1f}px)"
                                ),
                                "kink_info": {
                                    "row": row,
                                    "sec_a": sec.id,
                                    "sec_b": nxt.grid_col,
                                    "sec_a_id": sec.id,
                                    "sec_b_id": nxt.id,
                                    "exit_port": pid,
                                    "entry_port": npid,
                                    "exit_cy": exit_cy,
                                    "entry_cy": entry_cy,
                                    "delta": exit_cy - entry_cy,
                                },
                            }
                        )
    return violators


def _section_fan_columns(graph, section, PortSide) -> dict[float, list[str]]:
    bundle = _section_full_bundle(graph, section, PortSide)
    if not bundle:
        return {}
    port_set = set(section.entry_ports) | set(section.exit_ports)
    cols: dict[float, list[str]] = defaultdict(list)
    for sid in section.station_ids:
        if sid in port_set:
            continue
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.is_hidden or st.off_track:
            continue
        if set(graph.station_lines(sid)) != bundle:
            continue
        cols[round(st.x, 1)].append(sid)
    return {x: sids for x, sids in cols.items() if len(sids) >= 2}


def find_symfan_violators(graph, engine) -> list[dict]:
    PortSide = engine["PortSide"]
    offsets = engine["compute_station_offsets"](graph)
    violators = []
    for sec in graph.sections.values():
        cols = _section_fan_columns(graph, sec, PortSide)
        trunk = _section_trunk_marker_cy(graph, sec, offsets, PortSide)
        if trunk is None:
            continue
        trunk_cy = trunk[1]
        for x, sids in cols.items():
            if len(sids) != 2:
                continue
            cys = []
            for sid in sids:
                st = graph.stations[sid]
                lines = graph.station_lines(sid)
                line_offs = [offsets.get((sid, lid), 0.0) for lid in lines]
                cys.append((sid, st.y + (min(line_offs) + max(line_offs)) / 2))
            cys.sort(key=lambda t: t[1])
            above_gap = trunk_cy - cys[0][1]
            below_gap = cys[1][1] - trunk_cy
            if abs(above_gap - below_gap) >= _Y_TOL:
                top_sid, top_cy = cys[0]
                bot_sid, bot_cy = cys[1]
                lo_y = min(top_cy, bot_cy) - 16
                hi_y = max(top_cy, bot_cy) + 16
                source = None
                if sec.entry_ports:
                    source = next(iter(sec.entry_ports))
                violators.append(
                    {
                        "kind": "rect",
                        "x": x - 18,
                        "y": lo_y,
                        "w": 36,
                        "h": hi_y - lo_y,
                        "note": (
                            f"section {sec.id} col x={x}: pair "
                            f"({top_sid} cy={top_cy:.1f}, {bot_sid} cy={bot_cy:.1f}) "
                            f"not mirrored around trunk cy={trunk_cy:.1f} "
                            f"(above_gap={above_gap:.1f}, below_gap={below_gap:.1f})"
                        ),
                        "symfan_info": {
                            "section_id": sec.id,
                            "source": source or sec.id,
                            "top_sid": top_sid,
                            "bot_sid": bot_sid,
                            "top_cy": top_cy,
                            "bot_cy": bot_cy,
                            "trunk_cy": trunk_cy,
                            "delta": (above_gap - below_gap),
                        },
                    }
                )
    return violators


def find_breeze_past_violators(graph, engine) -> list[dict]:
    offsets = engine["compute_station_offsets"](graph)
    routes = engine["route_edges"](graph, station_offsets=offsets)
    consumed_by = defaultdict(set)
    produced_by = defaultdict(set)
    for e in graph.edges:
        consumed_by[e.target].add(e.line_id)
        produced_by[e.source].add(e.line_id)

    def seg_crosses_bbox(p1, p2, bbox):
        x1, y1 = p1
        x2, y2 = p2
        bx1, by1, bx2, by2 = bbox
        if max(x1, x2) < bx1 or min(x1, x2) > bx2:
            return False
        if max(y1, y2) < by1 or min(y1, y2) > by2:
            return False
        for k in range(21):
            f = k / 20.0
            x = x1 + f * (x2 - x1)
            y = y1 + f * (y2 - y1)
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                return True
        return False

    seen = set()
    violators = []
    for sid, _st in graph.stations.items():
        bbox = engine["_station_marker_bbox"](graph, sid, offsets=offsets)
        if bbox is None:
            continue
        station_lines = consumed_by.get(sid, set()) | produced_by.get(sid, set())
        for r in routes:
            if r.line_id in station_lines:
                continue
            if r.edge.source == sid or r.edge.target == sid:
                continue
            pts = engine["apply_route_offsets"](r, offsets)
            for k in range(len(pts) - 1):
                if seg_crosses_bbox(pts[k], pts[k + 1], bbox):
                    key = (sid,)
                    if key in seen:
                        break
                    seen.add(key)
                    bx1, by1, bx2, by2 = bbox
                    violators.append(
                        {
                            "kind": "rect",
                            "x": bx1 - 4,
                            "y": by1 - 4,
                            "w": (bx2 - bx1) + 8,
                            "h": (by2 - by1) + 8,
                            "note": (
                                f"line {r.line_id!r} on edge "
                                f"{r.edge.source}->{r.edge.target} "
                                f"crosses non-consumer marker {sid!r}"
                            ),
                            "breeze_info": {
                                "line_id": r.line_id,
                                "edge_source": r.edge.source,
                                "edge_target": r.edge.target,
                                "station": sid,
                            },
                        }
                    )
                    break
    return violators


def find_bbox_padding_violators(graph, engine) -> list[dict]:
    """test_section_bbox_has_bottom_padding."""
    from nf_metro.layout.constants import SECTION_Y_PADDING

    tol = 1.0
    violators = []
    for sec_id, sec in graph.sections.items():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        internal = [
            (sid, graph.stations[sid].y)
            for sid in sec.station_ids
            if sid in graph.stations
            and sid not in port_ids
            and not graph.stations[sid].is_hidden
        ]
        if not internal:
            continue
        lowest_sid, lowest_cy = max(internal, key=lambda t: t[1])
        bbox_bot = sec.bbox_y + sec.bbox_h
        gap = bbox_bot - lowest_cy
        if gap + tol < SECTION_Y_PADDING:
            violators.append(
                {
                    "kind": "rect",
                    "x": sec.bbox_x,
                    "y": lowest_cy - 4,
                    "w": sec.bbox_w,
                    "h": bbox_bot - (lowest_cy - 4),
                    "note": (
                        f"section {sec_id}: gap={gap:.1f}px between lowest "
                        f"station {lowest_sid} cy={lowest_cy:.1f} and bbox "
                        f"bottom={bbox_bot:.1f} < SECTION_Y_PADDING="
                        f"{SECTION_Y_PADDING}px"
                    ),
                    "bbox_pad_info": {
                        "section_id": sec_id,
                        "lowest_sid": lowest_sid,
                        "lowest_cy": lowest_cy,
                        "bbox_bot": bbox_bot,
                        "gap": gap,
                        "required": float(SECTION_Y_PADDING),
                    },
                }
            )
    return violators


def find_topo_siblings_violators(graph, engine) -> list[dict]:
    preds: dict[str, set[str]] = defaultdict(set)
    succs: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges:
        preds[e.target].add(e.source)
        succs[e.source].add(e.target)
    classes = defaultdict(list)
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or st.off_track:
            continue
        if not preds[sid] or not succs[sid]:
            continue
        key = (
            frozenset(preds[sid]),
            frozenset(succs[sid]),
            frozenset(graph.station_lines(sid)),
        )
        classes[key].append(sid)
    violators = []
    for key, members in classes.items():
        if len(members) < 2:
            continue
        ys_sorted = sorted(graph.stations[s].y for s in members)
        if max(ys_sorted) - min(ys_sorted) < 2.0:
            continue
        is_offender = False
        if len(members) == 2:
            is_offender = True
        else:
            mean_y = sum(ys_sorted) / len(ys_sorted)
            for y in ys_sorted:
                mirror = 2 * mean_y - y
                if not any(abs(other - mirror) < 2.0 for other in ys_sorted):
                    is_offender = True
                    break
        if not is_offender:
            continue
        xs = [graph.stations[m].x for m in members]
        ys = [graph.stations[m].y for m in members]
        lo_x = min(xs) - 20
        hi_x = max(xs) + 20
        lo_y = min(ys) - 16
        hi_y = max(ys) + 16
        member_str = ", ".join(f"{m}@y={graph.stations[m].y:.1f}" for m in members)
        preds_set, succs_set, _line_set = key
        parents_with_y = []
        for p in sorted(preds_set):
            pst = graph.stations.get(p)
            if pst is not None:
                parents_with_y.append((p, pst.y))
        members_with_y = [(m, graph.stations[m].y) for m in members]
        violators.append(
            {
                "kind": "rect",
                "x": lo_x,
                "y": lo_y,
                "w": hi_x - lo_x,
                "h": hi_y - lo_y,
                "note": (
                    f"topological siblings {{{member_str}}} share preds/succs "
                    f"but ys span {max(ys) - min(ys):.1f}px without symmetry"
                ),
                "siblings_info": {
                    "members": members_with_y,
                    "parents": parents_with_y,
                    "span": max(ys) - min(ys),
                },
            }
        )
    return violators


def find_label_anchor_violators(graph, engine) -> list[dict]:
    """test_label_x_anchored_to_station_marker_on_horizontal_runs.

    Flags middle-anchored labels on horizontal LR/RL runs whose X has
    drifted more than ``_LABEL_DRIFT_TOL`` from the station marker X. The
    expected position is the station's own marker, not a neighbour
    midpoint; the box is anchored on the actual rendered ``<text>`` glyph
    (resolved in :func:`annotate_svg`) rather than the engine's logical
    coords, so it lands on the label the reviewer sees.
    """
    offsets = engine["compute_station_offsets"](graph)
    routes = engine["route_edges"](graph, station_offsets=offsets)
    labels = engine["place_labels"](
        graph, station_offsets=offsets, label_angle=graph.label_angle or 0.0
    )
    label_by_sid = {lp.station_id: lp for lp in labels}
    in_routes = defaultdict(list)
    out_routes = defaultdict(list)
    for r in routes:
        in_routes[r.edge.target].append(r)
        out_routes[r.edge.source].append(r)
    violators = []
    for sid, st in graph.stations.items():
        if st.is_port or st.is_hidden or sid in graph.junctions:
            continue
        if st.off_track:
            continue
        sec = graph.sections.get(st.section_id) if st.section_id else None
        if sec is None or sec.direction not in ("LR", "RL"):
            continue
        lp = label_by_sid.get(sid)
        if lp is None or lp.text_anchor != "middle":
            continue
        ins = in_routes.get(sid, [])
        outs = out_routes.get(sid, [])
        if not ins or not outs:
            continue
        in_horizontal = all(
            len(r.points) >= 2
            and abs(r.points[-2][1] - r.points[-1][1]) <= _Y_TOL
            and abs(r.points[-1][1] - st.y) <= _Y_TOL
            for r in ins
        )
        if not in_horizontal:
            continue
        out_horizontal = all(
            len(r.points) >= 2
            and abs(r.points[0][1] - r.points[1][1]) <= _Y_TOL
            and abs(r.points[0][1] - st.y) <= _Y_TOL
            for r in outs
        )
        if not out_horizontal:
            continue
        drift = abs(lp.x - st.x)
        if drift > _LABEL_DRIFT_TOL:
            violators.append(
                {
                    "kind": "label",
                    "station_id": sid,
                    "expected_x": st.x,
                    "label_x": lp.x,
                    "note": (
                        f"station {sid!r} label.x={lp.x:.1f} vs marker "
                        f"x={st.x:.1f} (drift={drift:.1f}px)"
                    ),
                    "label_info": {
                        "station_id": sid,
                        "station_x": st.x,
                        "label_x": lp.x,
                        "expected_x": st.x,
                        "delta": lp.x - st.x,
                    },
                }
            )
    return violators


def find_stack_x_violators(graph, engine) -> list[dict]:
    """test_visual_stack_station_xs_share_column.

    A visual stack groups same-section stations by predecessor set and
    layer (successors are deliberately omitted, mirroring the live test),
    flagging X drift only when at least one pair sits within
    ``STACK_Y_WINDOW`` so the group reads as a vertical column rather than
    a side-by-side or far-spread layout.
    """
    from nf_metro.layout.constants import Y_SPACING

    stack_y_window = 2.0 * Y_SPACING
    preds = defaultdict(set)
    for e in graph.edges:
        preds[e.target].add(e.source)
    violators = []
    for sec in graph.sections.values():
        if sec.bbox_h <= 0:
            continue
        port_ids = set(sec.entry_ports) | set(sec.exit_ports)
        groups = defaultdict(list)
        for sid in sec.station_ids:
            if sid in port_ids:
                continue
            st = graph.stations.get(sid)
            if st is None or st.is_port or st.is_hidden:
                continue
            key = (frozenset(preds[sid]), st.layer)
            groups[key].append(sid)
        for members in groups.values():
            if len(members) < 2:
                continue
            xs = [graph.stations[s].x for s in members]
            ys = [graph.stations[s].y for s in members]
            visual_stack = any(
                0 < abs(ys[i] - ys[j]) <= stack_y_window
                for i in range(len(members))
                for j in range(i + 1, len(members))
            )
            if max(xs) - min(xs) > 1.0 and visual_stack:
                lo_x = min(xs) - 20
                hi_x = max(xs) + 20
                lo_y = min(ys) - 18
                hi_y = max(ys) + 18
                violators.append(
                    {
                        "kind": "rect",
                        "x": lo_x,
                        "y": lo_y,
                        "w": hi_x - lo_x,
                        "h": hi_y - lo_y,
                        "note": (
                            f"section {sec.id!r} stack {members} "
                            f"xs={[round(x, 1) for x in xs]} "
                            f"drift={max(xs) - min(xs):.1f}px"
                        ),
                        "stack_info": {
                            "section_id": sec.id,
                            "members": list(
                                zip(
                                    members,
                                    [round(x, 1) for x in xs],
                                    [round(y, 1) for y in ys],
                                )
                            ),
                            "drift": max(xs) - min(xs),
                        },
                    }
                )
    return violators


def find_off_track_violators(graph, engine) -> list[dict]:
    """test_off_track_inputs_above_consumer."""
    junction_ids = set(graph.junctions)
    consumer_of: dict[str, str] = {}
    for edge in graph.edges:
        src = graph.stations.get(edge.source)
        tgt = graph.stations.get(edge.target)
        if (
            src is None
            or tgt is None
            or not src.off_track
            or src.is_port
            or src.id in junction_ids
            or tgt.is_port
            or tgt.id in junction_ids
            or tgt.off_track
        ):
            continue
        consumer_of.setdefault(src.id, tgt.id)
    violators = []
    for off_id, consumer_id in consumer_of.items():
        off_st = graph.stations[off_id]
        cons_st = graph.stations[consumer_id]
        if not (off_st.y < cons_st.y - _Y_TOL):
            lo_x = min(off_st.x, cons_st.x) - 18
            hi_x = max(off_st.x, cons_st.x) + 18
            lo_y = min(off_st.y, cons_st.y) - 18
            hi_y = max(off_st.y, cons_st.y) + 18
            violators.append(
                {
                    "kind": "rect",
                    "x": lo_x,
                    "y": lo_y,
                    "w": hi_x - lo_x,
                    "h": hi_y - lo_y,
                    "note": (
                        f"off-track {off_id!r} y={off_st.y:.1f} not above "
                        f"consumer {consumer_id!r} y={cons_st.y:.1f}"
                    ),
                    "off_track_info": {
                        "off_track_id": off_id,
                        "off_track_y": off_st.y,
                        "consumer_id": consumer_id,
                        "consumer_y": cons_st.y,
                    },
                }
            )
    return violators


INVARIANT_FINDERS = {
    "test_row_trunk_marker_cy_consistent": find_row_trunk_violators,
    "test_no_kink_at_section_boundary": find_no_kink_violators,
    "test_symfan_pairs_share_y": find_symfan_violators,
    "test_lines_dont_cross_non_consumer_markers": find_breeze_past_violators,
    "test_section_bbox_has_bottom_padding": find_bbox_padding_violators,
    "test_topological_siblings_share_y_or_symmetric": find_topo_siblings_violators,
    "test_label_x_anchored_to_station_marker_on_horizontal_runs": find_label_anchor_violators,
    "test_visual_stack_station_xs_share_column": find_stack_x_violators,
    "test_off_track_inputs_above_consumer": find_off_track_violators,
}


# ---------------------------------------------------------------------------
# Annotate a base SVG with red overlay boxes.
# ---------------------------------------------------------------------------


_SVG_OPEN_RE = re.compile(r'<svg[^>]*viewBox="([^"]+)"[^>]*>')

_LABEL_CHAR_W_RATIO = 0.6  # rough glyph advance width per char, in font-size units


def _rendered_label_box(
    svg_text: str, station_id: str
) -> tuple[float, float, float, float] | None:
    """Extent ``(x, y, w, h)`` of the rendered ``<text data-station-id=...>``
    glyph, derived from its actual SVG attributes.

    The renderer applies ``text-anchor`` / ``dominant-baseline`` shifts that
    move the drawn glyph away from the engine's logical ``label.x`` / ``label.y``,
    so reading the emitted attributes back out is what lets the overlay box land
    on the ink the reviewer actually sees rather than floating beside it.
    """
    m = re.search(
        rf'<text\b([^>]*)\bdata-station-id="{re.escape(station_id)}"([^>]*)>(.*?)</text>',
        svg_text,
        re.DOTALL,
    )
    if m is None:
        return None
    attrs = m.group(1) + m.group(2)
    inner = m.group(3)

    def attr(name: str, default: str) -> str:
        am = re.search(rf'\b{name}="([^"]*)"', attrs)
        return am.group(1) if am else default

    try:
        x = float(attr("x", "0"))
        y = float(attr("y", "0"))
        font_size = float(attr("font-size", "13"))
    except ValueError:
        return None
    anchor = attr("text-anchor", "start")
    baseline = attr("dominant-baseline", "auto")

    # Inner content is plain text or a stack of <tspan> lines; the box spans
    # the longest line horizontally and all lines vertically.
    text_lines = [s.strip() for s in re.findall(r">([^<]+)<", ">" + inner + "<")]
    text_lines = [s for s in text_lines if s] or [station_id]
    width = max(len(s) for s in text_lines) * font_size * _LABEL_CHAR_W_RATIO
    height = len(text_lines) * font_size

    if anchor == "middle":
        left = x - width / 2
    elif anchor == "end":
        left = x - width
    else:
        left = x

    if baseline == "hanging":
        top = y
    elif baseline == "central":
        top = y - height / 2
    else:
        top = y - font_size
    return left, top, width, height


def annotate_svg(svg_text: str, violators: list[dict]) -> tuple[str, int]:
    """Insert red overlay shapes inside an existing SVG. Returns (svg, count)."""
    if not violators:
        return svg_text, 0
    overlay_parts: list[str] = []
    for v in violators:
        if v["kind"] == "rect":
            x = v["x"]
            y = v["y"]
            w = v["w"]
            h = v["h"]
            overlay_parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                f'height="{h:.1f}" fill="none" stroke="#ff3344" '
                f'stroke-width="3" stroke-dasharray="6,3" '
                f'pointer-events="none"/>'
            )
        elif v["kind"] == "marker":
            cx = v["x"]
            cy = v["y"]
            r = max(v.get("w", 24) / 2, 12)
            overlay_parts.append(
                f'<rect x="{cx - r:.1f}" y="{cy - r:.1f}" width="{2 * r:.1f}" '
                f'height="{2 * r:.1f}" fill="none" stroke="#ff3344" '
                f'stroke-width="3" stroke-dasharray="6,3" '
                f'pointer-events="none"/>'
            )
        elif v["kind"] == "label":
            box = _rendered_label_box(svg_text, v["station_id"])
            if box is None:
                continue
            lx, ly, lw, lh = box
            pad = 4.0
            overlay_parts.append(
                f'<rect x="{lx - pad:.1f}" y="{ly - pad:.1f}" '
                f'width="{lw + 2 * pad:.1f}" height="{lh + 2 * pad:.1f}" '
                f'fill="none" stroke="#ff3344" stroke-width="3" '
                f'stroke-dasharray="6,3" pointer-events="none"/>'
            )
            overlay_parts.append(
                f'<line x1="{v["expected_x"]:.1f}" y1="{ly - pad:.1f}" '
                f'x2="{v["expected_x"]:.1f}" y2="{ly + lh + pad:.1f}" '
                f'stroke="#22aaff" stroke-width="2" pointer-events="none"/>'
            )
    overlay = (
        '<g class="xfail-overlay" style="opacity:0.95">'
        + "".join(overlay_parts)
        + "</g>"
    )
    if "</svg>" not in svg_text:
        return svg_text + overlay, len(violators)
    return svg_text.replace("</svg>", overlay + "</svg>"), len(violators)


# ---------------------------------------------------------------------------
# Fail-list ingestion
# ---------------------------------------------------------------------------


_FAIL_RE = re.compile(
    r"^(?:FAILED|XFAIL)\s+tests/test_layout_invariants\.py::([A-Za-z_0-9]+)\[(.+?)\](?:\s+-\s+(.*))?$"
)


def parse_fail_list(text: str) -> list[dict]:
    """Parse pytest output (FAILED + XFAIL lines) into entries."""
    entries: list[dict] = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        m = _FAIL_RE.match(line)
        if not m:
            continue
        inv, fixture, reason = m.group(1), m.group(2), m.group(3) or ""
        key = (inv, fixture)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"invariant": inv, "fixture": fixture, "reason": reason})
    return entries


def collect_failures_via_pytest(worktree: Path, target: str) -> str:
    """Run pytest -rfX --tb=no -q against ``target`` inside ``worktree``
    and return its stdout. Failures don't abort us - the report is the goal."""
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{worktree / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    cmd = ["pytest", target, "-rfX", "--tb=no", "-q", "--no-header"]
    proc = subprocess.run(
        cmd,
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build_rows_from_violations(
    violations: list[dict], render_svg, annotated_dir: Path
) -> list[dict]:
    """Build triage rows directly from a list of pre-computed violation
    entries of shape ``{fixture, invariant, rects, issue, check}``, bypassing
    pytest discovery and the finder registry. Each ``rect`` becomes a
    red-bbox overlay; ``issue`` / ``check`` (if present) become the
    explanation block."""
    render_cache: dict[str, tuple[str | None, str | None]] = {}
    rows: list[dict] = []
    for i, entry in enumerate(violations):
        fixture = entry["fixture"]
        inv = entry.get("invariant", "ad-hoc-check")
        key = f"{fixture.replace('/', '__')}__{inv}__{i}"

        if fixture not in render_cache:
            svg_path, err = render_svg(fixture)
            render_cache[fixture] = (
                (svg_path.read_text(), None) if svg_path is not None else (None, err)
            )
        base_svg, render_err = render_cache[fixture]

        violators = [
            {
                "kind": "rect",
                "x": float(rect["x"]),
                "y": float(rect["y"]),
                "w": float(rect["w"]),
                "h": float(rect["h"]),
                "note": rect.get("note", ""),
            }
            for rect in entry.get("rects", [])
        ]

        if base_svg is None:
            annotated_svg = None
        else:
            annotated_svg, _ = annotate_svg(base_svg, violators)
            (annotated_dir / f"{key}.svg").write_text(annotated_svg)

        rows.append(
            {
                "key": key,
                "fixture": fixture,
                "invariant": inv,
                "reason": entry.get("reason", ""),
                "render_error": render_err,
                "violators": violators,
                "violator_error": None
                if violators
                else "no rects supplied; full fixture shown",
                "svg": annotated_svg,
                "issue": entry.get("issue"),
                "check": entry.get("check"),
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    worktree = args.worktree.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    renders_dir = output_dir / "renders"
    annotated_dir = renders_dir / "annotated"
    renders_dir.mkdir(parents=True, exist_ok=True)
    annotated_dir.mkdir(parents=True, exist_ok=True)

    engine = _setup_imports(worktree)
    resolve_fixture, fixtures_dir = make_resolve_fixture(worktree)
    load_layout = make_load_layout(engine, resolve_fixture, fixtures_dir)
    render_svg = make_render_svg(resolve_fixture, renders_dir)

    finders = dict(INVARIANT_FINDERS)
    extra_explanations: dict = {}
    if args.finder_module is not None:
        module = load_finder_module(args.finder_module)
        merge_finder_module(module, finders, extra_explanations)
        print(
            f"Loaded finder module {args.finder_module}: "
            f"{len(finders) - len(INVARIANT_FINDERS)} extra finder(s), "
            f"{len(extra_explanations)} extra explanation(s)",
            file=sys.stderr,
        )

    # Ad-hoc path: render cards straight from a violations JSON, skipping
    # pytest discovery and the finder registry entirely.
    if args.violations is not None:
        violations = json.loads(args.violations.read_text())
        print(
            f"Loaded {len(violations)} violation entr(ies) from {args.violations}",
            file=sys.stderr,
        )
        rows = build_rows_from_violations(violations, render_svg, annotated_dir)
        html = build_html(rows, generic_explanations=extra_explanations)
        out = output_dir / "index.html"
        out.write_text(html)
        print(f"Wrote {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)
        return 0

    if args.fail_list is not None:
        fail_text = args.fail_list.read_text()
        print(f"Reading fail list from {args.fail_list}", file=sys.stderr)
    else:
        print(
            f"Running pytest {args.pytest_target} in {worktree} to collect "
            "FAILED/XFAIL entries...",
            file=sys.stderr,
        )
        fail_text = collect_failures_via_pytest(worktree, args.pytest_target)
        # Persist so future runs can be re-driven without re-running pytest.
        (output_dir / "fail-list.txt").write_text(fail_text)

    entries = parse_fail_list(fail_text)
    print(f"Loaded {len(entries)} fail/xfail entries", file=sys.stderr)

    render_cache: dict[str, tuple[str | None, str | None]] = {}
    layout_cache: dict[str, tuple[object | None, str | None]] = {}

    bbox_attempted = 0
    bbox_succeeded = 0
    bbox_fallback = 0
    render_attempted = 0
    render_succeeded = 0
    render_failed = 0

    rows = []
    for entry in entries:
        fixture = entry["fixture"]
        inv = entry["invariant"]
        key = f"{fixture.replace('/', '__')}__{inv}"

        if fixture not in render_cache:
            render_attempted += 1
            svg_path, err = render_svg(fixture)
            if svg_path is None:
                render_failed += 1
                render_cache[fixture] = (None, err)
            else:
                render_succeeded += 1
                render_cache[fixture] = (svg_path.read_text(), None)
        base_svg, render_err = render_cache[fixture]

        if fixture not in layout_cache:
            try:
                layout_cache[fixture] = (load_layout(fixture), None)
            except Exception as e:  # noqa: BLE001
                layout_cache[fixture] = (
                    None,
                    f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}",
                )
        graph, layout_err = layout_cache[fixture]

        bbox_attempted += 1
        violators: list[dict] = []
        violator_err: str | None = None
        finder = finders.get(inv)
        if finder is None:
            bbox_fallback += 1
            violator_err = f"no violator extractor implemented for {inv}"
        elif graph is None:
            bbox_fallback += 1
            violator_err = f"layout failed: {layout_err}"
        else:
            try:
                violators = finder(graph, engine)
                if violators:
                    bbox_succeeded += 1
                else:
                    bbox_fallback += 1
                    violator_err = (
                        "extractor ran but found no offending element "
                        "(invariant may xfail under different params than the "
                        "viewer pipeline; full fixture shown)"
                    )
            except Exception as e:  # noqa: BLE001
                bbox_fallback += 1
                violator_err = f"violator extraction error: {type(e).__name__}: {e}"

        if base_svg is None:
            annotated_svg = None
        else:
            annotated_svg, _ = annotate_svg(base_svg, violators)
            (annotated_dir / f"{key}.svg").write_text(annotated_svg)

        rows.append(
            {
                "key": key,
                "fixture": fixture,
                "invariant": inv,
                "reason": entry["reason"],
                "render_error": render_err,
                "violators": violators,
                "violator_error": violator_err,
                "svg": annotated_svg,
            }
        )

    print(
        f"Renders: attempted={render_attempted} ok={render_succeeded} "
        f"failed={render_failed}",
        file=sys.stderr,
    )
    print(
        f"Bbox: attempted={bbox_attempted} ok={bbox_succeeded} "
        f"fallback={bbox_fallback}",
        file=sys.stderr,
    )

    html = build_html(rows, generic_explanations=extra_explanations)
    out = output_dir / "index.html"
    out.write_text(html)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)

    if render_failed:
        print(f"CAVEAT: {render_failed} fixture(s) failed to render", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


# Generic (issue, check) explanation pairs keyed by invariant. Finder-module
# EXPLANATIONS dicts use the same shape and are merged over this at runtime.
_GENERIC_EXPLANATIONS = {
    "test_row_trunk_marker_cy_consistent": (
        "Sections placed side-by-side in the same grid row are "
        "supposed to share a single trunk Y, so the horizontal rail "
        "through the row reads as one straight line. The invariant "
        "reports a drift in this fixture.",
        "Scan the horizontal trunk across each row. If you can see a "
        "vertical step inside any section that should run straight, "
        "it's a bug. If sections are intentionally on different rows, "
        "the row-grouping heuristic may be wrong and this is "
        "<strong>not</strong> a bug.",
    ),
    "test_no_kink_at_section_boundary": (
        "An exit port of one section is at a different Y than the "
        "entry port of the adjacent section, so the line between "
        "them has a vertical step.",
        "Look along section boundaries for a visible jog where a "
        "horizontal continuation should be straight. If adjacent "
        "sections are intentionally on different rows, the kink may "
        "be intended.",
    ),
    "test_symfan_pairs_share_y": (
        "A pair of branches feeding from a common point in this "
        "fixture is supposed to mirror around the trunk Y but does "
        "not.",
        "Look for fan-outs with two branches that obviously don't "
        "sit symmetrically across the trunk. If one branch is "
        "intentionally weighted, flag as Ambiguous.",
    ),
    "test_lines_dont_cross_non_consumer_markers": (
        "A routed line passes through a station marker it shouldn't "
        "touch (the station is neither producer nor consumer of "
        "that line).",
        "Find any line that visibly crosses through a station "
        "circle that isn't its endpoint. If the line only grazes a "
        "corner or the bbox is overly generous, flag as Ambiguous.",
    ),
    "test_section_bbox_has_bottom_padding": (
        "A section's bottom edge sits too close to its lowest "
        "internal station (less than the required padding).",
        "Look at section borders. If a station marker or label "
        "touches the border, it's a bug. If there's visible "
        "whitespace, flag as Ambiguous.",
    ),
    "test_topological_siblings_share_y_or_symmetric": (
        "Stations sharing the same predecessors, successors and "
        "line set are supposed to align in Y (or fan symmetrically) "
        "but don't in this fixture.",
        "Look for siblings of a common parent that visibly fail to "
        "align or mirror. If the diagram intentionally cascades "
        "them, the invariant is mis-identifying siblings - flag "
        "<strong>not</strong> a bug.",
    ),
    "test_label_x_anchored_to_station_marker_on_horizontal_runs": (
        "A station label has drifted off its own marker on a "
        "horizontal run in this fixture.",
        "Find labels that visibly sit left or right of the station "
        "circle they belong to. If the run isn't really horizontal, "
        "flag Ambiguous.",
    ),
    "test_visual_stack_station_xs_share_column": (
        "Stations that should stack in a single column drift "
        "apart on the X axis in this fixture.",
        "Look for clusters that visibly fan out where they should "
        "be a vertical stack. If a grid override is intentional, "
        "flag Ambiguous.",
    ),
    "test_off_track_inputs_above_consumer": (
        "An off-track input station does not sit above the station it feeds.",
        "Find off-track inputs whose feed arrow points up instead "
        "of down. If the layout intentionally puts the input "
        "below, flag Ambiguous.",
    ),
}


def _explanation_html(issue_sentences: list[str], check_sentences: list[str]) -> str:
    if not issue_sentences:
        return ""
    issue_html = " ".join(issue_sentences)
    check_html = " ".join(check_sentences)
    return (
        '<div class="block-issue"><strong>Supposed issue:</strong> '
        + issue_html
        + "</div>"
        + '<div class="block-check"><strong>What to check:</strong> '
        + check_html
        + "</div>"
    )


def _build_explanation_blocks(
    invariant: str,
    violators: list[dict],
    generic_explanations: dict | None = None,
    explicit_issue: str | None = None,
    explicit_check: str | None = None,
) -> str:
    # Ad-hoc / violation-JSON entries can supply their own prose directly.
    if explicit_issue or explicit_check:
        return _explanation_html(
            [explicit_issue] if explicit_issue else [],
            [explicit_check] if explicit_check else [],
        )

    generic = dict(_GENERIC_EXPLANATIONS)
    if generic_explanations:
        generic.update(generic_explanations)

    def esc(s) -> str:
        s = str(s)
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    issue_sentences: list[str] = []
    check_sentences: list[str] = []

    for v in violators:
        if invariant == "test_row_trunk_marker_cy_consistent":
            info = v.get("row_trunk_info")
            if info:
                issue_sentences.append(
                    f"Section <code>{esc(info['section_id'])}</code> in row "
                    f"<code>{esc(info['row'])}</code> has its trunk at "
                    f"cy=<code>{info['section_cy']:.1f}</code>, but the row's "
                    f"anchor section <code>{esc(info['anchor_section_id'])}</code> "
                    f"sits at cy=<code>{info['anchor_cy']:.1f}</code> "
                    f"(delta <code>{info['delta']:+.1f}</code>px). The invariant "
                    f"says all sections placed side-by-side in a row should "
                    f"share the same trunk height so the horizontal line "
                    f"through them looks like one straight rail."
                )
                check_sentences.append(
                    f"Look at the horizontal trunk through row "
                    f"<code>{esc(info['row'])}</code> (red bbox at trunk "
                    f"station <code>{esc(info['trunk_station'])}</code>). If "
                    f"the trunk visibly steps up or down inside section "
                    f"<code>{esc(info['section_id'])}</code>, that's the kink "
                    f"and it's a bug. If section "
                    f"<code>{esc(info['section_id'])}</code> is visually on a "
                    f"different row, or is the only section in its row in "
                    f"this fixture's layout, the row-grouping heuristic is "
                    f"wrong and this is <strong>not</strong> a bug."
                )
        elif invariant == "test_no_kink_at_section_boundary":
            info = v.get("kink_info")
            if info:
                issue_sentences.append(
                    f"The exit port <code>{esc(info['exit_port'])}</code> of "
                    f"section <code>{esc(info['sec_a_id'])}</code> is at "
                    f"cy=<code>{info['exit_cy']:.1f}</code>, but the entry "
                    f"port <code>{esc(info['entry_port'])}</code> of the "
                    f"adjacent section <code>{esc(info['sec_b_id'])}</code> "
                    f"is at cy=<code>{info['entry_cy']:.1f}</code> "
                    f"(delta <code>{info['delta']:+.1f}</code>px). The line "
                    f"between them therefore has a vertical step instead of "
                    f"running straight."
                )
                check_sentences.append(
                    f"Look at the line crossing the boundary between sections "
                    f"<code>{esc(info['sec_a_id'])}</code> and "
                    f"<code>{esc(info['sec_b_id'])}</code> (red bbox). If you "
                    f"see a visible vertical step / jog in what should be a "
                    f"horizontal continuation, it's a bug. If the two "
                    f"sections are intentionally on different rows (e.g. one "
                    f"feeds a station below the other), the kink is intended "
                    f"layout, not a defect."
                )
        elif invariant == "test_symfan_pairs_share_y":
            info = v.get("symfan_info")
            if info:
                issue_sentences.append(
                    f"Stations <code>{esc(info['top_sid'])}</code> and "
                    f"<code>{esc(info['bot_sid'])}</code> are a paired branch "
                    f"of a symmetric fan inside section "
                    f"<code>{esc(info['section_id'])}</code> (fed via "
                    f"<code>{esc(info['source'])}</code>), expected to sit at "
                    f"mirrored Y around the trunk "
                    f"cy=<code>{info['trunk_cy']:.1f}</code>. Their Y values "
                    f"are <code>{info['top_cy']:.1f}</code> vs "
                    f"<code>{info['bot_cy']:.1f}</code>, so the fan isn't "
                    f"symmetric (asymmetry delta "
                    f"<code>{info['delta']:+.1f}</code>px)."
                )
                check_sentences.append(
                    f"Look at the fan inside section "
                    f"<code>{esc(info['section_id'])}</code> (red bbox around "
                    f"the two stations). If the upper and lower branches "
                    f"obviously don't mirror each other across the trunk, "
                    f"it's a bug. If one branch is intentionally weighted by "
                    f"station count, or the pair is actually two unrelated "
                    f"stations that happen to share a column, asymmetry may "
                    f"be expected - flag those as Ambiguous."
                )
        elif invariant == "test_lines_dont_cross_non_consumer_markers":
            info = v.get("breeze_info")
            if info:
                issue_sentences.append(
                    f"The line <code>{esc(info['line_id'])}</code> (on edge "
                    f"<code>{esc(info['edge_source'])}</code> &rarr; "
                    f"<code>{esc(info['edge_target'])}</code>) passes through "
                    f"the marker bbox of station "
                    f"<code>{esc(info['station'])}</code>, but "
                    f"<code>{esc(info['station'])}</code> is not a consumer "
                    f"or producer of <code>{esc(info['line_id'])}</code>. "
                    f"The route should detour around it (via a bypass V or "
                    f"virtual station)."
                )
                check_sentences.append(
                    f"Look at station <code>{esc(info['station'])}</code> "
                    f"(red bbox). If a line you can see clearly enters the "
                    f"station's marker and exits the other side without the "
                    f"station being one of the line's endpoints, it's a bug. "
                    f"If the line only grazes the bbox corner, or the bbox "
                    f"includes generous engine padding that overstates the "
                    f"marker's visual footprint, flag as Ambiguous."
                )
        elif invariant == "test_section_bbox_has_bottom_padding":
            info = v.get("bbox_pad_info")
            if info:
                issue_sentences.append(
                    f"Section <code>{esc(info['section_id'])}</code>'s "
                    f"bottom edge is at y=<code>{info['bbox_bot']:.1f}</code>, "
                    f"and its bottom-most station "
                    f"<code>{esc(info['lowest_sid'])}</code> sits at "
                    f"y=<code>{info['lowest_cy']:.1f}</code>. The invariant "
                    f"requires <code>{info['required']:.0f}</code>px of "
                    f"clearance from the lowest station centre to the bbox "
                    f"bottom; current clearance is "
                    f"<code>{info['gap']:.1f}</code>px."
                )
                check_sentences.append(
                    f"Look at the bottom of section "
                    f"<code>{esc(info['section_id'])}</code> (red bbox covers "
                    f"the area between station "
                    f"<code>{esc(info['lowest_sid'])}</code> and the bbox "
                    f"bottom). If the station's marker (or its label) "
                    f"clearly touches or pokes into the section border, it's "
                    f"a bug. If there's visible whitespace and the invariant "
                    f"just requires unusually generous padding from the "
                    f"station centre, flag Ambiguous."
                )
        elif invariant == "test_topological_siblings_share_y_or_symmetric":
            info = v.get("siblings_info")
            if info:
                members_part = ", ".join(
                    f"<code>{esc(m)}</code> y=<code>{y:.1f}</code>"
                    for m, y in info["members"]
                )
                if info["parents"]:
                    if len(info["parents"]) == 1:
                        p, py = info["parents"][0]
                        parent_part = (
                            f"parent <code>{esc(p)}</code> (y=<code>{py:.1f}</code>)"
                        )
                    else:
                        parent_part = (
                            "parents {"
                            + ", ".join(
                                f"<code>{esc(p)}</code> y=<code>{py:.1f}</code>"
                                for p, py in info["parents"]
                            )
                            + "}"
                        )
                else:
                    parent_part = "their shared predecessor(s)"
                issue_sentences.append(
                    f"Stations {members_part} are topological siblings (they "
                    f"share predecessors, successors and line set, fed from "
                    f"{parent_part}), but their Y values aren't equal and "
                    f"aren't mirrored. The invariant says siblings should "
                    f"line up or fan symmetrically (Y span "
                    f"<code>{info['span']:.1f}</code>px)."
                )
                check_sentences.append(
                    "Look at the branch-out around the parent stations "
                    "(red bbox). If the siblings visually look misaligned - "
                    "neither side-by-side nor symmetric around a centre "
                    "line - it's a bug. If the diagram intentionally "
                    "cascades these siblings (e.g. one feeds another "
                    "downstream, breaking the 'true sibling' assumption), "
                    "the invariant is mis-identifying them as siblings and "
                    "this is <strong>not</strong> a bug."
                )
        elif invariant == "test_label_x_anchored_to_station_marker_on_horizontal_runs":
            info = v.get("label_info")
            if info:
                issue_sentences.append(
                    f"Station <code>{esc(info['station_id'])}</code>'s label "
                    f"sits at x=<code>{info['label_x']:.1f}</code>, but its "
                    f"marker is at x=<code>{info['station_x']:.1f}</code> "
                    f"(drift <code>{info['delta']:+.1f}</code>px). The "
                    f"invariant says a middle-anchored label on a horizontal "
                    f"run should sit over its own station marker."
                )
                check_sentences.append(
                    f"Look at the label for station "
                    f"<code>{esc(info['station_id'])}</code> (red bbox is the "
                    f"rendered label; the blue tick marks where its marker is). "
                    f"If the label visibly sits left or right of the station "
                    f"circle, it's a bug. If the station has non-horizontal "
                    f"incoming or outgoing edges hidden by this fixture's "
                    f"geometry, flag Ambiguous."
                )
        elif invariant == "test_visual_stack_station_xs_share_column":
            info = v.get("stack_info")
            if info:
                members_part = ", ".join(
                    f"<code>{esc(m)}</code>@x=<code>{x:.1f}</code>"
                    for m, x, _y in info["members"]
                )
                issue_sentences.append(
                    f"Stations {members_part} in section "
                    f"<code>{esc(info['section_id'])}</code> share the same "
                    f"predecessors, successors and layer, so they should "
                    f"stack in a single column. Their Xs drift by "
                    f"<code>{info['drift']:.1f}</code>px, breaking the "
                    f"column alignment."
                )
                check_sentences.append(
                    f"Look at the cluster of stations inside section "
                    f"<code>{esc(info['section_id'])}</code> (red bbox). If "
                    f"the stack visibly fans out into separate columns when "
                    f"it should be a vertical line of stations, it's a bug. "
                    f"If one of the stations is intentionally pushed out "
                    f"horizontally (e.g. by a manual grid override), flag "
                    f"Ambiguous."
                )
        elif invariant == "test_off_track_inputs_above_consumer":
            info = v.get("off_track_info")
            if info:
                issue_sentences.append(
                    f"Off-track input <code>{esc(info['off_track_id'])}</code> "
                    f"at y=<code>{info['off_track_y']:.1f}</code> is not above "
                    f"its consumer <code>{esc(info['consumer_id'])}</code> at "
                    f"y=<code>{info['consumer_y']:.1f}</code>. The invariant "
                    f"says off-track inputs should sit above the station they "
                    f"feed so the drop-in arrow reads as a downward feed."
                )
                check_sentences.append(
                    f"Look at the off-track input "
                    f"<code>{esc(info['off_track_id'])}</code> relative to "
                    f"<code>{esc(info['consumer_id'])}</code> (red bbox). If "
                    f"the off-track station sits at or below its consumer, "
                    f"giving an upward-pointing feed arrow, it's a bug. If "
                    f"this is an intentional below-the-track input (rare), "
                    f"flag Ambiguous."
                )

    if not issue_sentences:
        gen = generic.get(invariant)
        if gen:
            issue_sentences.append(gen[0])
            check_sentences.append(gen[1])

    return _explanation_html(issue_sentences, check_sentences)


def build_html(rows: list[dict], generic_explanations: dict | None = None) -> str:
    fixtures = sorted({r["fixture"] for r in rows})
    invariants = sorted({r["invariant"] for r in rows})

    row_meta = [
        {"key": r["key"], "fixture": r["fixture"], "invariant": r["invariant"]}
        for r in rows
    ]
    rows_json = json.dumps(row_meta)

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    row_html_parts = []
    for r in rows:
        key = r["key"]
        fixture = r["fixture"]
        inv = r["invariant"]
        reason = r["reason"]
        svg = r["svg"]
        v_err = r["violator_error"]
        rdr_err = r["render_error"]

        violator_summary = ""
        if r["violators"]:
            violator_summary = (
                f'<div class="violator-list"><strong>{len(r["violators"])} '
                f"violator(s) highlighted:</strong><ul>"
                + "".join(f"<li>{esc(v.get('note', ''))}</li>" for v in r["violators"])
                + "</ul></div>"
            )
        elif v_err:
            violator_summary = f'<div class="violator-note"><em>{esc(v_err)}</em></div>'

        if svg is None:
            img_block = (
                f'<div class="render-fail">render failed: '
                f"{esc(rdr_err or 'unknown')}</div>"
            )
        else:
            b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            img_block = (
                f'<img loading="lazy" '
                f'src="data:image/svg+xml;base64,{b64}" '
                f'alt="{esc(fixture)} {esc(inv)}"/>'
            )

        explanation_blocks = _build_explanation_blocks(
            inv,
            r["violators"],
            generic_explanations=generic_explanations,
            explicit_issue=r.get("issue"),
            explicit_check=r.get("check"),
        )

        row_html_parts.append(
            f"""
<article class="row untagged" data-key="{esc(key)}"
         data-fixture="{esc(fixture)}" data-invariant="{esc(inv)}">
  <header class="row-head">
    <h3><span class="fixture">{esc(fixture)}</span>
        <span class="sep">/</span>
        <span class="invariant">{esc(inv)}</span></h3>
    <pre class="xfail-reason">{esc(reason)}</pre>
  </header>
  <div class="row-body">
    <div class="render-pane">{img_block}</div>
    <div class="controls-pane">
      {violator_summary}
      {explanation_blocks}
      <fieldset class="tag-fieldset">
        <legend>Classification</legend>
        <label><input type="radio" name="tag-{esc(key)}" value="bug"> Bug</label>
        <label><input type="radio" name="tag-{esc(key)}" value="not-a-bug"> Not a bug</label>
        <label><input type="radio" name="tag-{esc(key)}" value="ambiguous"> Ambiguous</label>
      </fieldset>
      <textarea class="notes" data-key="{esc(key)}"
                placeholder="Notes (optional)"></textarea>
    </div>
  </div>
</article>
""".strip()
        )

    rows_html = "\n".join(row_html_parts)

    fixture_options = "\n".join(
        f'<option value="{esc(f)}">{esc(f)}</option>' for f in fixtures
    )
    invariant_options = "\n".join(
        f'<option value="{esc(i)}">{esc(i)}</option>' for i in invariants
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>nf-metro layout-invariant triage</title>
<style>
:root {{
  --bg: #f6f7f9;
  --card-bg: #ffffff;
  --border: #d6dae0;
  --text: #1a1d21;
  --muted: #6e757d;
  --accent: #2255cc;
  --untagged: #f0c83a;
  --bug: #d9534f;
  --not-bug: #28a745;
  --ambiguous: #b07ad6;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial,
    sans-serif;
  background: var(--bg);
  color: var(--text);
}}
header.page-head {{
  position: sticky;
  top: 0;
  z-index: 100;
  background: #1a1d21;
  color: #fff;
  padding: 12px 20px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.15);
}}
header.page-head h1 {{
  margin: 0 0 4px 0;
  font-size: 18px;
}}
.stats {{ font-size: 13px; color: #c0c5cb; }}
.page-actions {{ margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap;
  align-items: center; }}
.page-actions button {{
  padding: 6px 12px;
  border-radius: 4px;
  border: 1px solid #555;
  background: #2b2f35;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
}}
.page-actions button:hover {{ background: #3a3f47; }}
.progress {{ font-weight: bold; padding: 4px 10px;
  background: #2b2f35; border-radius: 4px; }}
.filter-bar {{
  position: sticky;
  top: 76px;
  z-index: 90;
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
  padding: 8px 20px;
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 13px;
  align-items: center;
}}
.filter-bar label {{ display: flex; align-items: center; gap: 4px; }}
.filter-bar select {{
  padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px;
}}
main {{ padding: 16px 20px; }}
.row {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-left: 6px solid var(--untagged);
  border-radius: 6px;
  margin-bottom: 16px;
  padding: 12px 16px;
  transition: border-color 120ms ease;
}}
.row:hover {{ box-shadow: 0 2px 6px rgba(0,0,0,0.08); }}
.row.tag-bug {{ border-left-color: var(--bug); }}
.row.tag-not-a-bug {{ border-left-color: var(--not-bug); }}
.row.tag-ambiguous {{ border-left-color: var(--ambiguous); }}
.row.hidden {{ display: none; }}
.row-head h3 {{
  margin: 0 0 6px 0;
  font-size: 15px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}}
.row-head .fixture {{ color: var(--accent); }}
.row-head .invariant {{ color: #c84a26; }}
.row-head .sep {{ color: var(--muted); margin: 0 4px; }}
.xfail-reason {{
  margin: 0;
  font-size: 12px;
  background: #f3f4f7;
  border: 1px solid #e1e3e8;
  padding: 6px 10px;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  white-space: pre-wrap;
}}
.row-body {{
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 16px;
  margin-top: 10px;
}}
@media (max-width: 980px) {{
  .row-body {{ grid-template-columns: 1fr; }}
}}
.render-pane img {{
  width: 100%;
  height: auto;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: #2b2b2b;
  display: block;
}}
.render-fail {{
  padding: 24px;
  background: #fde8e9;
  border: 1px solid #f3c2c5;
  border-radius: 4px;
  color: #882020;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  white-space: pre-wrap;
  font-size: 12px;
}}
.controls-pane {{ display: flex; flex-direction: column; gap: 10px; }}
.violator-list {{
  font-size: 12px;
  background: #fff7e6;
  border: 1px solid #f2d391;
  padding: 6px 10px;
  border-radius: 4px;
}}
.violator-list ul {{ margin: 4px 0 0 16px; padding: 0; }}
.violator-list li {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco,
  Consolas, monospace; line-height: 1.4; }}
.violator-note {{
  font-size: 12px;
  background: #f3f4f7;
  border: 1px dashed var(--border);
  padding: 6px 10px;
  border-radius: 4px;
  color: var(--muted);
}}
.block-issue {{
  border-left: 4px solid #d97706;
  background: #fffbeb;
  padding: 8px 12px;
  margin: 8px 0;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.45;
}}
.block-issue strong {{ color: #92400e; }}
.block-check {{
  border-left: 4px solid #2563eb;
  background: #eff6ff;
  padding: 8px 12px;
  margin: 8px 0;
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.45;
}}
.block-check strong {{ color: #1e40af; }}
.block-issue code, .block-check code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  background: rgba(0,0,0,0.06);
  padding: 0 3px;
  border-radius: 3px;
}}
.tag-fieldset {{
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 8px 10px;
  margin: 0;
}}
.tag-fieldset legend {{ font-size: 12px; padding: 0 4px; color: var(--muted); }}
.tag-fieldset label {{ display: inline-block; margin-right: 10px; font-size: 13px; }}
.notes {{
  width: 100%;
  min-height: 60px;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 6px 8px;
  font-family: inherit;
  font-size: 13px;
  resize: vertical;
}}
.legend {{
  font-size: 12px;
  color: var(--muted);
}}
.legend .swatch {{
  display: inline-block;
  width: 12px;
  height: 12px;
  vertical-align: middle;
  margin-right: 2px;
  border: 1px solid #888;
}}
</style>
</head>
<body>
<header class="page-head">
  <h1>nf-metro layout-invariant triage</h1>
  <div class="stats">{len(rows)} fail/xfail entries across {len(fixtures)} fixtures &times;
    {len(invariants)} invariants.
    Red dashed boxes mark the violating element(s) computed from the layout.</div>
  <div class="page-actions">
    <span class="progress" id="progress">0 / {len(rows)} tagged</span>
    <button id="export-btn">Export JSON</button>
    <button id="reset-btn">Reset localStorage</button>
    <span class="legend">
      <span class="swatch" style="background: var(--untagged)"></span> untagged
      <span class="swatch" style="background: var(--bug)"></span> bug
      <span class="swatch" style="background: var(--not-bug)"></span> not a bug
      <span class="swatch" style="background: var(--ambiguous)"></span> ambiguous
    </span>
  </div>
</header>
<div class="filter-bar">
  <label>Fixture
    <select id="filter-fixture">
      <option value="">(any)</option>
      {fixture_options}
    </select>
  </label>
  <label>Invariant
    <select id="filter-invariant">
      <option value="">(any)</option>
      {invariant_options}
    </select>
  </label>
  <label>Tag
    <select id="filter-tag">
      <option value="">(any)</option>
      <option value="untagged">untagged</option>
      <option value="bug">bug</option>
      <option value="not-a-bug">not a bug</option>
      <option value="ambiguous">ambiguous</option>
    </select>
  </label>
</div>
<main id="rows">
{rows_html}
</main>
<script>
const ROW_META = {rows_json};
const STORAGE_KEY = "nfmetro-xfail-review-v1";

function loadState() {{
  try {{
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{{}}");
  }} catch (e) {{ return {{}}; }}
}}
function saveState(state) {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}}

function applyRowVisual(rowEl, tag) {{
  rowEl.classList.remove("tag-bug", "tag-not-a-bug", "tag-ambiguous", "untagged");
  if (tag) {{
    rowEl.classList.add("tag-" + tag);
  }} else {{
    rowEl.classList.add("untagged");
  }}
}}

function updateProgress() {{
  const state = loadState();
  const tagged = Object.values(state).filter(v => v && v.tag).length;
  document.getElementById("progress").textContent =
    tagged + " / " + ROW_META.length + " tagged";
}}

function restore() {{
  const state = loadState();
  for (const meta of ROW_META) {{
    const k = meta.key;
    const entry = state[k] || {{}};
    const rowEl = document.querySelector('article[data-key="' + cssEscape(k) + '"]');
    if (!rowEl) continue;
    if (entry.tag) {{
      const radio = rowEl.querySelector('input[name="tag-' + k + '"][value="' + entry.tag + '"]');
      if (radio) radio.checked = true;
      applyRowVisual(rowEl, entry.tag);
    }} else {{
      applyRowVisual(rowEl, null);
    }}
    if (entry.notes) {{
      const ta = rowEl.querySelector('textarea.notes');
      if (ta) ta.value = entry.notes;
    }}
  }}
  updateProgress();
}}

function cssEscape(s) {{
  return s.replace(/(["\\\\\\]\\[])/g, "\\\\$1");
}}

function onTagChange(e) {{
  if (e.target.matches('input[type="radio"][name^="tag-"]')) {{
    const key = e.target.name.slice(4);
    const state = loadState();
    state[key] = state[key] || {{}};
    state[key].tag = e.target.value;
    saveState(state);
    const rowEl = e.target.closest("article.row");
    applyRowVisual(rowEl, e.target.value);
    updateProgress();
    applyFilters();
  }}
}}

function onNotesChange(e) {{
  if (e.target.matches('textarea.notes')) {{
    const key = e.target.dataset.key;
    const state = loadState();
    state[key] = state[key] || {{}};
    state[key].notes = e.target.value;
    saveState(state);
  }}
}}

function applyFilters() {{
  const fFix = document.getElementById("filter-fixture").value;
  const fInv = document.getElementById("filter-invariant").value;
  const fTag = document.getElementById("filter-tag").value;
  const state = loadState();
  document.querySelectorAll("article.row").forEach(row => {{
    const k = row.dataset.key;
    const fix = row.dataset.fixture;
    const inv = row.dataset.invariant;
    const tag = (state[k] && state[k].tag) || "";
    let show = true;
    if (fFix && fix !== fFix) show = false;
    if (fInv && inv !== fInv) show = false;
    if (fTag === "untagged" && tag !== "") show = false;
    if (fTag && fTag !== "untagged" && tag !== fTag) show = false;
    row.classList.toggle("hidden", !show);
  }});
}}

function exportJson() {{
  const state = loadState();
  const blob = new Blob([JSON.stringify(state, null, 2)], {{ type: "application/json" }});
  const url = URL.createObjectURL(blob);
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 16);
  const a = document.createElement("a");
  a.href = url;
  a.download = "xfail-review-tags-" + ts + ".json";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}}

function resetAll() {{
  if (confirm("Clear all tags and notes? This cannot be undone.")) {{
    localStorage.removeItem(STORAGE_KEY);
    location.reload();
  }}
}}

document.addEventListener("change", e => {{
  onTagChange(e);
  onNotesChange(e);
}});
document.addEventListener("input", e => {{ onNotesChange(e); }});
document.getElementById("filter-fixture").addEventListener("change", applyFilters);
document.getElementById("filter-invariant").addEventListener("change", applyFilters);
document.getElementById("filter-tag").addEventListener("change", applyFilters);
document.getElementById("export-btn").addEventListener("click", exportJson);
document.getElementById("reset-btn").addEventListener("click", resetAll);

restore();
applyFilters();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
