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
import re
from collections import Counter
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from nf_metro.parser.provenance import ConnectorEndpointRole, EffectiveDecision
from nf_metro.parser.route_topology import build_route_topology_query
from nf_metro.themes import resolve_style

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph
    from nf_metro.parser.route_topology import RouteConnector

__all__ = ["build_info", "format_info_json", "format_info_text", "station_kind"]

StationKind = Literal["station", "junction", "port", "bypass", "hidden", "unknown"]


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _decision_info(decision: EffectiveDecision[Any] | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "value": _json_value(decision.value),
        "origin": decision.origin.value,
        "state": decision.state.value,
        "locked": decision.is_reinference_locked,
        "reason": decision.reason.value,
        "authored_values": [_json_value(value) for value in decision.authored_values],
    }


def _connector_side_info(
    graph: MetroGraph,
    connector: RouteConnector,
    role: ConnectorEndpointRole,
) -> dict[str, Any]:
    endpoint = graph.layout_provenance.endpoint_key(connector.id, role)
    return {
        "connector_id": str(connector.id),
        "line_id": connector.line_id,
        "source": connector.source,
        "target": connector.target,
        "provenance": _decision_info(
            graph.layout_provenance.endpoint_decision(endpoint)
        ),
    }


def _inferred_summary(records: list[dict[str, Any]]) -> bool | None:
    ownership = {
        record["provenance"]["origin"] == "authored"
        for record in records
        if record["provenance"] is not None
    }
    if not ownership:
        return None
    if len(ownership) > 1:
        return None
    return not next(iter(ownership))


def station_kind(graph: MetroGraph, station_id: str) -> StationKind:
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

    ``style`` reports the theme name the map resolves to, which is the name
    ``--theme`` accepts: an alias or an unrecognised value is reported as the
    brand the render will actually use, not as authored.
    """
    real_sections = graph.real_sections
    topology_query = build_route_topology_query(graph)
    connector_sides: dict[tuple[str, ConnectorEndpointRole], list[dict[str, Any]]] = {}
    if topology_query is not None:
        for connector in topology_query.connectors:
            for section_id, role in (
                (connector.source_section, ConnectorEndpointRole.EXIT),
                (connector.target_section, ConnectorEndpointRole.ENTRY),
            ):
                connector_sides.setdefault((section_id, role), []).append(
                    _connector_side_info(graph, connector, role)
                )

    lines = []
    for lid, line in graph.lines.items():
        route_raw = graph.line_stations(lid)
        lines.append(
            {
                "id": lid,
                "display_name": line.display_name,
                "color": line.color,
                "style": line.style,
                # route_raw includes synthetic ports/junctions; route excludes them.
                "n_stations": len(route_raw),
                "route": [
                    sid for sid in route_raw if station_kind(graph, sid) == "station"
                ],
            }
        )

    sections = []
    for sid, sec in graph.sections.items():
        direction = graph.layout_provenance.direction_decision(sid)
        grid = graph.layout_provenance.grid_decision(sid)
        entry_sides = connector_sides.get((sid, ConnectorEndpointRole.ENTRY), [])
        exit_sides = connector_sides.get((sid, ConnectorEndpointRole.EXIT), [])
        sections.append(
            {
                "id": sid,
                "name": sec.name,
                "number": sec.number,
                "n_stations": len(sec.station_ids),
                "direction": sec.direction,
                "direction_inferred": (
                    direction is None or not direction.is_author_owned
                ),
                "direction_provenance": _decision_info(direction),
                "grid": {
                    "col": sec.grid_col,
                    "row": sec.grid_row,
                    "row_span": sec.grid_row_span,
                    "col_span": sec.grid_col_span,
                },
                "grid_inferred": grid is None or not grid.is_author_owned,
                "grid_provenance": _decision_info(grid),
                "is_implicit": sec.is_implicit,
                "stations": [
                    st for st in sec.station_ids if station_kind(graph, st) == "station"
                ],
                "entry_ports": list(sec.entry_ports),
                "exit_ports": list(sec.exit_ports),
                "entry_sides_inferred": _inferred_summary(entry_sides),
                "exit_sides_inferred": _inferred_summary(exit_sides),
                "entry_side_provenance": entry_sides,
                "exit_side_provenance": exit_sides,
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
        role = (
            ConnectorEndpointRole.ENTRY if port.is_entry else ConnectorEndpointRole.EXIT
        )
        side_provenance: list[dict[str, Any]] = []
        if topology_query is not None:
            for connector_id in topology_query.connector_ids_for_port(pid):
                endpoint = graph.layout_provenance.endpoint_key(connector_id, role)
                side_provenance.append(
                    {
                        "connector_id": str(connector_id),
                        "provenance": _decision_info(
                            graph.layout_provenance.endpoint_decision(endpoint)
                        ),
                    }
                )
        ports.append(
            {
                "id": pid,
                "section_id": port.section_id,
                "side": port.side.value,
                "is_entry": port.is_entry,
                "side_inferred": _inferred_summary(side_provenance),
                "side_provenance": side_provenance,
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
        "caption": graph.caption or None,
        "style": resolve_style(graph.style),
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
            "fold_threshold_provenance": _decision_info(
                graph.layout_provenance.fold_threshold_decision
            ),
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


def _format_inferred(inferred: bool | None) -> str:
    if inferred is None:
        return "mixed"
    return "inferred" if inferred else "authored"


#: PyYAML's own implicit-typing resolvers (``yaml.resolver.Resolver``),
#: reproduced verbatim rather than approximated with Python's ``float()``:
#: the two disagree on details ``float()`` is more permissive about, such as
#: where an underscore may sit in a run of digits (``1__000`` is a YAML int,
#: 1000, but not a valid Python float literal).
_YAML_BOOL = re.compile(
    r"yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF"
)
_YAML_NULL = re.compile(r"~|null|Null|NULL|")
_YAML_INT = re.compile(
    r"[-+]?0b[0-1_]+"
    r"|[-+]?0[0-7_]+"
    r"|[-+]?(?:0|[1-9][0-9_]*)"
    r"|[-+]?0x[0-9a-fA-F_]+"
    r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+"
)
_YAML_FLOAT = re.compile(
    r"[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
    r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*"
    r"|[-+]?\.(?:inf|Inf|INF)"
    r"|\.(?:nan|NaN|NAN)"
)
_YAML_TIMESTAMP = re.compile(
    r"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    r"|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?"
    r"(?:[Tt]|[ \t]+)[0-9][0-9]?"
    r":[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?"
    r"(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?"
)
#: The bare ``value`` and ``merge`` tags: neither starts with an indicator
#: character already blocked below, and neither is a bool/null/int/float/
#: timestamp, so they need their own entry.
_YAML_VALUE_OR_MERGE = re.compile(r"=|<<")
_YAML_IMPLICIT_TYPES = (
    _YAML_BOOL,
    _YAML_NULL,
    _YAML_INT,
    _YAML_FLOAT,
    _YAML_TIMESTAMP,
    _YAML_VALUE_OR_MERGE,
)


#: DEL, the C1 control range, and the Unicode line/paragraph separators:
#: YAML treats a raw occurrence of any of these as a literal line break even
#: inside a quoted scalar (some, like NEL U+0085, are instead silently folded
#: to a space), which can corrupt or desynchronise the surrounding document.
#: ``json.dumps`` only escapes plain ASCII control characters (below 0x20).
#: Built from ``chr()`` calls, not literal characters, so the source file
#: stays plain ASCII rather than embedding an invisible separator.
_YAML_UNSAFE_CHARS = frozenset(chr(c) for c in range(0x7F, 0xA0)) | {
    chr(0x2028),
    chr(0x2029),
}
_YAML_UNSAFE_QUOTED = re.compile("[" + "".join(_YAML_UNSAFE_CHARS) + "]")


def _is_plain_scalar(text: str) -> bool:
    """Whether *text* can be written unquoted and read back unchanged.

    Conservative on purpose: anything a parser might resolve to a number, a
    date, a boolean or null, and anything opening with an indicator
    character, is quoted instead. A colon disqualifies outright - it
    separates a key in a mapping, and ``12:30`` is a number in YAML 1.1.
    """
    if not text or text != text.strip():
        return False
    if text[0] in "-?:,[]{}#&*!|>'\"%@`" or ":" in text or "#" in text:
        return False
    if any(ord(char) < 0x20 for char in text) or any(
        char in _YAML_UNSAFE_CHARS for char in text
    ):
        return False
    return not any(pattern.fullmatch(text) for pattern in _YAML_IMPLICIT_TYPES)


def _yaml_quote(text: str) -> str:
    """JSON-quote *text* for use as a YAML scalar.

    ``ensure_ascii=False`` keeps an astral character (e.g. an emoji) as
    itself rather than a UTF-16 surrogate pair, which JSON parsers recombine
    but YAML's double-quoted scalar does not. The bytes JSON leaves raw that
    YAML still rejects are escaped afterwards.
    """
    quoted = json.dumps(text, ensure_ascii=False)
    return _YAML_UNSAFE_QUOTED.sub(lambda m: f"\\u{ord(m.group()):04x}", quoted)


def _yaml_scalar(value: object) -> str:
    """One YAML scalar, quoted only where a bare word would not survive."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return text if _is_plain_scalar(text) else _yaml_quote(text)


#: A key needing no quotes: no colon, no leading indicator character.
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/ ]*$")


def _yaml_key(key: object) -> str:
    """*key* as written, quoted when a bare word would not parse back."""
    text = str(key)
    plain = _PLAIN_KEY.fullmatch(text) and _is_plain_scalar(text)
    return text if plain else _yaml_quote(text)


def to_yaml(value: object, indent: int = 0) -> list[str]:
    """Render *value* as block-style YAML lines."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = _yaml_key(key)
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{name}:")
                lines.extend(to_yaml(item, indent + 1))
            elif isinstance(item, list) and item:
                lines.append(f"{pad}{name}:")
                lines.extend(to_yaml(item, indent + 1))
            elif isinstance(item, dict):
                lines.append(f"{pad}{name}: {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}{name}: []")
            else:
                lines.append(f"{pad}{name}: {_yaml_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = to_yaml(item, indent + 1)
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            elif isinstance(item, dict):
                lines.append(f"{pad}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}- []")
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def _info_summary(info: dict[str, Any]) -> dict[str, Any]:
    """The stable headline summary: title, counts, lines, sections."""
    summary: dict[str, Any] = {"title": info["title"]}
    if info.get("caption"):
        summary["caption"] = info["caption"]
    summary["style"] = info["style"]
    summary["counts"] = info["counts"]
    summary["lines"] = [
        {
            "name": line["display_name"],
            "color": line["color"],
            "stations": line["n_stations"],
        }
        for line in info["lines"]
    ]
    summary["sections"] = [
        {
            "number": sec["number"],
            "name": sec["name"],
            "stations": sec["n_stations"],
        }
        for sec in info["sections"]
    ]
    return summary


def _route_keys(lines: list[dict[str, Any]]) -> list[str]:
    """Route keys for *lines*, one per line, never colliding.

    ``%%metro line:`` only requires a unique id; two lines may share a
    display name, and ``routes:`` is a dict keyed by name for readability,
    so a plain ``{display_name: route}`` comprehension would silently drop
    one line's route on a collision. Disambiguated with the id only where
    a collision actually occurs, so the common case stays plain names.
    """
    names = [line["display_name"] for line in lines]
    counts = Counter(names)
    return [
        f"{line['display_name']} ({line['id']})"
        if counts[line["display_name"]] > 1
        else line["display_name"]
        for line in lines
    ]


def _info_detail(info: dict[str, Any]) -> dict[str, Any]:
    """What ``--verbose`` adds: warnings, the DAG, layout, routes, synthetics."""
    layout = info["layout"]
    return {
        "warnings": list(info["warnings"]),
        "section_dag": [
            {"from": edge["from"], "to": edge["to"], "lines": list(edge["lines"])}
            for edge in info["section_dag"]["edges"]
        ],
        "layout": {
            "rows": layout["rows"],
            "folded": layout["folded"],
            "sections_by_row": {
                str(row): list(ids) for row, ids in layout["sections_by_row"].items()
            },
        },
        "routes": dict(
            zip(
                _route_keys(info["lines"]),
                (list(line["route"]) for line in info["lines"]),
            )
        ),
        "section_detail": [
            {
                "number": sec["number"],
                "name": sec["name"],
                "box": "implicit" if sec["is_implicit"] else "explicit",
                "direction": sec["direction"],
                "direction_source": (
                    sec["direction_provenance"]["state"]
                    if sec["direction_provenance"]
                    else "unrecorded"
                ),
                "grid": f"{sec['grid']['col']},{sec['grid']['row']}",
                "grid_source": (
                    sec["grid_provenance"]["state"]
                    if sec["grid_provenance"]
                    else "unrecorded"
                ),
                "stations": list(sec["stations"]),
                "entry_ports": list(sec["entry_ports"]),
                "entry_sides": _format_inferred(sec["entry_sides_inferred"]),
                "exit_ports": list(sec["exit_ports"]),
                "exit_sides": _format_inferred(sec["exit_sides_inferred"]),
            }
            for sec in info["sections"]
        ],
        "ports": [
            {
                "id": port["id"],
                "kind": "entry" if port["is_entry"] else "exit",
                "side": port["side"],
                "side_source": _format_inferred(port["side_inferred"]),
                "section": port["section_id"],
            }
            for port in info["ports"]
        ],
        "junctions": list(info["junctions"]),
    }


def format_info_text(info: dict[str, Any], *, verbose: bool = False) -> str:
    """Render the introspection dict as YAML.

    The non-verbose form is the stable, headline summary (title, counts,
    per-line and per-section station counts). ``verbose`` appends the richer
    introspection: warnings, the section dependency graph, fold/row layout,
    per-line routes, per-section detail with inferred/authored provenance,
    and the synthetic ports and junctions.
    """
    document = _info_summary(info)
    if verbose:
        document.update(_info_detail(info))
    return "\n".join(to_yaml(document))
