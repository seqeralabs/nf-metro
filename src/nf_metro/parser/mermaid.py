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

import copy
import re
import warnings
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from nf_metro.options import LineOrder, is_line_order
from nf_metro.parser.commitments import (
    AppliedLayoutCommitments,
    LayoutCommitmentOverlay,
    apply_layout_commitment_overlay,
)
from nf_metro.parser.directives import (
    _apply_directive,
    _deduplicate_section_number_overrides,
    _resolve_legend_combos,
    _warn_unresolved_references,
)
from nf_metro.parser.grammar import (
    _Comment,
    _Directive,
    _Edge,
    _End,
    _GraphHeader,
    _Junk,
    _Node,
    _normalize_multiline_text,
    _Statement,
    _Subgraph,
    _unquote,
    parse_statements,
)
from nf_metro.parser.model import (
    UNANNOTATED_LINE_ID,
    Edge,
    MetroGraph,
    Section,
    Station,
)
from nf_metro.parser.resolve import (
    _create_implicit_section,
    _expand_interchanges,
    _insert_bypass_stations,
    _insert_terminus_convergence_stations,
    _remove_empty_sections,
    _resolve_sections,
    resolve_section_endpoints,
)
from nf_metro.parser.route_topology import (
    AuthoredEdgeLineage,
    RouteResolutionTrace,
    build_route_topology,
    build_route_topology_query,
    capture_authored_routes,
    snapshot_resolved_authored_edges,
)
from nf_metro.parser.validate import (
    find_cycle,
    find_section_cycle,
    find_undeclared_line_edges,
)

# A row-wrap width no real map reaches, so section packing never folds: the
# layout it yields is the unbounded baseline a user-set threshold is judged
# against for fold-induced compression.
_UNBOUNDED_FOLD = 1_000_000


def _unbounded_section_grid(
    graph: MetroGraph, infer_section_layout: Callable[..., None]
) -> dict[str, tuple[int, int]] | None:
    """Section ``(grid_col, grid_row)`` map for the no-fold baseline layout.

    A deep copy is inferred at a width no map folds at, so the result is the
    layout a user-set ``fold_threshold`` is compared against to find which
    sections it relocated.  Returns ``None`` if the probe raises: detection is
    best-effort, never breaking a render that would otherwise succeed.
    """
    try:
        probe = copy.deepcopy(graph)
        infer_section_layout(probe, max_station_columns=_UNBOUNDED_FOLD)
    except Exception:
        return None
    return {sid: (s.grid_col, s.grid_row) for sid, s in probe.sections.items()}


def _line_hint(line: int | None) -> str:
    """Return `` (line N)`` when a source line is known, else empty string."""
    return f" (line {line})" if line is not None else ""


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
            "See: https://seqeralabs.github.io/nf-metro/latest/nextflow/"
        )

    if has_flowchart:
        raise ValueError(
            "Mermaid 'flowchart' syntax is not supported. "
            "Use 'graph LR' with %%metro directives instead.\n\n"
            "See the guide: "
            "https://seqeralabs.github.io/nf-metro/latest/guide/"
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
    """Reject edges that carry no line annotation or name an undeclared line.

    Raises ``ValueError`` with an authoring hint naming the offending edges.
    The undeclared-line half comes from
    :func:`~nf_metro.parser.validate.find_undeclared_line_edges`, the same
    detector ``validate_graph`` reports through, so the ``render`` and
    ``validate`` commands accept exactly the same maps.
    """
    if not graph.edges:
        return

    bad_edges = [edge for edge in graph.edges if edge.line_id == UNANNOTATED_LINE_ID]
    undeclared_lines: defaultdict[str, set[int]] = defaultdict(set)
    for edge in find_undeclared_line_edges(graph):
        if edge.source_line is not None:
            undeclared_lines[edge.line_id].add(edge.source_line)
        else:
            undeclared_lines.setdefault(edge.line_id, set())

    if bad_edges:
        examples = []
        seen = set()
        for edge in bad_edges:
            key = (edge.source, edge.target)
            if key not in seen:
                seen.add(key)
                examples.append(
                    f"  {edge.source} --> {edge.target}{_line_hint(edge.source_line)}"
                )
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
            + ", ".join(
                f"'{lid}'{_line_hint(min(undeclared_lines[lid], default=None))}"
                for lid in sorted(undeclared_lines)
            )
            + "\n\n"
            "Declare each line with a %%metro line: directive, e.g.:\n"
            + "\n".join(
                f"  %%metro line: {lid} | {lid} | #hexcolor"
                for lid in sorted(undeclared_lines)
            )
        )


def parse_metro_mermaid_file(path: Path, **kwargs: object) -> MetroGraph:
    """Parse the map at *path*, recording the directory it came from.

    ``graph.source_dir`` is what a ``%%metro logo:`` path resolves against.
    Reading a map's text and calling :func:`parse_metro_mermaid` directly drops
    that, leaving the asset resolvable only while the process working directory
    happens to sit where the path was written from; load from disk through here
    instead so no caller has to remember. *path* is resolved to an absolute
    path before its parent is recorded, so a later change to the process
    working directory can't shift what ``source_dir`` means.
    """
    path = path.resolve()
    graph = parse_metro_mermaid(path.read_text(), **kwargs)  # type: ignore[arg-type]
    graph.source_dir = str(path.parent)
    return graph


def parse_metro_mermaid(
    text: str,
    max_station_columns: int | None = None,
    auto_process: bool | None = None,
    process_scope: str | None = None,
    caller_line_order: LineOrder | None = None,
    *,
    _layout_commitments: LayoutCommitmentOverlay | None = None,
) -> MetroGraph:
    """Parse a Mermaid graph definition with %%metro directives.

    ``max_station_columns`` is the row-wrap width supplied by the caller (the
    ``--max-layers-per-row`` CLI flag). When ``None``, a ``%%metro
    fold_threshold`` directive supplies the width, falling back to 15.

    ``auto_process`` is the ``--auto-process`` CLI flag; when not ``None`` it
    overrides the ``%%metro auto_process`` directive, deciding whether stations
    without an explicit ``process:`` mapping get their id as a default pattern.

    ``process_scope`` is the ``--process-scope`` CLI flag; when not ``None`` it
    overrides the ``%%metro process_scope`` directive, supplying the common FQN
    prefix that explicit ``process:`` tails are joined under and matched
    literally against.

    ``caller_line_order`` is the validated caller policy whose provenance must
    be captured before layout inference.
    """
    if caller_line_order is not None and not is_line_order(caller_line_order):
        raise ValueError(f"unsupported caller line order {caller_line_order!r}")
    _check_unsupported_input(text)
    graph = MetroGraph()
    _apply_statements(parse_statements(text), graph)
    _finalize_graph(
        graph,
        max_station_columns,
        auto_process,
        process_scope,
        caller_line_order,
        layout_commitments=_layout_commitments,
    )
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


def _finalize_graph(
    graph: MetroGraph,
    max_station_columns: int | None,
    auto_process: bool | None = None,
    process_scope: str | None = None,
    caller_line_order: LineOrder | None = None,
    *,
    layout_commitments: LayoutCommitmentOverlay | None = None,
) -> None:
    """Validate, run the post-parse resolution, and apply buffered metadata."""
    _validate_edge_annotations(graph)
    _resolve_legend_combos(graph)
    _warn_unresolved_references(graph)
    authored_routes = capture_authored_routes(graph)
    graph.layout_provenance.capture_authored_intent(
        graph,
        authored_routes,
        max_station_columns,
        caller_line_order,
    )

    if graph.sections:
        _remove_empty_sections(graph)
        _deduplicate_section_number_overrides(graph)

    # Re-check: _remove_empty_sections may have emptied graph.sections.
    if graph.sections:
        _create_implicit_section(graph)

    applied_commitments = (
        apply_layout_commitment_overlay(graph, authored_routes, layout_commitments)
        if layout_commitments is not None
        else AppliedLayoutCommitments()
    )

    # Layout inference (interchange expansion, section grids, port/junction
    # resolution) assumes the graph is a DAG. A cyclic graph is rejected at the
    # render boundary (compute_layout) and reported by validate_graph, so leave
    # it un-inferred here rather than feed a distorted size estimate into
    # strategy selection.
    if find_cycle(graph) is None and find_section_cycle(graph) is None:
        _infer_layout(graph, max_station_columns, applied_commitments)

    # A caller value (the --auto-process / --process-scope CLI flags) wins over
    # the matching %%metro directive set during statement application.
    if auto_process is not None:
        graph.auto_process = auto_process
    if process_scope is not None:
        graph.process_scope = process_scope

    _apply_pending_metadata(graph)


def _infer_layout(
    graph: MetroGraph,
    max_station_columns: int | None,
    commitments: AppliedLayoutCommitments = AppliedLayoutCommitments(),
) -> None:
    """Run interchange expansion, section-grid inference, and section resolution.

    The graph must be acyclic: the section-grid size estimator and the
    topological passes this drives assume a DAG.
    """
    from nf_metro.layout.auto_layout import (
        infer_interchanges,
        infer_section_layout,
    )

    # Populate graph.interchanges before expansion so auto-detected and
    # author-written interchanges share the expansion path.
    infer_interchanges(graph)

    # A sectionless graph needs an implicit section to host the bypass detours
    # its skip-lines would otherwise draw straight through the markers they skip;
    # that detour routing is gated on a section existing.
    if not graph.sections:
        _create_implicit_section(graph)

    authored_capture = capture_authored_routes(graph)
    authored_lineage = AuthoredEdgeLineage.from_capture(graph.edges, authored_capture)

    if graph.sections:
        _expand_interchanges(graph, authored_lineage)

        # Row-wrap width precedence: an explicit caller value (the
        # --max-layers-per-row CLI flag) wins over a %%metro fold_threshold
        # directive, which in turn overrides the default of 15. Raising it
        # keeps a long horizontal trunk of sections on a single row.
        if max_station_columns is not None:
            eff_cols = max_station_columns
            author_set_fold = True
        elif graph.fold_threshold is not None:
            eff_cols = graph.fold_threshold
            author_set_fold = True
        else:
            eff_cols = 15
            author_set_fold = False

        unbounded_grid = (
            _unbounded_section_grid(graph, infer_section_layout)
            if author_set_fold
            else None
        )
        infer_section_layout(graph, max_station_columns=eff_cols)
        if unbounded_grid is not None:
            relocated = {
                sid
                for sid, s in graph.sections.items()
                if (s.grid_col, s.grid_row) != unbounded_grid.get(sid)
            }
            if relocated:
                graph._fold_compressed_sections = relocated
        _insert_terminus_convergence_stations(graph, authored_lineage)
        resolved_authored_edges = snapshot_resolved_authored_edges(
            authored_capture, authored_lineage, graph.edges
        )
        endpoint_resolution = resolve_section_endpoints(
            graph, authored_lineage, commitments
        )
        graph.route_topology = build_route_topology(
            authored_capture, authored_lineage, endpoint_resolution
        )
        graph.route_resolution = _resolve_sections(
            graph,
            endpoint_resolution,
            graph.route_topology,
            resolved_authored_edges,
        )
        graph.route_resolution = _insert_bypass_stations(graph, graph.route_resolution)
    else:
        graph.route_topology = build_route_topology(authored_capture, authored_lineage)
        graph.route_resolution = RouteResolutionTrace(
            authored_edges=snapshot_resolved_authored_edges(
                authored_capture, authored_lineage, graph.edges
            )
        )
    build_route_topology_query(graph)


def _apply_pending_metadata(graph: MetroGraph) -> None:
    """Apply terminus icons, off-track marks, and markers buffered during parse.

    Off-track marks apply only to pure sources and sinks because a hub has no
    free side against which it can be lifted. File icons are station metadata
    and apply independently of graph degree.
    """
    for station_id, entries in graph._pending_terminus.items():
        station = graph.stations.get(station_id)
        if not station:
            continue
        station.terminus_labels = [label for label, _, _, _ in entries]
        station.terminus_icon_types = [icon_type for _, icon_type, _, _ in entries]
        station.terminus_names = [name for _, _, name, _ in entries]
        station.terminus_icon_banners = [banner for _, _, _, banner in entries]

    for station_id in graph._pending_off_track:
        station = graph.stations.get(station_id)
        if not station or graph.is_hub(station_id):
            continue
        station.off_track = True

    for station_id, marker in graph._pending_markers.items():
        station = graph.stations.get(station_id)
        if station:
            station.marker = marker

    scope = graph.process_scope
    for station_id, pattern in graph._pending_process:
        if station_id not in graph.stations:
            continue
        # Under a scope the prefix anchors the start and the literal tail
        # anchors the final segment(s), tolerating intermediate subworkflow
        # nesting between them; without a scope the value is a regex matched
        # as-is.
        if scope:
            effective = rf"(?:^|:){re.escape(scope)}:(?:.+:)?{re.escape(pattern)}$"
        else:
            effective = pattern
        graph.process_mapping.setdefault(station_id, []).append(effective)

    if graph.auto_process:
        for station_id, station in graph.stations.items():
            if station.is_port or station.is_hidden:
                continue
            # Anchor the id to the start of the final ':'-delimited segment (the
            # process name itself) so it can't match a tool name buried in the
            # scope path, e.g. station 'star' must not light up for
            # '...:QUANTIFY_STAR_SALMON:SALMON_QUANT'.
            default = rf"(?:^|:){re.escape(station_id)}[^:]*$"
            graph.process_mapping.setdefault(station_id, [default])


def _apply_node(
    node_id: str,
    label: str,
    graph: MetroGraph,
    section_id: str | None = None,
) -> None:
    """Declare a node: register it (or set the label on a station an edge
    auto-created) from its id and (shape-stripped) label text."""
    label = _unquote(label.strip())
    label = _normalize_multiline_text(label)
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


def _apply_edge(
    edge: _Edge,
    graph: MetroGraph,
    section_id: str | None,
) -> None:
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
        graph.add_edge(
            Edge(
                source=edge.source,
                target=edge.target,
                line_id=line_id,
                source_line=edge.line_no,
            )
        )
