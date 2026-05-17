"""Dump baseline station/port coordinates produced by nf_metro.compute_layout.

Used by the constraint-solver spike to compare against solver output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from nf_metro.layout.engine import compute_layout
from nf_metro.parser.mermaid import parse_metro_mermaid


def dump(path: Path) -> dict:
    text = path.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph, validate=False)
    sections = {}
    for sid, s in graph.sections.items():
        sections[sid] = {
            "name": s.name,
            "grid_col": s.grid_col,
            "grid_row": s.grid_row,
            "bbox_x": s.bbox_x,
            "bbox_y": s.bbox_y,
            "bbox_w": s.bbox_w,
            "bbox_h": s.bbox_h,
            "direction": s.direction,
            "station_ids": list(s.station_ids),
            "entry_ports": list(s.entry_ports),
            "exit_ports": list(s.exit_ports),
        }
    stations = {}
    for sid, st in graph.stations.items():
        stations[sid] = {
            "label": st.label,
            "x": st.x,
            "y": st.y,
            "layer": st.layer,
            "track": st.track,
            "is_port": st.is_port,
            "section_id": st.section_id,
        }
    ports = {}
    for pid, p in graph.ports.items():
        ports[pid] = {
            "section_id": p.section_id,
            "side": p.side.value,
            "is_entry": p.is_entry,
            "x": p.x,
            "y": p.y,
            "line_ids": list(p.line_ids),
        }
    edges = [
        {"source": e.source, "target": e.target, "line_id": e.line_id}
        for e in graph.edges
    ]
    return {
        "title": graph.title,
        "sections": sections,
        "stations": stations,
        "ports": ports,
        "edges": edges,
    }


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        out = dump(Path(arg))
        print(json.dumps(out, indent=2, default=str))
        print("---")
