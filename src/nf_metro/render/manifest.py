"""nf-metro's graph adapter for the embedded-manifest standard.

The format, reader, matcher, and producer helpers live in the dependency-free
:mod:`nf_metro.manifest` package (built to be lifted into its own
distribution), whose vocabulary is tool-neutral: nodes, groups, regions.  This
module is the thin nf-metro-specific adapter: it maps a laid-out
:class:`~nf_metro.parser.model.MetroGraph` (stations, lines, sections, process
mappings) onto that neutral vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nf_metro.manifest import build_manifest_data
from nf_metro.render.constants import palette_pick

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

__all__ = ["build_manifest"]


def build_manifest(
    graph: MetroGraph,
    *,
    width: int,
    height: int,
    station_radius: float,
    line_colors: tuple[str, ...] = (),
    extra_node_data: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the manifest dict from the graph the renderer just laid out.

    Maps the metro graph onto the standard's neutral vocabulary - stations are
    nodes, lines are groups, sections are regions, and a station's process
    patterns are its node patterns - then hands plain data to the standalone
    :func:`~nf_metro.manifest.build_manifest_data`.  Coordinates and the process
    mapping are read from the same fields the live server uses
    (``graph.stations[].x/.y`` and ``graph.process_mapping``) so the embedded
    data cannot drift from the live behaviour.  ``width`` and ``height`` are the
    final canvas dimensions; ``station_radius`` is the single nominal marker
    radius.

    ``extra_node_data`` is an optional mapping from station id to additional
    fields (e.g. ``w``, ``h``, ``rx`` pill dimensions computed by the renderer)
    that are merged into each node entry before serialisation.

    Ports and hidden nodes are excluded.  Every other station is included --
    unmapped ones simply carry an empty ``patterns`` list -- so the manifest is
    a complete, future-proof inventory of addressable nodes rather than only the
    subset that lights up today.
    """
    drawn_sections = {sid: sec for sid, sec in graph.sections.items() if sec.draws_box}

    nodes: list[dict[str, Any]] = []
    for station in graph.stations.values():
        if station.is_port or station.is_hidden:
            continue
        entry: dict[str, Any] = {
            "id": station.id,
            "label": station.label or station.id,
            "x": station.x,
            "y": station.y,
            "r": station_radius,
            "groups": graph.station_lines(station.id),
            "patterns": list(graph.process_mapping.get(station.id, [])),
        }
        if station.section_id in drawn_sections:
            entry["region"] = station.section_id
        if extra_node_data and station.id in extra_node_data:
            entry.update(extra_node_data[station.id])
        nodes.append(entry)

    return build_manifest_data(
        title=graph.title,
        width=width,
        height=height,
        nodes=nodes,
        groups=[
            {
                "id": line.id,
                "label": line.display_name,
                "color": palette_pick(line_colors, line),
            }
            for line in graph.lines.values()
        ],
        regions=[{"id": sid, "label": sec.name} for sid, sec in drawn_sections.items()],
    )
