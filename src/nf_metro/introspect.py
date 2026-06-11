"""Structured introspection of a parsed :class:`~nf_metro.parser.model.MetroGraph`.

This is the data behind ``nf-metro info``: a faithful answer to "what did
nf-metro actually build from my ``.mmd``?".  It surfaces three things a flat
station/edge count cannot:

* the section dependency graph and the ordered route of each line;
* synthetic elements the author never wrote (entry/exit ports, fan-out
  junctions) that ``_resolve_sections`` inserts;
* the defaults auto-layout inferred where the author was silent (section
  flow direction, port sides, grid placement and folding).

The embedded SVG manifest (:mod:`nf_metro.render.manifest`) is the render-time,
geometry-bearing consumer contract and deliberately *strips* these synthetic and
derived internals; this module is their author-facing counterpart.  Coordinates
are out of scope here -- render and read the manifest for laid-out geometry.

Everything reported is available immediately after
:func:`~nf_metro.parser.parse_metro_mermaid`, which runs auto-layout and section
resolution internally; no full layout pass is required.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

__all__ = ["build_info", "format_info_json", "format_info_text", "station_kind"]


def station_kind(graph: MetroGraph, station_id: str) -> str:
    """Classify a station as authored or synthetic.

    Returns one of ``"station"`` (an authored node), ``"junction"`` (a fan-out
    helper from ``_resolve_sections``), ``"port"`` (a section-boundary entry or
    exit point), ``"bypass"`` (a hidden V helper that routes a line clear of a
    station marker), or ``"hidden"`` (any other non-rendered helper).  Junctions
    are checked first because their stations also carry ``is_port=True``.
    """
    if station_id in graph.junction_ids:
        return "junction"
    station = graph.stations.get(station_id)
    if station is None:
        return "unknown"
    if station.is_port:
        return "port"
    if station.bypasses_station_id is not None:
        return "bypass"
    if station.is_hidden:
        return "hidden"
    return "station"


def build_info(graph: MetroGraph, warnings: list[str] | None = None) -> dict[str, Any]:
    """Assemble the structured introspection dict for a parsed graph.

    *warnings* are parse-time warning messages captured by the caller (the
    parser emits these via :mod:`warnings`); pass ``None`` for none.
    """
    real_sections = {
        sid: sec for sid, sec in graph.sections.items() if not sec.is_implicit
    }

    lines = []
    for lid, line in graph.lines.items():
        route_raw = graph.line_stations(lid)
        lines.append(
            {
                "id": lid,
                "display_name": line.display_name,
                "color": line.color,
                "style": line.style,
                # Raw membership count (includes synthetic ports/junctions on the
                # resolved chain) drives the human headline; ``route`` is the
                # authored stations only.
                "n_stations": len(route_raw),
                "route": [
                    sid for sid in route_raw if station_kind(graph, sid) == "station"
                ],
            }
        )

    sections = []
    for sid, sec in graph.sections.items():
        sections.append(
            {
                "id": sid,
                "name": sec.name,
                "number": sec.number,
                "n_stations": len(sec.station_ids),
                "direction": sec.direction,
                "direction_inferred": sid not in graph._explicit_directions,
                "grid": {
                    "col": sec.grid_col,
                    "row": sec.grid_row,
                    "row_span": sec.grid_row_span,
                    "col_span": sec.grid_col_span,
                },
                "grid_inferred": sid not in graph._explicit_grid,
                "is_implicit": sec.is_implicit,
                "stations": [
                    st
                    for st in sec.station_ids
                    if station_kind(graph, st) == "station"
                ],
                "entry_ports": list(sec.entry_ports),
                "exit_ports": list(sec.exit_ports),
                "entry_sides_inferred": sid not in graph._explicit_entry,
                "exit_sides_inferred": sid not in graph._explicit_exit,
            }
        )

    stations = []
    for sid, station in graph.stations.items():
        stations.append(
            {
                "id": sid,
                "label": station.label,
                "section_id": station.section_id,
                "kind": station_kind(graph, sid),
                "lines": graph.station_lines(sid),
                "off_track": station.off_track,
                "processes": list(graph.process_mapping.get(sid, [])),
            }
        )

    ports = []
    for pid, port in graph.ports.items():
        explicit_set = graph._explicit_entry if port.is_entry else graph._explicit_exit
        ports.append(
            {
                "id": pid,
                "section_id": port.section_id,
                "side": port.side.value,
                "is_entry": port.is_entry,
                "side_inferred": port.section_id not in explicit_set,
            }
        )

    dag = graph.section_dag
    dag_edges = []
    if dag is not None:
        for src, tgt in sorted(dag.section_edges):
            dag_edges.append(
                {
                    "from": src,
                    "to": tgt,
                    "lines": sorted(dag.edge_lines[(src, tgt)]),
                }
            )

    rows: dict[int, list[str]] = {}
    for sid, sec in real_sections.items():
        rows.setdefault(sec.grid_row, []).append(sid)

    return {
        "title": graph.title or None,
        "style": graph.style,
        "warnings": list(warnings or []),
        "counts": {
            "stations": len(graph.stations),
            "edges": len(graph.edges),
            "lines": len(graph.lines),
            "sections": len(graph.sections),
            "ports": len(graph.ports),
            "junctions": len(graph.junctions),
        },
        "lines": lines,
        "sections": sections,
        "stations": stations,
        "ports": ports,
        "junctions": sorted(graph.junctions),
        "section_dag": {"edges": dag_edges},
        "layout": {
            "rows": len(rows),
            "folded": len(rows) > 1,
            "sections_by_row": {
                str(row): sorted(ids) for row, ids in sorted(rows.items())
            },
        },
    }


def format_info_json(info: dict[str, Any]) -> str:
    """Serialize the introspection dict as indented JSON."""
    return json.dumps(info, indent=2)


def _format_inferred(inferred: bool) -> str:
    return "inferred" if inferred else "explicit"


def format_info_text(info: dict[str, Any], *, verbose: bool = False) -> str:
    """Render the introspection dict as human-readable text.

    The non-verbose form is the stable, headline summary (title, counts,
    per-line and per-section station counts).  ``verbose`` appends the richer
    introspection: warnings, the section dependency graph, fold/row layout,
    ordered per-line routes, per-section detail with inferred/explicit flags,
    and the synthetic ports and junctions.
    """
    out: list[str] = []
    out.append(f"Title: {info['title'] or '(none)'}")
    out.append(f"Style: {info['style']}")
    counts = info["counts"]
    out.append(f"Stations: {counts['stations']}")
    out.append(f"Edges: {counts['edges']}")
    out.append(f"Lines: {counts['lines']}")
    for line in info["lines"]:
        out.append(
            f"  {line['display_name']} ({line['color']}): "
            f"{line['n_stations']} stations"
        )
    out.append(f"Sections: {counts['sections']}")
    for sec in info["sections"]:
        out.append(f"  [{sec['number']}] {sec['name']}: {sec['n_stations']} stations")

    if not verbose:
        return "\n".join(out)

    out.append("")
    out.append("Warnings:")
    if info["warnings"]:
        for warning in info["warnings"]:
            out.append(f"  - {warning}")
    else:
        out.append("  (none)")

    out.append("")
    out.append("Section dependency graph:")
    if info["section_dag"]["edges"]:
        for edge in info["section_dag"]["edges"]:
            out.append(
                f"  {edge['from']} -> {edge['to']} [{', '.join(edge['lines'])}]"
            )
    else:
        out.append("  (no inter-section edges)")

    layout = info["layout"]
    fold = "folded" if layout["folded"] else "single row"
    out.append("")
    out.append(f"Layout: {layout['rows']} row(s), {fold}")
    for row, ids in layout["sections_by_row"].items():
        out.append(f"  row {row}: {', '.join(ids)}")

    out.append("")
    out.append("Per-line routes:")
    for line in info["lines"]:
        route = " -> ".join(line["route"]) if line["route"] else "(empty)"
        out.append(f"  {line['display_name']}: {route}")

    out.append("")
    out.append("Sections (detail):")
    for sec in info["sections"]:
        tags = [
            "implicit" if sec["is_implicit"] else "explicit-box",
            f"{sec['direction']} ({_format_inferred(sec['direction_inferred'])})",
            f"grid {sec['grid']['col']},{sec['grid']['row']} "
            f"({_format_inferred(sec['grid_inferred'])})",
        ]
        out.append(f"  [{sec['number']}] {sec['name']}: {', '.join(tags)}")
        if sec["stations"]:
            out.append(f"      stations: {', '.join(sec['stations'])}")
        if sec["entry_ports"]:
            out.append(
                f"      entry ports ({_format_inferred(sec['entry_sides_inferred'])}): "
                f"{', '.join(sec['entry_ports'])}"
            )
        if sec["exit_ports"]:
            out.append(
                f"      exit ports ({_format_inferred(sec['exit_sides_inferred'])}): "
                f"{', '.join(sec['exit_ports'])}"
            )

    out.append("")
    out.append("Ports (synthetic):")
    if info["ports"]:
        for port in info["ports"]:
            kind = "entry" if port["is_entry"] else "exit"
            out.append(
                f"  {port['id']}: {kind} {port['side']} "
                f"({_format_inferred(port['side_inferred'])}) in {port['section_id']}"
            )
    else:
        out.append("  (none)")

    out.append("")
    out.append("Junctions (synthetic):")
    if info["junctions"]:
        for jid in info["junctions"]:
            out.append(f"  {jid}")
    else:
        out.append("  (none)")

    return "\n".join(out)
