"""Public entry point for parsing Mermaid graph definitions with %%metro directives.

This module is the driver: it turns source text into a list of typed statements
(via :mod:`nf_metro.parser.grammar`) and applies them in source order to build a
:class:`MetroGraph`, maintaining the enclosing-subgraph context exactly as the
source order implies, then runs the post-parse resolution.

The grammar lives in :mod:`nf_metro.parser.grammar`, directive parsing and
dispatch in :mod:`nf_metro.parser.directives`, and the post-parse graph rewrites
in :mod:`nf_metro.parser.resolve`.

Sections are defined as Mermaid subgraphs with %%metro entry/exit directives.
"""

from __future__ import annotations

import re
import warnings

from nf_metro.parser.directives import _apply_directive
from nf_metro.parser.grammar import (
    _Comment,
    _Directive,
    _Edge,
    _End,
    _GraphHeader,
    _Junk,
    _Node,
    _Statement,
    _Subgraph,
    _unquote,
    parse_statements,
)
from nf_metro.parser.model import Edge, MetroGraph, Section, Station
from nf_metro.parser.resolve import (
    _create_implicit_section,
    _insert_bypass_stations,
    _insert_terminus_convergence_stations,
    _remove_empty_sections,
    _resolve_sections,
)


def _check_unsupported_input(text: str) -> None:
    """Detect common unsupported input formats and raise helpful errors."""
    lines = text.strip().split("\n")
    has_flowchart = any(line.strip().startswith("flowchart ") for line in lines)
    has_metro_directives = any(line.strip().startswith("%%metro") for line in lines)

    if has_flowchart and not has_metro_directives:
        raise ValueError(
            "This looks like raw Nextflow DAG output (flowchart syntax "
            "without %%metro directives). Use 'nf-metro convert' to "
            "convert it to nf-metro format first, or pass "
            "'--from-nextflow' to 'nf-metro render'.\n\n"
            "See: https://pinin4fjords.github.io/nf-metro/latest/nextflow/"
        )

    if has_flowchart:
        raise ValueError(
            "Mermaid 'flowchart' syntax is not supported. "
            "Use 'graph LR' with %%metro directives instead.\n\n"
            "See the guide: "
            "https://pinin4fjords.github.io/nf-metro/latest/guide/"
        )


_GRAPH_DIRECTION_PATTERN = re.compile(r"^graph\s+([A-Za-z]{2})\b")


def _warn_if_non_lr_primary(graph_line: str) -> None:
    """Warn when the `graph` header declares a primary direction other than LR.

    nf-metro lays the section meta-graph out left-to-right; any other Mermaid
    direction (TB/TD/BT/RL) is not honoured. A bare ``graph`` (no direction,
    Mermaid-defaults to LR) is fine and warns nothing. Per-section
    flow is controlled separately by ``%%metro direction:`` and is unaffected.
    """
    m = _GRAPH_DIRECTION_PATTERN.match(graph_line)
    if not m:
        return
    direction = m.group(1).upper()
    if direction == "LR":
        return
    warnings.warn(
        f"nf-metro honours only 'graph LR' as the primary layout direction; "
        f"the declared direction '{direction}' was ignored and the map is laid "
        f"out left-to-right. Use per-section '%%metro direction:' directives to "
        f"control individual section flow.",
        stacklevel=2,
    )


def _validate_edge_annotations(graph: MetroGraph) -> None:
    """Validate that all edges have metro line annotations.

    Raises ValueError with a helpful message if any edge uses the default
    placeholder (meaning it had no |line_id| annotation in the source).
    """
    if not graph.edges:
        return

    bad_edges = []
    undeclared_lines = set()
    for edge in graph.edges:
        if edge.line_id == "default":
            bad_edges.append(edge)
        elif graph.lines and edge.line_id not in graph.lines:
            undeclared_lines.add(edge.line_id)

    if bad_edges:
        examples = []
        seen = set()
        for edge in bad_edges:
            key = (edge.source, edge.target)
            if key not in seen:
                seen.add(key)
                examples.append(f"  {edge.source} --> {edge.target}")
        raise ValueError(
            "Some edges have no metro line annotation. "
            "Every edge must specify which line(s) it belongs to "
            "using -->|line_id| syntax.\n\n"
            "Edges missing annotations:\n" + "\n".join(examples) + "\n\n"
            "Fix by adding line annotations, e.g.:\n"
            "  fastp -->|qc| falco\n\n"
            "Lines must also be declared with %%metro line: directives, e.g.:\n"
            "  %%metro line: qc | Quality Control | #4CAF50"
        )

    if undeclared_lines:
        raise ValueError(
            "Some edges reference undeclared metro lines: "
            + ", ".join(sorted(undeclared_lines))
            + "\n\n"
            "Declare each line with a %%metro line: directive, e.g.:\n"
            + "\n".join(
                f"  %%metro line: {lid} | {lid} | #hexcolor"
                for lid in sorted(undeclared_lines)
            )
        )


def parse_metro_mermaid(
    text: str, max_station_columns: int | None = None
) -> MetroGraph:
    """Parse a Mermaid graph definition with %%metro directives.

    ``max_station_columns`` is the row-wrap width supplied by the caller (the
    ``--max-layers-per-row`` CLI flag). When ``None``, a ``%%metro
    fold_threshold`` directive supplies the width, falling back to 15.
    """
    _check_unsupported_input(text)
    graph = MetroGraph()
    _apply_statements(parse_statements(text), graph)
    _finalize_graph(graph, max_station_columns)
    return graph


def _apply_statements(statements: list[_Statement], graph: MetroGraph) -> None:
    """Apply parsed statements in source order, tracking the enclosing section."""
    current_section_id: str | None = None

    for stmt in statements:
        if isinstance(stmt, _End):
            current_section_id = None
        elif isinstance(stmt, _Subgraph):
            graph.add_section(Section(id=stmt.section_id, name=stmt.name))
            current_section_id = stmt.section_id
        elif isinstance(stmt, _Directive):
            _apply_directive(stmt.key, stmt.value, graph, current_section_id)
        elif isinstance(stmt, _GraphHeader):
            _warn_if_non_lr_primary(stmt.line)
        elif isinstance(stmt, _Comment):
            continue
        elif isinstance(stmt, _Junk):
            warnings.warn(f"Ignored unrecognised line: {stmt.text!r}", stacklevel=2)
        elif isinstance(stmt, _Edge):
            _apply_edge(stmt, graph, current_section_id)
        elif isinstance(stmt, _Node):
            _apply_node(stmt.node_id, stmt.label, graph, current_section_id)


def _finalize_graph(graph: MetroGraph, max_station_columns: int | None) -> None:
    """Validate, run the post-parse resolution, and apply buffered metadata."""
    _validate_edge_annotations(graph)

    # Remove empty sections, create implicit section for loose stations,
    # auto-infer layout parameters, then resolve sections.
    if graph.sections:
        _remove_empty_sections(graph)

    if graph.sections:
        _create_implicit_section(graph)

        from nf_metro.layout.auto_layout import infer_section_layout

        # Row-wrap width precedence: an explicit caller value (the
        # --max-layers-per-row CLI flag) wins over a %%metro fold_threshold
        # directive, which in turn overrides the default of 15. Raising it
        # keeps a long horizontal trunk of sections on a single row.
        if max_station_columns is not None:
            eff_cols = max_station_columns
        elif graph.fold_threshold is not None:
            eff_cols = graph.fold_threshold
        else:
            eff_cols = 15
        infer_section_layout(graph, max_station_columns=eff_cols)
        _insert_terminus_convergence_stations(graph)
        _resolve_sections(graph)
        _insert_bypass_stations(graph)

    _apply_pending_metadata(graph)


def _apply_pending_metadata(graph: MetroGraph) -> None:
    """Apply terminus icons, off-track marks, and markers buffered during parse.

    Terminus icons skip mid-pipeline hub stations: a station the author tagged
    as a file/dir input but which both receives and forwards data. The file
    icon implies a path start or end, which is misleading on a routing hub, so
    only pure sources (no predecessors) and pure sinks (no successors) keep it.
    """
    for station_id, entries in graph._pending_terminus.items():
        station = graph.stations.get(station_id)
        if not station:
            continue
        if graph.edges_to(station_id) and graph.edges_from(station_id):
            continue
        station.terminus_labels = [label for label, _, _, _ in entries]
        station.terminus_icon_types = [icon_type for _, icon_type, _, _ in entries]
        station.terminus_names = [name for _, _, name, _ in entries]
        station.terminus_icon_banners = [banner for _, _, _, banner in entries]

    for station_id in graph._pending_off_track:
        station = graph.stations.get(station_id)
        if station:
            station.off_track = True

    for station_id, marker in graph._pending_markers.items():
        station = graph.stations.get(station_id)
        if station:
            station.marker = marker

    for station_id, pattern in graph._pending_process:
        if station_id in graph.stations:
            graph.process_mapping.setdefault(station_id, []).append(pattern)
        else:
            warnings.warn(
                f"%%metro process: unknown station id {station_id!r}; ignoring",
                stacklevel=2,
            )


def _apply_node(
    node_id: str,
    label: str,
    graph: MetroGraph,
    section_id: str | None = None,
) -> None:
    """Declare a node: register it (or set the label on a station an edge
    auto-created) from its id and (shape-stripped) label text."""
    label = _unquote(label.strip())
    # Convert literal \n sequences to real newlines (multi-line labels)
    if "\\n" in label:
        label = "\n".join(part.strip() for part in label.split("\\n"))
    if node_id not in graph.stations:
        graph.register_station(
            Station(
                id=node_id,
                label=label,
                section_id=section_id,
                is_hidden=node_id.startswith("_"),
            )
        )
    else:
        graph.stations[node_id].label = label
        graph.stations[node_id].is_hidden = node_id.startswith("_")
        if section_id and graph.stations[node_id].section_id is None:
            graph.stations[node_id].section_id = section_id
            if section_id in graph.sections:
                graph.sections[section_id].station_ids.append(node_id)


def _ensure_station(graph: MetroGraph, node_id: str, section_id: str | None) -> None:
    """Register a bare edge endpoint as a station if it doesn't exist yet.

    Unlike a node declaration, a bare endpoint provides no label, so an existing
    station's label is left untouched and a new one takes its id as its label.
    """
    if node_id not in graph.stations:
        graph.register_station(
            Station(
                id=node_id,
                label=node_id,
                section_id=section_id,
                is_hidden=node_id.startswith("_"),
            )
        )


def _apply_edge(edge: _Edge, graph: MetroGraph, section_id: str | None) -> None:
    """Register an edge and its endpoints (one ``Edge`` per line id).

    An endpoint written with an inline shape also declares that node with its
    label; a bare endpoint just ensures the station exists.
    """
    for node_id, node_label in (
        (edge.source, edge.source_label),
        (edge.target, edge.target_label),
    ):
        if node_label is not None:
            _apply_node(node_id, node_label, graph, section_id)
        else:
            _ensure_station(graph, node_id, section_id)

    for line_id in edge.line_ids:
        graph.add_edge(Edge(source=edge.source, target=edge.target, line_id=line_id))
