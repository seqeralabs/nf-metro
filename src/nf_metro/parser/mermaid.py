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
            section = Section(id=section_id, name=_unquote(display_name.strip()))
            graph.add_section(section)
            current_section_id = section_id
            continue

        # Metro directives
        if stripped.startswith("%%metro"):
            _parse_directive(stripped, graph, current_section_id)
            continue

        # Graph declaration
        if stripped.startswith("graph "):
            _warn_if_non_lr_primary(stripped)
            continue

        # Skip regular comments
        if stripped.startswith("%%"):
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

    # Apply pending terminus designations.  Skip mid-pipeline hub
    # stations: stations the .mmd author tagged as a file/dir input but
    # which actually receive AND forward data (predecessors + successors).
    # The file icon implies "this is where the path starts" or "this is
    # where it ends", which is misleading on a routing hub.  Pure sources
    # (no predecessors) and pure sinks (no successors) keep their icon.
    for station_id, entries in graph._pending_terminus.items():
        station = graph.stations.get(station_id)
        if not station:
            continue
        if graph.edges_to(station_id) and graph.edges_from(station_id):
            continue
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


def _unquote(text: str) -> str:
    """Strip one pair of surrounding double quotes from a title or label.

    Mermaid requires special characters such as parentheses to be wrapped in
    double quotes (e.g. ``["Liftover (Picard)"]``) so the diagram parses on
    GitHub. The quotes are escaping syntax, not part of the displayed text, so
    they are removed here, leaving the inner text untouched.
    """
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


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
    elif content.startswith("logo_scale:"):
        _parse_logo_scale_directive(content[len("logo_scale:") :].strip(), graph)
    elif content.startswith("logo:"):
        graph.logo_path = content[len("logo:") :].strip()
    elif content.startswith("compact_offsets:"):
        val = content[len("compact_offsets:") :].strip().lower()
        graph.compact_offsets = val in ("true", "yes", "1")
    elif content.startswith("center_ports:"):
        val = content[len("center_ports:") :].strip().lower()
        graph.center_ports = val in ("true", "yes", "1")
    elif content.startswith("label_angle:"):
        try:
            graph.label_angle = float(content[len("label_angle:") :].strip())
        except ValueError:
            pass
    elif content.startswith("legend_min_height:"):
        try:
            graph.legend_min_height = float(
                content[len("legend_min_height:") :].strip()
            )
        except ValueError:
            pass
    elif content.startswith("legend_combo:"):
        _parse_legend_combo_directive(content[len("legend_combo:") :].strip(), graph)
    elif content.startswith("legend:"):
        _parse_legend_directive(content[len("legend:") :].strip(), graph)
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


_LEGEND_KEYWORDS = ("bl", "br", "tl", "tr", "bottom", "right", "none")


def _parse_xy(text: str) -> tuple[float, float] | None:
    """Parse a ``x,y`` pair into floats, or None if it is not a number pair."""
    if "," not in text:
        return None
    x_str, _, y_str = text.partition(",")
    try:
        return (float(x_str.strip()), float(y_str.strip()))
    except ValueError:
        return None


def _parse_legend_directive(value: str, graph: MetroGraph) -> None:
    """Parse the %%metro legend: directive positioning the legend+logo block.

    Grammar (the keyword forms stay content-anchored with the historical
    overlap fallback; the qualifier and absolute forms pin the block exactly):

        legend: <keyword>              keyword (bl/br/tl/tr/bottom/right/none)
        legend: <keyword> | canvas     anchor the keyword to the canvas frame
        legend: <keyword> | <dx>,<dy>  nudge the keyword anchor by an offset
        legend: <x>,<y>                absolute top-left coordinates
    """
    # Reset modifiers so a re-declared directive starts clean.
    graph.legend_anchor = "content"
    graph.legend_offset = None
    graph.legend_at = None

    head, sep, qualifier = value.partition("|")
    head = head.strip().lower()
    qualifier = qualifier.strip().lower()

    coords = _parse_xy(head)
    if coords is not None:
        graph.legend_at = coords
        graph.legend_position = "free"
        if sep:
            warnings.warn(
                f"legend qualifier {qualifier!r} ignored with absolute coordinates.",
                stacklevel=2,
            )
        return

    if head not in _LEGEND_KEYWORDS:
        warnings.warn(
            f"Unknown legend position {head!r}; expected one of "
            f"{'/'.join(_LEGEND_KEYWORDS)} or 'x,y'. Ignoring.",
            stacklevel=2,
        )
        return
    graph.legend_position = head

    if not qualifier:
        return
    if qualifier == "canvas":
        graph.legend_anchor = "canvas"
        return
    offset = _parse_xy(qualifier)
    if offset is not None:
        graph.legend_offset = offset
    else:
        warnings.warn(
            f"Unknown legend qualifier {qualifier!r}; expected 'canvas' or "
            "'dx,dy'. Ignoring.",
            stacklevel=2,
        )


def _parse_logo_scale_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro logo_scale: <factor> (1.0 = default auto-size)."""
    try:
        scale = float(value)
    except ValueError:
        warnings.warn(
            f"Invalid logo_scale {value!r}; expected a positive number.",
            stacklevel=2,
        )
        return
    if scale <= 0:
        warnings.warn(
            f"logo_scale must be positive, got {value!r}; ignoring.",
            stacklevel=2,
        )
        return
    graph.logo_scale = scale


def _parse_legend_combo_directive(value: str, graph: MetroGraph) -> None:
    """Parse %%metro legend_combo: lineA, lineB[, ...] | Display Label.

    Stores a (line_ids, label) entry on ``graph.legend_combos``. The named
    lines are rendered as a single combined legend row and suppressed from
    their own individual rows. A combo referencing unknown lines is warned
    about and ignored; unknown members of an otherwise-valid combo are
    dropped (with a warning) and the remaining members kept.
    """
    parts = value.split("|", 1)
    if len(parts) != 2:
        warnings.warn(
            f"Invalid legend_combo {value!r}; expected 'lineA, lineB | Display Label'.",
            stacklevel=2,
        )
        return
    ids_raw, label = parts[0], parts[1].strip()
    line_ids = [s.strip() for s in ids_raw.split(",") if s.strip()]
    if len(line_ids) < 2 or not label:
        warnings.warn(
            f"Invalid legend_combo {value!r}; expected at least two line IDs "
            "and a non-empty label.",
            stacklevel=2,
        )
        return
    known = [lid for lid in line_ids if lid in graph.lines]
    unknown = [lid for lid in line_ids if lid not in graph.lines]
    if unknown:
        warnings.warn(
            f"legend_combo {label!r} references unknown line(s) "
            f"{', '.join(unknown)}; ignoring those.",
            stacklevel=2,
        )
    if len(known) < 2:
        warnings.warn(
            f"legend_combo {label!r} has fewer than two known lines; ignoring.",
            stacklevel=2,
        )
        return
    graph.legend_combos.append((tuple(known), label))


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
            label = (
                m.group(2).strip()
                if m.lastindex is not None and m.lastindex >= 2
                else node_id
            )
            label = _unquote(label)
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
        graph.replace_edges(
            [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
        )
    for edge in new_edges:
        graph.add_edge(edge)


def _insert_bypass_stations(graph: MetroGraph) -> None:
    """Insert virtual stations so non-consumed lines bypass intermediate stops.

    When a station S sits in the layer-path between an in-section
    source P and an exit port, lines flowing ``P -> exit_port`` that S
    neither consumes nor produces would otherwise route through S's
    column and crash into the marker.  Inserting a hidden virtual
    station ``V`` between P and the exit port gives the routing engine
    a column-mate to fan the bypassing lines around S, using the same
    parallel-branch primitives the rest of the section uses.

    The trigger only fires when the routing engine genuinely needs the
    helper - otherwise V's add tracks that inflate section height
    without visual benefit.  The three discriminants are:

    1. *Section topology*.  Single-trunk sections (one head station at
       the lowest non-port layer, e.g. the 05/06 guide family) funnel
       every line through a shared trunk and can't escape S's marker
       without help.  Multi-trunk sections (rnaseq_auto's
       ``genome_align``, epitopeprediction's ``input_processing``,
       etc.) already place each inbound line on its own parallel track
       from the entry, so the routing engine clears the marker via
       track consolidation - bypass would only over-detour the line.
       In multi-trunk sections we still allow bypass at fan-in
       convergence points (S with >=2 in-section predecessors, e.g.
       differentialabundance's ``annotate``) where the line bundle
       genuinely loses its parallel-track headroom past S.

    2. *Trunk consumption*.  S must consume at least one line that
       also flows through some other in-section edge.  A station whose
       only consumed line is a local spur (e.g. nf_with_subworkflows's
       ``samtools_index`` taking a single ``spur`` line straight from
       ``samtools_sort``) sits off-trunk; bypass would snap it back to
       the trunk Y and open a vertical gap.

    3. *Candidate predecessors*.  In single-trunk sections we scan all
       lower-layer in-section stations P (siblings and direct preds
       alike) because the bypass line may originate from either side of
       the trunk.  In multi-trunk fan-in sections we restrict to S's
       direct predecessors P -> S, since unrelated lines have their own
       track already.

    Rewrite (per bypassing ``(P, S)`` group):

    * Add ``V`` (``id=f"__bypass_{S}_{P}_{n}"``, ``is_hidden=True``,
      same section as S).
    * For each bypassed edge ``P -> exit_port (L)`` (L not in S's
      consumed-or-produced line set, ``layer(P) < layer(S) <
      layer(exit)``), replace with ``P -> V (L)`` + ``V -> exit_port
      (L)``.
    """
    if not graph.sections:
        return

    import networkx as nx

    pending_terminus_ids: set[str] = set(graph._pending_terminus.keys())

    edges_by_source: dict[str, list[tuple[int, Edge]]] = {}
    for i, edge in enumerate(graph.edges):
        edges_by_source.setdefault(edge.source, []).append((i, edge))

    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
    bypass_count = 0

    def _section_layers(section_ids: set[str]) -> dict[str, int]:
        sub: nx.DiGraph[str] = nx.DiGraph()
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

        in_section_edges = [
            e
            for e in graph.edges
            if e.source in station_ids and e.target in station_ids
        ]
        in_preds_by_target: dict[str, set[str]] = {}
        consumed_lines_by_target: dict[str, set[str]] = {}
        for e in in_section_edges:
            in_preds_by_target.setdefault(e.target, set()).add(e.source)
            consumed_lines_by_target.setdefault(e.target, set()).add(e.line_id)

        entry_port_ids = set(section.entry_ports)
        internal_ids = [
            sid
            for sid in station_ids
            if sid not in exit_port_ids
            and sid not in entry_port_ids
            and sid in sec_layers
        ]
        if not internal_ids:
            continue
        min_internal_layer = min(sec_layers[sid] for sid in internal_ids)
        head_count = sum(
            1 for sid in internal_ids if sec_layers[sid] == min_internal_layer
        )
        single_trunk_section = head_count <= 1

        for sid in section.station_ids:
            station = graph.stations.get(sid)
            if station is None or station.is_port or station.is_hidden:
                continue
            if station.is_terminus or sid in pending_terminus_ids:
                continue

            s_layer = sec_layers.get(sid)
            if s_layer is None:
                continue
            s_lines = set(graph.station_lines(sid))

            in_section_preds = in_preds_by_target.get(sid, set())
            # In multi-trunk sections, only fan-in convergence points
            # (S has >=2 in-section predecessors) need bypass help, and
            # only from a direct predecessor of S - other lines already
            # have their own parallel tracks.
            if not single_trunk_section and len(in_section_preds) < 2:
                continue

            # Skip stations that only consume a spur line - the bypass
            # would snap S's spur track to the section trunk Y, opening
            # an unnecessary vertical gap to S.  A consumed line is
            # "trunk" when it has at least one in-section edge that
            # doesn't touch S.
            consumed_lines = consumed_lines_by_target.get(sid, set())
            trunk_lines = {
                e.line_id
                for e in in_section_edges
                if e.source != sid and e.target != sid
            }
            if consumed_lines and not (consumed_lines & trunk_lines):
                continue

            candidate_preds = sorted(
                station_ids if single_trunk_section else in_section_preds
            )

            bypass_by_pred: dict[str, list[tuple[int, Edge]]] = {}
            for pred_id in candidate_preds:
                if pred_id == sid:
                    continue
                pred_layer = sec_layers.get(pred_id)
                if pred_layer is None or pred_layer >= s_layer:
                    continue
                for i, edge in edges_by_source.get(pred_id, []):
                    if edge.target not in exit_port_ids:
                        continue
                    if edge.line_id in s_lines:
                        continue
                    t_layer = sec_layers.get(edge.target)
                    if t_layer is None or t_layer <= s_layer:
                        continue
                    bypass_by_pred.setdefault(pred_id, []).append((i, edge))

            for pred_id, bypass_edges in bypass_by_pred.items():
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
        graph.replace_edges(
            [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
        )
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
        # _classify_edges only files an edge as inter-section when both
        # endpoints resolve to a section, so neither lookup is None here.
        assert src_sec is not None and tgt_sec is not None
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

    for sec_id in exit_group_edges:
        side = section_exit_side.get(sec_id, PortSide.RIGHT)
        port_id = f"{sec_id}__exit_{side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=sec_id,
            side=side,
            is_entry=False,
        )
        graph.add_port(port)
        exit_port_map[sec_id] = port_id
        port_counter += 1

    entry_port_map: dict[tuple[str, PortSide], str] = {}

    for sec_id, side in entry_group_edges:
        port_id = f"{sec_id}__entry_{side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=sec_id,
            side=side,
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
        assert src_sec is not None and tgt_sec is not None
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
            graph.add_junction(junction_id)

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
    graph.replace_edges(deduped)


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
        graph.add_junction(merge_id)

        # Rewrite: each source -> merge junction
        for edge in edges:
            edges_to_remove.add((edge.source, edge.target, edge.line_id))
            new_edges.append(Edge(source=edge.source, target=merge_id, line_id=line_id))

        # One edge: merge junction -> entry port
        new_edges.append(Edge(source=merge_id, target=entry_port_id, line_id=line_id))

    # Apply edge rewrites
    kept = [
        e for e in graph.edges if (e.source, e.target, e.line_id) not in edges_to_remove
    ]
    graph.replace_edges(kept + new_edges)
