"""Parser for Mermaid graph definitions with %%metro directives.

Uses a simple line-by-line approach rather than a full grammar parser,
since the Mermaid subset we need is straightforward.

Sections are defined as Mermaid subgraphs with %%metro entry/exit directives.
"""

from __future__ import annotations

import re
import warnings

from nf_metro.parser.model import (
    VALID_ICON_TYPES,
    VALID_LINE_STYLES,
    Edge,
    MetroGraph,
    MetroLine,
    Port,
    PortSide,
    Section,
    Station,
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


def parse_metro_mermaid(text: str, max_station_columns: int = 15) -> MetroGraph:
    """Parse a Mermaid graph definition with %%metro directives."""
    _check_unsupported_input(text)

    graph = MetroGraph()
    lines = text.strip().split("\n")

    current_section_id: str | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Subgraph end
        if stripped == "end":
            current_section_id = None
            continue

        # Subgraph start
        subgraph_m = _SUBGRAPH_PATTERN.match(stripped)
        if subgraph_m:
            section_id = subgraph_m.group(1)
            display_name = subgraph_m.group(2) or section_id
            section = Section(id=section_id, name=display_name.strip())
            graph.add_section(section)
            current_section_id = section_id
            continue

        # Metro directives
        if stripped.startswith("%%metro"):
            _parse_directive(stripped, graph, current_section_id)
            continue

        # Skip regular comments and graph declaration
        if stripped.startswith("%%") or stripped.startswith("graph "):
            continue

        # Try edge first (contains arrow)
        if "-->" in stripped or "---" in stripped or "==>" in stripped:
            _parse_edge(stripped, graph, current_section_id)
            continue

        # Try node definition
        _parse_node(stripped, graph, current_section_id)

    # Validate edges before layout
    _validate_edge_annotations(graph)

    # Post-parse: remove empty sections, create implicit section for loose
    # stations, auto-infer layout parameters, then resolve sections
    if graph.sections:
        _remove_empty_sections(graph)

    if graph.sections:
        _create_implicit_section(graph)

        from nf_metro.layout.auto_layout import infer_section_layout

        infer_section_layout(graph, max_station_columns=max_station_columns)
        _insert_terminus_convergence_stations(graph)
        _resolve_sections(graph)
        _insert_bypass_stations(graph)

    # Apply pending terminus designations
    for station_id, entries in graph._pending_terminus.items():
        station = graph.stations.get(station_id)
        if station:
            station.terminus_labels = [label for label, _, _ in entries]
            station.terminus_icon_types = [icon_type for _, icon_type, _ in entries]
            station.terminus_names = [name for _, _, name in entries]

    # Apply pending off-track marks
    for station_id in graph._pending_off_track:
        station = graph.stations.get(station_id)
        if station:
            station.off_track = True

    return graph


# Subgraph pattern: subgraph id [Display Name]
_SUBGRAPH_PATTERN = re.compile(r"^subgraph\s+(\w+)\s*(?:\[(.+?)\])?\s*$")


def _parse_directive(
    line: str,
    graph: MetroGraph,
    current_section_id: str | None = None,
) -> None:
    """Parse a %%metro directive line."""
    content = line[len("%%metro") :].strip()

    if content.startswith("title:"):
        graph.title = content[len("title:") :].strip()
    elif content.startswith("style:"):
        graph.style = content[len("style:") :].strip()
    elif content.startswith("line_order:"):
        order = content[len("line_order:") :].strip().lower()
        if order in ("definition", "span"):
            graph.line_order = order
    elif content.startswith("line:"):
        parts = content[len("line:") :].strip().split("|")
        if len(parts) >= 3:
            style = "solid"
            if len(parts) >= 4:
                raw_style = parts[3].strip().lower()
                if raw_style in VALID_LINE_STYLES:
                    style = raw_style
            graph.add_line(
                MetroLine(
                    id=parts[0].strip(),
                    display_name=parts[1].strip(),
                    color=parts[2].strip(),
                    style=style,
                )
            )
    elif content.startswith("entry:"):
        if current_section_id:
            _parse_port_hint(content, graph, current_section_id, is_entry=True)
    elif content.startswith("exit:"):
        if current_section_id:
            _parse_port_hint(content, graph, current_section_id, is_entry=False)
    elif content.startswith("direction:"):
        if current_section_id and current_section_id in graph.sections:
            direction = content[len("direction:") :].strip().upper()
            if direction in ("LR", "RL", "TB"):
                graph.sections[current_section_id].direction = direction
                graph._explicit_directions.add(current_section_id)
    elif content.startswith("grid:"):
        _parse_grid_directive(content, graph)
    elif content.startswith("logo:"):
        graph.logo_path = content[len("logo:") :].strip()
    elif content.startswith("compact_offsets:"):
        val = content[len("compact_offsets:") :].strip().lower()
        graph.compact_offsets = val in ("true", "yes", "1")
    elif content.startswith("legend_min_height:"):
        try:
            graph.legend_min_height = float(
                content[len("legend_min_height:") :].strip()
            )
        except ValueError:
            pass
    elif content.startswith("legend:"):
        pos = content[len("legend:") :].strip().lower()
        if pos in ("bl", "br", "tl", "tr", "bottom", "right", "none"):
            graph.legend_position = pos
    elif content.startswith("off_track:"):
        ids = [s.strip() for s in content[len("off_track:") :].split(",")]
        graph._pending_off_track.extend(sid for sid in ids if sid)
    elif ":" in content and content.split(":", 1)[0] in VALID_ICON_TYPES:
        icon_type, rest = content.split(":", 1)
        parts = rest.strip().split("|")
        if len(parts) >= 2:
            station_id = parts[0].strip()
            raw_labels = parts[1].strip()
            labels = [s.strip() for s in raw_labels.split(",") if s.strip()]
            # Optional third field: human-readable caption rendered below the
            # icon. A single name applies to all labels from this directive.
            name = parts[2].strip() if len(parts) >= 3 else ""
            graph._pending_terminus.setdefault(station_id, []).extend(
                (label, icon_type, name) for label in labels
            )


def _parse_port_hint(
    content: str,
    graph: MetroGraph,
    section_id: str,
    is_entry: bool,
) -> None:
    """Parse %%metro entry:/exit: and store as a hint on the Section.

    Does NOT create Port objects - those are created later in _resolve_sections
    based on actual inter-section edges.
    """
    prefix = "entry:" if is_entry else "exit:"
    rest = content[len(prefix) :].strip()
    parts = rest.split("|")
    if len(parts) < 2:
        return

    side_str = parts[0].strip().lower()
    side_map = {
        "left": PortSide.LEFT,
        "right": PortSide.RIGHT,
        "top": PortSide.TOP,
        "bottom": PortSide.BOTTOM,
    }
    side = side_map.get(side_str)
    if side is None:
        return

    line_ids = [lid.strip() for lid in parts[1].strip().split(",") if lid.strip()]

    section = graph.sections.get(section_id)
    if section:
        if is_entry:
            section.entry_hints.append((side, line_ids))
        else:
            section.exit_hints.append((side, line_ids))


def _parse_grid_directive(content: str, graph: MetroGraph) -> None:
    """Parse %%metro grid: section_id | col,row[,rowspan[,colspan]] directive."""
    rest = content[len("grid:") :].strip()
    parts = rest.split("|")
    if len(parts) < 2:
        return

    section_id = parts[0].strip()
    coords = parts[1].strip().split(",")
    if len(coords) < 2:
        return

    try:
        col = int(coords[0].strip())
        row = int(coords[1].strip())
        rowspan = int(coords[2].strip()) if len(coords) >= 3 else 1
        colspan = int(coords[3].strip()) if len(coords) >= 4 else 1
    except ValueError:
        return
    graph.grid_overrides[section_id] = (col, row, rowspan, colspan)
    graph._explicit_grid.add(section_id)


# Regex patterns for node shapes
_NODE_PATTERNS = [
    # stadium: node_id([label])
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\(\[(.+?)\]\)$"),
    # subroutine: node_id[[label]]
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\[\[(.+?)\]\]$"),
    # circle: node_id((label))
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\(\((.+?)\)\)$"),
    # square bracket: node_id[label]
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\[(.+?)\]$"),
    # round bracket: node_id(label)
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\((.+?)\)$"),
    # rhombus: node_id{label}
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\{(.+?)\}$"),
    # bare id
    re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)$"),
]

# Edge pattern: source -->|label| target  or  source --> target
_EDGE_PATTERN = re.compile(
    r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*"  # source
    r"(-->|---|==>)"  # arrow
    r"(?:\|([^|]*)\|)?\s*"  # optional |label|
    r"([a-zA-Z_][a-zA-Z0-9_]*)$"  # target
)


def _parse_node(
    line: str,
    graph: MetroGraph,
    section_id: str | None = None,
) -> None:
    """Parse a node definition line."""
    for pattern in _NODE_PATTERNS:
        m = pattern.match(line)
        if m:
            node_id = m.group(1)
            label = m.group(2).strip() if m.lastindex >= 2 else node_id
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
                # Update label if station was auto-created from an edge
                graph.stations[node_id].label = label
                graph.stations[node_id].is_hidden = node_id.startswith("_")
                # Also set section if not yet set
                if section_id and graph.stations[node_id].section_id is None:
                    graph.stations[node_id].section_id = section_id
                    if section_id in graph.sections:
                        graph.sections[section_id].station_ids.append(node_id)
            return


def _parse_edge(
    line: str,
    graph: MetroGraph,
    section_id: str | None = None,
) -> None:
    """Parse an edge definition line.

    Supports comma-separated line IDs: a -->|line1,line2,line3| b
    Creates a separate Edge for each line ID.
    """
    m = _EDGE_PATTERN.match(line)
    if not m:
        return

    source = m.group(1)
    label = m.group(3).strip() if m.group(3) else "default"
    target = m.group(4)

    # Ensure stations exist
    if source not in graph.stations:
        graph.register_station(
            Station(
                id=source,
                label=source,
                section_id=section_id,
                is_hidden=source.startswith("_"),
            )
        )
    if target not in graph.stations:
        graph.register_station(
            Station(
                id=target,
                label=target,
                section_id=section_id,
                is_hidden=target.startswith("_"),
            )
        )

    # Split comma-separated line IDs
    line_ids = [lid.strip() for lid in label.split(",")]
    for line_id in line_ids:
        graph.add_edge(Edge(source=source, target=target, line_id=line_id))


def _remove_empty_sections(graph: MetroGraph) -> None:
    """Remove sections that have no stations.

    Sections can end up empty when a subgraph contains only edges referencing
    nodes defined elsewhere. Empty sections cause layout failures.
    """
    empty_ids = [sid for sid, sec in graph.sections.items() if not sec.station_ids]
    for sid in empty_ids:
        del graph.sections[sid]
        warnings.warn(
            f"Section '{sid}' has no node definitions and was ignored. "
            f"Nodes must be defined inside a subgraph (not just referenced "
            f"in edges) to become members of that section.",
            stacklevel=2,
        )


def _create_implicit_section(graph: MetroGraph) -> None:
    """Create an implicit section for stations not in any explicit section.

    When some stations are in sections and others are not, the layout engine
    only positions sectioned stations. This creates an invisible section for
    the remaining 'loose' stations so they participate in layout.
    """
    loose = [
        s for s in graph.stations.values() if s.section_id is None and not s.is_port
    ]
    if not loose:
        return

    implicit = Section(id="__implicit__", name="", is_implicit=True)
    for s in loose:
        s.section_id = "__implicit__"
        implicit.station_ids.append(s.id)
    graph.add_section(implicit)


def _insert_terminus_convergence_stations(graph: MetroGraph) -> None:
    """Insert virtual convergence stations before multi-source termini.

    When a terminus station (a file/files/dir output) has 2+ direct
    inbound edges from distinct sources, the layout typically places
    the sources at different Ys and routes diagonals into the terminus
    marker, producing a Y-shaped converge AT the icon.  Inserting a
    hidden convergence station between the sources and the terminus
    forces the layout to allocate a column for the converge, so the
    diagonals meet there and the final segment to the terminus marker
    is a clean horizontal/vertical line.

    The convergence station inherits the terminus's section.  It is
    marked ``is_hidden`` so the renderer skips its label and marker.
    For each inbound edge ``source -> terminus`` carrying line ``L``,
    the edge is replaced with ``source -> converge`` followed by a
    single ``converge -> terminus`` edge per distinct line.
    """
    pending_terminus = graph._pending_terminus
    if not pending_terminus:
        return

    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
    converge_count = 0

    for terminus_id in list(pending_terminus.keys()):
        # Find direct inbound edges and their sources.
        inbound: list[tuple[int, Edge]] = []
        for i, edge in enumerate(graph.edges):
            if edge.target == terminus_id:
                inbound.append((i, edge))
        if not inbound:
            continue
        sources = {e.source for _, e in inbound}
        if len(sources) < 2:
            continue

        terminus = graph.stations.get(terminus_id)
        if terminus is None:
            continue

        converge_count += 1
        converge_id = f"__converge_{terminus_id}_{converge_count}"
        new_stations.append(
            Station(
                id=converge_id,
                label="",
                section_id=terminus.section_id,
                is_hidden=True,
            )
        )

        # Replace each ``src -> terminus (line)`` with
        # ``src -> converge (line)`` and add ``converge -> terminus (line)``.
        seen_lines: set[str] = set()
        for idx, edge in inbound:
            edges_to_remove.add(idx)
            new_edges.append(
                Edge(source=edge.source, target=converge_id, line_id=edge.line_id)
            )
            if edge.line_id not in seen_lines:
                seen_lines.add(edge.line_id)
                new_edges.append(
                    Edge(source=converge_id, target=terminus_id, line_id=edge.line_id)
                )

    if not new_stations:
        return

    for st in new_stations:
        graph.register_station(st)

    if edges_to_remove:
        graph.edges = [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
    for edge in new_edges:
        graph.add_edge(edge)


def _insert_bypass_stations(graph: MetroGraph) -> None:
    """Insert virtual stations so non-consumed lines bypass intermediate stops.

    When a station S sits between a predecessor P and one or more
    downstream targets, lines that flow ``P -> downstream`` but are
    *not* consumed by S would otherwise route straight through S's
    column at S's trunk Y, crashing into the marker.  By inserting a
    hidden virtual station ``V`` in the same section at S's column
    (i.e. one layer past P, same as S), the routing engine treats V
    just like any other column-mate of S: the line gets a Y track of
    its own and fans diagonally to/from the trunk exactly like a
    regular parallel branch.

    Detection (per section):

      * Compute longest-path layers within the section subgraph.
      * For each non-port station S in the section, gather
        ``consumed_lines(S)`` = ``{e.line_id for e in inbound edges
        to S}``.
      * For each predecessor ``P -> S`` and each other outbound
        edge ``P -> T`` (T != S), the lines on ``P -> T`` that are
        *not* in ``consumed_lines(S)`` and whose path traverses S's
        column (``layer(P) < layer(S) <= layer(T)``) are bypassers.

    Rewrite:
      * Add ``V`` (``id=f"__bypass_{S}_{P}"``, ``is_hidden=True``).
      * For each bypassed line L, replace edge ``P -> T (L)`` with
        ``P -> V (L)`` + ``V -> T (L)``.

    The virtual station inherits S's section so it participates in
    section layout / fan / symfan with the same primitives the rest
    of the section uses.  Cross-section targets (where T is in a
    different section) participate too because section resolution
    happens after this pass.
    """
    if not graph.sections:
        return

    import networkx as nx

    pending_terminus_ids: set[str] = set(graph._pending_terminus.keys())

    consumed_by: dict[str, set[str]] = {}
    for edge in graph.edges:
        consumed_by.setdefault(edge.target, set()).add(edge.line_id)

    edges_by_source: dict[str, list[tuple[int, Edge]]] = {}
    for i, edge in enumerate(graph.edges):
        edges_by_source.setdefault(edge.source, []).append((i, edge))

    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
    bypass_count = 0

    def _section_layers(section_ids: set[str]) -> dict[str, int]:
        sub = nx.DiGraph()
        for sid in section_ids:
            sub.add_node(sid)
        for edge in graph.edges:
            if edge.source in section_ids and edge.target in section_ids:
                sub.add_edge(edge.source, edge.target)
        try:
            topo = list(nx.topological_sort(sub))
        except nx.NetworkXUnfeasible:
            return {}
        layers: dict[str, int] = {}
        for node in topo:
            preds = list(sub.predecessors(node))
            layers[node] = (
                max((layers[p] for p in preds), default=-1) + 1 if preds else 0
            )
        return layers

    for section in graph.sections.values():
        station_ids = set(section.station_ids)
        if not station_ids:
            continue

        sec_layers = _section_layers(station_ids)
        exit_port_ids = set(section.exit_ports)
        # Pin exit ports past every internal station so longest-path layering
        # doesn't tie an exit port with an internal station sharing its
        # predecessor (which would suppress the bypass trigger).
        if exit_port_ids and sec_layers:
            internal_max = max(
                v for k, v in sec_layers.items() if k not in exit_port_ids
            )
            for pid in exit_port_ids:
                if sec_layers.get(pid, 0) <= internal_max:
                    sec_layers[pid] = internal_max + 1

        for sid in section.station_ids:
            station = graph.stations.get(sid)
            if station is None or station.is_port or station.is_hidden:
                continue
            if station.is_terminus or sid in pending_terminus_ids:
                continue

            consumed = consumed_by.get(sid, set())
            s_layer = sec_layers.get(sid)
            if s_layer is None:
                continue

            pred_ids: set[str] = {
                edge.source
                for edge in graph.edges
                if edge.target == sid and edge.source in station_ids
            }

            for pred_id in pred_ids:
                pred_layer = sec_layers.get(pred_id)
                if pred_layer is None or pred_layer >= s_layer:
                    continue

                # Require shared lines on P->exit and P->S so the bypass only
                # fires when the non-consumed line would route at S's trunk Y.
                # Without this guard, branch stations whose bypass line and
                # consumed-line bundle never share a row get over-detoured.
                p_exit_lines: dict[str, set[str]] = {}
                for _, edge in edges_by_source.get(pred_id, []):
                    if edge.target in exit_port_ids:
                        p_exit_lines.setdefault(edge.target, set()).add(edge.line_id)

                bypass_edges: list[tuple[int, Edge]] = []
                for i, edge in edges_by_source.get(pred_id, []):
                    if edge.target == sid or edge.line_id in consumed:
                        continue
                    if edge.target not in exit_port_ids:
                        continue
                    t_layer = sec_layers.get(edge.target)
                    if t_layer is None or t_layer <= s_layer:
                        continue
                    if not (p_exit_lines.get(edge.target, set()) & consumed):
                        continue
                    bypass_edges.append((i, edge))

                if not bypass_edges:
                    continue

                bypass_count += 1
                v_id = f"__bypass_{sid}_{pred_id}_{bypass_count}"
                new_stations.append(
                    Station(
                        id=v_id,
                        label="",
                        section_id=section.id,
                        is_hidden=True,
                    )
                )

                for idx, edge in bypass_edges:
                    edges_to_remove.add(idx)
                    new_edges.append(
                        Edge(source=edge.source, target=v_id, line_id=edge.line_id)
                    )
                    new_edges.append(
                        Edge(source=v_id, target=edge.target, line_id=edge.line_id)
                    )

    if not new_stations:
        return

    for st in new_stations:
        graph.register_station(st)

    if edges_to_remove:
        graph.edges = [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
    for edge in new_edges:
        graph.add_edge(edge)


def _resolve_sections(graph: MetroGraph) -> None:
    """Post-parse: classify edges, create ports, rewrite inter-section edges.

    Key design: ONE exit port per source section. All lines leaving a section
    exit together, ensuring consistent ordering. Junctions handle fan-out
    to multiple target sections. ONE entry port per target section per side
    (side from hints or LEFT default).
    """
    entry_side_for_line = _build_entry_side_mapping(graph)
    internal_edges, inter_section_edges = _classify_edges(graph)

    if inter_section_edges:
        _create_ports_and_junctions(
            graph, internal_edges, inter_section_edges, entry_side_for_line
        )
        _insert_merge_junctions(graph)

    _assign_section_numbers(graph)


def _assign_section_numbers(graph: MetroGraph) -> None:
    """Assign sequential numbers to sections that don't already have one."""
    for i, section in enumerate(graph.sections.values()):
        if section.number == 0:
            section.number = i + 1


def _natural_entry_side(direction: str) -> PortSide:
    """Return the natural entry side for a section's flow direction."""
    if direction == "RL":
        return PortSide.RIGHT
    if direction == "TB":
        return PortSide.TOP
    return PortSide.LEFT  # LR default


def _build_entry_side_mapping(
    graph: MetroGraph,
) -> dict[tuple[str, str], PortSide]:
    """Build per-line entry side lookup from explicit entry hints.

    Each section gets a single entry side.  When all hints agree on one
    side, that side is used.  When hints specify multiple sides, they
    are collapsed to the natural entry for the section's flow direction
    (LEFT for LR, RIGHT for RL, TOP for TB) so all lines share one
    entry port.

    Returns dict mapping (section_id, line_id) -> PortSide.
    """
    entry_side_for_line: dict[tuple[str, str], PortSide] = {}
    for sec_id, section in graph.sections.items():
        if not section.entry_hints:
            continue
        unique_sides = {s for s, _ in section.entry_hints}
        if len(unique_sides) == 1:
            side = unique_sides.pop()
        else:
            side = _natural_entry_side(section.direction)
        all_lines: set[str] = set()
        for _hint_side, line_ids in section.entry_hints:
            all_lines.update(line_ids)
        for lid in all_lines:
            entry_side_for_line[(sec_id, lid)] = side
    return entry_side_for_line


def _classify_edges(
    graph: MetroGraph,
) -> tuple[list[Edge], list[Edge]]:
    """Separate edges into internal and inter-section categories.

    Internal edges stay within a single section. Inter-section edges
    cross section boundaries and need port/junction rewriting.
    Also populates section.internal_edges for each section.

    Returns (internal_edges, inter_section_edges).
    """
    internal_edges: list[Edge] = []
    inter_section_edges: list[Edge] = []

    for edge in graph.edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)

        if src_sec and tgt_sec and src_sec != tgt_sec:
            inter_section_edges.append(edge)
        else:
            internal_edges.append(edge)
            sec_id = src_sec or tgt_sec
            if sec_id and sec_id in graph.sections:
                graph.sections[sec_id].internal_edges.append(edge)

    return internal_edges, inter_section_edges


def _determine_exit_sides(
    graph: MetroGraph,
) -> dict[str, PortSide]:
    """Map each section to its exit side based on exit hints."""
    section_exit_side: dict[str, PortSide] = {}
    for sec_id, section in graph.sections.items():
        unique_sides = {side for side, _line_ids in section.exit_hints}
        if len(unique_sides) == 1:
            section_exit_side[sec_id] = unique_sides.pop()
        else:
            section_exit_side[sec_id] = PortSide.RIGHT
    return section_exit_side


def _group_inter_section_edges(
    graph: MetroGraph,
    inter_section_edges: list[Edge],
    entry_side_for_line: dict[tuple[str, str], PortSide],
) -> tuple[dict[str, list[Edge]], dict[tuple[str, PortSide], list[Edge]]]:
    """Group inter-section edges by exit section and (entry section, side)."""
    exit_group_edges: dict[str, list[Edge]] = {}
    entry_group_edges: dict[tuple[str, PortSide], list[Edge]] = {}

    for edge in inter_section_edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)
        entry_side = entry_side_for_line.get((tgt_sec, edge.line_id), PortSide.LEFT)

        exit_group_edges.setdefault(src_sec, []).append(edge)
        entry_group_edges.setdefault((tgt_sec, entry_side), []).append(edge)

    return exit_group_edges, entry_group_edges


def _create_port_stations(
    graph: MetroGraph,
    exit_group_edges: dict[str, list[Edge]],
    entry_group_edges: dict[tuple[str, PortSide], list[Edge]],
    section_exit_side: dict[str, PortSide],
) -> tuple[dict[str, str], dict[tuple[str, PortSide], str], int]:
    """Create exit and entry port stations on the graph.

    Returns (exit_port_map, entry_port_map, next_port_counter).
    """
    port_counter = 0
    exit_port_map: dict[str, str] = {}

    for sec_id, edges in exit_group_edges.items():
        side = section_exit_side.get(sec_id, PortSide.RIGHT)
        all_line_ids = sorted({e.line_id for e in edges})
        port_id = f"{sec_id}__exit_{side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=sec_id,
            side=side,
            line_ids=all_line_ids,
            is_entry=False,
        )
        graph.add_port(port)
        exit_port_map[sec_id] = port_id
        port_counter += 1

    entry_port_map: dict[tuple[str, PortSide], str] = {}

    for (sec_id, side), edges in entry_group_edges.items():
        all_line_ids = sorted({e.line_id for e in edges})
        port_id = f"{sec_id}__entry_{side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=sec_id,
            side=side,
            line_ids=all_line_ids,
            is_entry=True,
        )
        graph.add_port(port)
        entry_port_map[(sec_id, side)] = port_id
        port_counter += 1

    return exit_port_map, entry_port_map, port_counter


def _rewrite_edges_with_junctions(
    graph: MetroGraph,
    internal_edges: list[Edge],
    inter_section_edges: list[Edge],
    entry_side_for_line: dict[tuple[str, str], PortSide],
    exit_port_map: dict[str, str],
    entry_port_map: dict[tuple[str, PortSide], str],
    port_counter: int,
) -> None:
    """Rewrite inter-section edges into 3-part chains with junctions."""
    new_edges: list[Edge] = list(internal_edges)

    # Group by exit port to detect fan-outs
    exit_fan: dict[str, dict[str, list[Edge]]] = {}

    for edge in inter_section_edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)
        entry_side = entry_side_for_line.get((tgt_sec, edge.line_id), PortSide.LEFT)

        exit_port_id = exit_port_map[src_sec]
        entry_port_id = entry_port_map[(tgt_sec, entry_side)]

        new_edges.append(
            Edge(source=edge.source, target=exit_port_id, line_id=edge.line_id)
        )
        new_edges.append(
            Edge(source=entry_port_id, target=edge.target, line_id=edge.line_id)
        )

        exit_fan.setdefault(exit_port_id, {}).setdefault(entry_port_id, []).append(edge)

    for exit_port_id, entry_targets in exit_fan.items():
        if len(entry_targets) <= 1:
            for entry_port_id, edges in entry_targets.items():
                for edge in edges:
                    new_edges.append(
                        Edge(
                            source=exit_port_id,
                            target=entry_port_id,
                            line_id=edge.line_id,
                        )
                    )
        else:
            junction_id = f"__junction_{port_counter}"
            port_counter += 1
            junction = Station(id=junction_id, label="", is_port=True, section_id=None)
            graph.add_station(junction)
            graph.junctions.append(junction_id)

            all_line_ids_set: set[str] = set()
            for edges in entry_targets.values():
                for edge in edges:
                    all_line_ids_set.add(edge.line_id)
            for lid in sorted(all_line_ids_set):
                new_edges.append(
                    Edge(source=exit_port_id, target=junction_id, line_id=lid)
                )

            for entry_port_id, edges in entry_targets.items():
                for edge in edges:
                    new_edges.append(
                        Edge(
                            source=junction_id,
                            target=entry_port_id,
                            line_id=edge.line_id,
                        )
                    )

    # Deduplicate edges by (source, target, line_id) - multiple original
    # inter-section edges targeting different stations in the same section
    # can produce identical port-to-port or junction-to-port edges.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Edge] = []
    for edge in new_edges:
        key = (edge.source, edge.target, edge.line_id)
        if key not in seen:
            seen.add(key)
            deduped.append(edge)
    graph.edges = deduped
    graph._invalidate_edge_caches()


def _create_ports_and_junctions(
    graph: MetroGraph,
    internal_edges: list[Edge],
    inter_section_edges: list[Edge],
    entry_side_for_line: dict[tuple[str, str], PortSide],
) -> None:
    """Create exit/entry ports and junctions, rewrite inter-section edges.

    Creates one exit port per source section, one entry port per
    (target_section, entry_side), and inserts junction stations where
    an exit port fans out to multiple entry ports.
    """
    section_exit_side = _determine_exit_sides(graph)
    exit_groups, entry_groups = _group_inter_section_edges(
        graph, inter_section_edges, entry_side_for_line
    )
    exit_port_map, entry_port_map, port_counter = _create_port_stations(
        graph, exit_groups, entry_groups, section_exit_side
    )
    _rewrite_edges_with_junctions(
        graph,
        internal_edges,
        inter_section_edges,
        entry_side_for_line,
        exit_port_map,
        entry_port_map,
        port_counter,
    )


def _insert_merge_junctions(graph: MetroGraph) -> None:
    """Insert merge junctions where multiple same-line edges converge on one entry port.

    After _create_ports_and_junctions, multiple inter-section edges of the same
    line can target the same entry port from different sources (e.g. raw_asm,
    purging, polishing all sending 'assemblies' to scaffolding's entry port).

    For each such group (N>1 same-line edges to one entry port), this inserts a
    merge junction and rewrites edges: all N sources -> merge junction, then one
    edge merge junction -> entry port.

    The merge junction's section_id is set to the TARGET section so that
    _resolve_section_col() in routing correctly resolves its column for bypass
    detection.
    """
    # Find edges from fan-out junctions targeting entry ports, grouped
    # by (entry_port_id, line_id).  Only junction sources are counted -
    # exit port sources route fine as normal L-shapes and shouldn't be
    # merged (merging disrupts exit port positioning).
    convergent: dict[tuple[str, str], list[Edge]] = {}
    for edge in graph.edges:
        tgt_port = graph.ports.get(edge.target)
        if not tgt_port or not tgt_port.is_entry:
            continue
        if edge.source not in graph.junctions:
            continue
        key = (edge.target, edge.line_id)
        convergent.setdefault(key, []).append(edge)

    # Only process groups with N>1 convergent edges
    merge_groups = {k: v for k, v in convergent.items() if len(v) > 1}
    if not merge_groups:
        return

    counter = len(graph.junctions)
    edges_to_remove: set[tuple[str, str, str]] = set()
    new_edges: list[Edge] = []

    for (entry_port_id, line_id), edges in merge_groups.items():
        entry_port = graph.ports[entry_port_id]
        merge_id = f"__merge_{counter}"
        counter += 1

        merge_station = Station(
            id=merge_id,
            label="",
            is_port=True,
            section_id=entry_port.section_id,
        )
        graph.add_station(merge_station)
        graph.junctions.append(merge_id)

        # Rewrite: each source -> merge junction
        for edge in edges:
            edges_to_remove.add((edge.source, edge.target, edge.line_id))
            new_edges.append(Edge(source=edge.source, target=merge_id, line_id=line_id))

        # One edge: merge junction -> entry port
        new_edges.append(Edge(source=merge_id, target=entry_port_id, line_id=line_id))

    # Apply edge rewrites
    graph.edges = [
        e for e in graph.edges if (e.source, e.target, e.line_id) not in edges_to_remove
    ] + new_edges
    graph._invalidate_edge_caches()
