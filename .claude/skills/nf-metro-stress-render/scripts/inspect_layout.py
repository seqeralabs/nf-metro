#!/usr/bin/env python3
"""Dump the laid-out geometry of a .mmd so a reported defect can be confirmed.

`probe_layout.py` answers "did anything trip a check?". This answers "where is
everything?" - the coordinates you need to turn an eyeballed report ("section 2
content is pulled too low", "that input floats too high") into a quantified,
credible bug ("`al_minimap` y=392 vs the trunk at y=216.8, a 175px drag").

For each section it prints the bbox extents and every station's (x, y) with a
PORT / OFF(-track) tag, then a few derived red flags:
  - stations that sit off their section's trunk (modal y of non-port stations)
  - off-track inputs/outputs more than ~one row from their nearest neighbour
  - the inter-section gaps between vertically-stacked sections

Usage:
    python inspect_layout.py INPUT.mmd
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
tests_dir = _REPO_ROOT / "tests"
if (tests_dir / "layout_validator.py").exists():
    sys.path.insert(0, str(tests_dir))

from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402
from nf_metro.parser.model import Station  # noqa: E402


def _is_port(st: Station) -> bool:
    return bool(getattr(st, "is_port", False))


def _is_off(st: Station) -> bool:
    return bool(getattr(st, "off_track", False))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    graph = parse_metro_mermaid(path.read_text(), max_station_columns=15)
    compute_layout(graph)

    print(f"=== {path.name} ===")

    # Per-section dump.
    boxes: list[tuple[float, float, int, int, str]] = []
    for sid, sec in graph.sections.items():
        x0, y0 = sec.bbox_x, sec.bbox_y
        x1, y1 = x0 + sec.bbox_w, y0 + sec.bbox_h
        boxes.append((y0, y1, sec.grid_col, sec.grid_row, sid))
        print(
            f"\n[{sid}] '{sec.name}' grid=({sec.grid_col},{sec.grid_row}) "
            f"box x:[{x0:.0f},{x1:.0f}] y:[{y0:.0f},{y1:.0f}]"
        )
        trunk_ys = [
            graph.stations[s].y
            for s in sec.station_ids
            if not _is_port(graph.stations[s]) and not _is_off(graph.stations[s])
        ]
        trunk_y = Counter(round(y, 1) for y in trunk_ys).most_common(1)
        trunk_y = trunk_y[0][0] if trunk_y else None
        for stid in sec.station_ids:
            st = graph.stations[stid]
            tag = "PORT" if _is_port(st) else ("OFF" if _is_off(st) else "")
            flag = ""
            if tag == "" and trunk_y is not None and abs(st.y - trunk_y) > 20:
                flag = f"  <-- OFF TRUNK by {st.y - trunk_y:+.0f} (trunk y={trunk_y})"
            print(f"    {stid:18s} x={st.x:7.1f} y={st.y:7.1f} {tag:4s}{flag}")

    # Vertically-stacked section gaps (same grid_col, adjacent grid_row).
    print("\n-- inter-row gaps (stacked sections, same column) --")
    by_col: dict[int, list[tuple[int, float, float, str]]] = {}
    for y0, y1, col, row, sid in boxes:
        by_col.setdefault(col, []).append((row, y0, y1, sid))
    flagged = False
    for col, rows in sorted(by_col.items()):
        rows.sort()
        for (r1, _, y1_bot, s1), (r2, y2_top, _, s2) in zip(rows, rows[1:]):
            gap = y2_top - y1_bot
            mark = "  <-- large vs siblings" if gap > 120 else ""
            print(
                f"   col {col}: {s1}(row{r1}) -> {s2}(row{r2})  gap={gap:.0f}px{mark}"
            )
            flagged = True
    if not flagged:
        print("   (no vertically-stacked sections)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
