"""Post-parse graph rewrites.

After the driver in :mod:`nf_metro.parser.mermaid` has applied every statement,
these helpers reshape the :class:`MetroGraph`: dropping empty sections, wrapping
loose stations in an implicit section, inserting convergence/bypass/merge
junctions, and resolving inter-section edges into port-to-port chains.
"""

from __future__ import annotations

import warnings
from collections import defaultdict
from dataclasses import dataclass, replace

import networkx as nx

from nf_metro.graph_views import directed_graph, longest_path_layers
from nf_metro.parser.commitments import AppliedLayoutCommitments
from nf_metro.parser.directives import _warn_directive
from nf_metro.parser.model import (
    BYPASS_V_PREFIX,
    CONVERGE_PREFIX,
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
)
from nf_metro.parser.provenance import (
    ConnectorEndpointKey,
    ConnectorEndpointRole,
    DecisionOrigin,
    DecisionReason,
    EndpointSideSelection,
    EndpointSideTransition,
)
from nf_metro.parser.route_topology import (
    AuthoredEdgeKey,
    AuthoredEdgeLineage,
    ConnectorId,
    ConvergenceId,
    DivergenceGroup,
    DivergenceId,
    EndpointGroupId,
    ResolvedAuthoredEdge,
    ResolvedConvergence,
    ResolvedDivergence,
    ResolvedEdge,
    ResolvedEndpointPort,
    RouteConnector,
    RouteResolutionTrace,
    RouteTopology,
)


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


def _expand_interchanges(graph: MetroGraph, lineage: AuthoredEdgeLineage) -> None:
    """Expand each ``%%metro interchange:`` node into one sub-station per rail.

    The named node becomes a column of co-located ordinary stations (one per
    rail that carries a live line, top to bottom in declaration order); every
    edge touching the node is repointed to the sub-station whose rail owns the
    edge's line.  From here on the layout engine treats them as plain stations,
    so each line keeps its own track through the step and only the renderer
    joins them into one connector glyph.  An unknown node, or a directive that
    resolves to fewer than two rails actually carrying the node's lines, is
    warned about and skipped.
    """
    # (node_id, line_id) -> the member sub-station that line's edges retarget to.
    moved: dict[tuple[str, str], str] = {}
    for ic in graph.interchanges:
        orig = graph.stations.get(ic.node_id)
        if orig is None:
            _warn_directive(
                "interchange", f"unknown station id {ic.node_id!r}; ignoring"
            )
            continue
        # Assign each of the node's lines to the first rail that names it; lines
        # named in no rail (or only in rails that name lines the node lacks) fall
        # onto the first surviving rail.  Rails that name no live line drop out.
        node_lines = set(graph.station_lines(ic.node_id))
        line_rail: dict[str, int] = {}
        for i, rail in enumerate(ic.rails):
            for lid in rail:
                if lid in node_lines and lid not in line_rail:
                    line_rail[lid] = i
        surviving = sorted(set(line_rail.values()))
        if len(surviving) < 2:
            _warn_directive(
                "interchange",
                f"node {ic.node_id!r} resolves to fewer than two rails "
                "carrying its lines; ignoring",
            )
            continue
        for lid in node_lines - set(line_rail):
            line_rail[lid] = surviving[0]

        rail_member: dict[int, str] = {}
        member_ids: list[str] = []
        for pos, rail_i in enumerate(surviving):
            if pos == 0:
                orig.interchange_id = ic.node_id
                rail_member[rail_i] = ic.node_id
            else:
                sub = Station(
                    id=f"{ic.node_id}__rail{rail_i}",
                    label="",
                    section_id=orig.section_id,
                    interchange_id=ic.node_id,
                )
                graph.register_station(sub)
                rail_member[rail_i] = sub.id
            member_ids.append(rail_member[rail_i])

        for lid, rail_i in line_rail.items():
            moved[(ic.node_id, lid)] = rail_member[rail_i]
        ic.label = orig.label
        ic.member_ids = member_ids

    if not moved:
        return
    replacement_edges: list[Edge] = []
    for edge in graph.edges:
        replacement = Edge(
            source=moved.get((edge.source, edge.line_id), edge.source),
            target=moved.get((edge.target, edge.line_id), edge.target),
            line_id=edge.line_id,
            source_line=edge.source_line,
        )
        lineage.replace(edge, replacement)
        replacement_edges.append(replacement)
    graph.replace_edges(replacement_edges)


# Cascade a fan-in when the lone-junction merge would leave a sibling group
# running more columns parallel than the guard tolerates (mirrors
# ``guards._MAX_SIBLING_MERGE_SLACK``): the single junction sits at
# ``max_local + 1``, so its slack for a group at ``layer`` is
# ``(max_local + 1) - layer``.
_MAX_FANIN_MERGE_SLACK = 2


def _section_local_layers(graph: MetroGraph, section_id: str | None) -> dict[str, int]:
    """Longest-path layers over one section's internal edges.

    Cross-section edges are ignored, so a station fed only from outside the
    section is a local root (layer 0), matching how the layout engine columns a
    section from its own entry.  This is the grouping metric for
    :func:`_insert_terminus_convergence_stations`: same-layer siblings here land
    in the same grid column at layout time.
    """
    members = {sid for sid, s in graph.stations.items() if s.section_id == section_id}
    return _section_topo_layers(graph, members)


def _insert_terminus_convergence_stations(
    graph: MetroGraph, lineage: AuthoredEdgeLineage
) -> None:
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

    When the sources sit at different layers -- a short fan and a longer
    parallel path both feeding the terminus -- routing them all through one
    junction placed at the deepest source's column makes the short fan run
    parallel the whole way there, bowing it out to fill the gap.  Instead the
    sources merge in a cascade by ascending layer: same-layer siblings meet at
    a junction one layer downstream, and each running trunk folds in the next,
    deeper source group, so a fan merges locally and the remaining distance is
    a single-line run (issue #1296).
    """
    pending_terminus = graph._pending_terminus
    if not pending_terminus:
        return

    local_layers: dict[str | None, dict[str, int]] = {}
    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
    converge_count = 0

    for terminus_id in list(pending_terminus.keys()):
        # Find direct inbound edges and the lines each source carries.
        inbound: list[int] = []
        source_lines: dict[str, set[str]] = {}
        origins_by_source_line: dict[tuple[str, str], list[AuthoredEdgeKey]] = (
            defaultdict(list)
        )
        for i, edge in enumerate(graph.edges):
            if edge.target != terminus_id:
                continue
            inbound.append(i)
            source_lines.setdefault(edge.source, set()).add(edge.line_id)
            origins_by_source_line[(edge.source, edge.line_id)].extend(
                lineage.origins(edge)
            )
        if len(source_lines) < 2:
            continue

        terminus = graph.stations.get(terminus_id)
        if terminus is None:
            continue
        edges_to_remove.update(inbound)

        layers = local_layers.get(terminus.section_id)
        if layers is None:
            layers = _section_local_layers(graph, terminus.section_id)
            local_layers[terminus.section_id] = layers

        # Cascade the sources into the terminus low-to-high.  In-section
        # sources are grouped by layer so same-layer siblings converge at a
        # junction one layer on, then the running trunk folds in each deeper
        # group -- a fan merges locally and the remaining distance is a
        # single-line run.  Out-of-section sources arrive through the section's
        # entry port at the terminus's own column, so they share one final
        # group rather than a per-layer cascade over unrelated columns.
        by_layer: dict[int, list[str]] = defaultdict(list)
        cross_section: list[str] = []
        for src in source_lines:
            st = graph.stations.get(src)
            if st is not None and st.section_id == terminus.section_id:
                by_layer[layers.get(src, 0)].append(src)
            else:
                cross_section.append(src)

        # Only cascade when it earns the extra junction: a sibling group of 2+
        # would otherwise run more than the tolerated columns parallel to the
        # deepest source's merge column.  A group that merges one column late is
        # a modest bulge left as a single junction, so dense maps aren't
        # perturbed for a marginal gain.
        max_local = max(by_layer, default=-1)
        should_cascade = any(
            len(srcs) >= 2 and (max_local + 1) - layer > _MAX_FANIN_MERGE_SLACK
            for layer, srcs in by_layer.items()
        )

        if should_cascade:
            groups = [by_layer[layer] for layer in sorted(by_layer)]
            if cross_section:
                groups.append(cross_section)
        else:
            groups = [list(source_lines)]

        # ``carry`` is the node feeding onward with the lines gathered so far.
        carry: tuple[str, set[str], dict[str, tuple[AuthoredEdgeKey, ...]]] | None = (
            None
        )
        for group in groups:
            members = [
                (
                    src,
                    source_lines[src],
                    {
                        line_id: lineage.ordered_union(
                            origins_by_source_line[(src, line_id)]
                        )
                        for line_id in source_lines[src]
                    },
                )
                for src in group
            ]
            if carry is not None:
                members.append(carry)
            if len(members) == 1:
                carry = members[0]
                continue

            converge_count += 1
            converge_id = f"{CONVERGE_PREFIX}{terminus_id}_{converge_count}"
            new_stations.append(
                Station(
                    id=converge_id,
                    label="",
                    section_id=terminus.section_id,
                    is_hidden=True,
                )
            )
            merged_lines: set[str] = set()
            merged_origins: dict[str, list[AuthoredEdgeKey]] = defaultdict(list)
            for member_id, member_lines, member_origins in members:
                merged_lines |= member_lines
                for line_id in sorted(member_lines):
                    origins = member_origins.get(line_id, ())
                    edge = Edge(
                        source=member_id,
                        target=converge_id,
                        line_id=line_id,
                    )
                    lineage.bind(edge, origins)
                    new_edges.append(edge)
                    merged_origins[line_id].extend(origins)
            carry = (
                converge_id,
                merged_lines,
                {
                    line_id: lineage.ordered_union(origins)
                    for line_id, origins in merged_origins.items()
                },
            )

        assert carry is not None
        carry_id, carry_lines, carry_origins = carry
        for line_id in sorted(carry_lines):
            edge = Edge(
                source=carry_id,
                target=terminus_id,
                line_id=line_id,
            )
            lineage.bind(edge, carry_origins.get(line_id, ()))
            new_edges.append(edge)

    if not new_stations:
        return

    for st in new_stations:
        graph.register_station(st)

    if edges_to_remove:
        for i, edge in enumerate(graph.edges):
            if i in edges_to_remove:
                lineage.discard(edge)
        graph.replace_edges(
            [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
        )
    for edge in new_edges:
        graph.add_edge(edge)


def _expand_resolved_paths(
    edge_paths: tuple[tuple[ResolvedEdge, ...], ...],
    replacements: dict[ResolvedEdge, list[tuple[ResolvedEdge, ...]]],
) -> tuple[tuple[ResolvedEdge, ...], ...]:
    """Expand connector paths across edges replaced by parallel bypass paths."""
    expanded_paths: list[tuple[ResolvedEdge, ...]] = []
    changed = False
    for path in edge_paths:
        variants: list[tuple[ResolvedEdge, ...]] = [()]
        for edge in path:
            replacement_paths = replacements.get(edge)
            if replacement_paths is None:
                replacement_paths = [(edge,)]
            else:
                changed = True
            variants = [
                prefix + replacement
                for prefix in variants
                for replacement in replacement_paths
            ]
        expanded_paths.extend(variants)
    return tuple(expanded_paths) if changed else edge_paths


def _expand_resolved_authored_edges(
    records: tuple[ResolvedAuthoredEdge, ...],
    replacements: dict[ResolvedEdge, list[tuple[ResolvedEdge, ...]]],
) -> tuple[ResolvedAuthoredEdge, ...]:
    """Apply physical-edge replacements to the canonical authored trace."""
    shared_paths: dict[
        tuple[tuple[ResolvedEdge, ...], ...],
        tuple[tuple[ResolvedEdge, ...], ...],
    ] = {}
    expanded: list[ResolvedAuthoredEdge] = []
    for record in records:
        paths = _expand_resolved_paths(record.edge_paths, replacements)
        paths = shared_paths.setdefault(paths, paths)
        expanded.append(
            record
            if paths is record.edge_paths
            else ResolvedAuthoredEdge(record.authored_edge_id, paths)
        )
    return tuple(expanded)


def _insert_bypass_stations(
    graph: MetroGraph, route_resolution: RouteResolutionTrace
) -> RouteResolutionTrace:
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
        return route_resolution

    pending_terminus_ids: set[str] = set(graph._pending_terminus.keys())

    edges_by_source: dict[str, list[tuple[int, Edge]]] = {}
    for i, edge in enumerate(graph.edges):
        edges_by_source.setdefault(edge.source, []).append((i, edge))

    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
    bypass_replacements: dict[ResolvedEdge, list[tuple[ResolvedEdge, ...]]] = {}
    bypass_count = 0

    for section in graph.sections.values():
        station_ids = set(section.station_ids)
        if not station_ids:
            continue
        ctx = _build_bypass_section_ctx(graph, section, station_ids)
        if ctx is None:
            continue

        for sid in section.station_ids:
            bypass_by_pred = _station_bypass_groups(
                graph, sid, ctx, edges_by_source, pending_terminus_ids
            )
            for pred_id, bypass_edges in bypass_by_pred.items():
                bypass_count += 1
                v_id = f"{BYPASS_V_PREFIX}{sid}_{pred_id}_{bypass_count}"
                new_stations.append(
                    Station(
                        id=v_id,
                        label="",
                        section_id=section.id,
                        is_hidden=True,
                        bypasses_station_id=sid,
                    )
                )
                for idx, edge in bypass_edges:
                    edges_to_remove.add(idx)
                    first = Edge(source=edge.source, target=v_id, line_id=edge.line_id)
                    second = Edge(source=v_id, target=edge.target, line_id=edge.line_id)
                    new_edges.extend((first, second))
                    replaced = ResolvedEdge(edge.source, edge.target, edge.line_id)
                    bypass_replacements.setdefault(replaced, []).append(
                        (
                            ResolvedEdge(first.source, first.target, first.line_id),
                            ResolvedEdge(second.source, second.target, second.line_id),
                        )
                    )

    if not new_stations:
        return route_resolution

    for st in new_stations:
        graph.register_station(st)

    if edges_to_remove:
        graph.replace_edges(
            [e for i, e in enumerate(graph.edges) if i not in edges_to_remove]
        )
    for edge in new_edges:
        graph.add_edge(edge)

    return replace(
        route_resolution,
        authored_edges=_expand_resolved_authored_edges(
            route_resolution.authored_edges, bypass_replacements
        ),
    )


def _section_topo_layers(graph: MetroGraph, section_ids: set[str]) -> dict[str, int]:
    """Longest-path layer index for the in-section subgraph (empty if cyclic)."""
    station_ids = [sid for sid in graph.stations if sid in section_ids]
    section_graph = directed_graph(
        station_ids,
        (
            (edge.source, edge.target)
            for edge in graph.edges
            if edge.source in section_ids and edge.target in section_ids
        ),
    )
    try:
        return longest_path_layers(section_graph, station_ids)
    except nx.NetworkXUnfeasible:
        return {}


@dataclass
class _BypassSectionCtx:
    """Per-section state the bypass trigger reads for each candidate station."""

    station_ids: set[str]
    sec_layers: dict[str, int]
    exit_port_ids: set[str]
    in_preds_by_target: dict[str, set[str]]
    consumed_lines_by_target: dict[str, set[str]]
    in_section_edges: list[Edge]
    single_trunk_section: bool


def _build_bypass_section_ctx(
    graph: MetroGraph, section: Section, station_ids: set[str]
) -> _BypassSectionCtx | None:
    """Compute bypass context for one section, or None when it has no internals."""
    sec_layers = _section_topo_layers(graph, station_ids)
    exit_port_ids = set(section.exit_ports)
    # Pin exit ports past every internal station so longest-path layering
    # doesn't tie an exit port with an internal station sharing its
    # predecessor (which would suppress the bypass trigger).
    if exit_port_ids and sec_layers:
        internal_max = max(v for k, v in sec_layers.items() if k not in exit_port_ids)
        for pid in exit_port_ids:
            if sec_layers.get(pid, 0) <= internal_max:
                sec_layers[pid] = internal_max + 1

    in_section_edges = [
        e for e in graph.edges if e.source in station_ids and e.target in station_ids
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
        if sid not in exit_port_ids and sid not in entry_port_ids and sid in sec_layers
    ]
    if not internal_ids:
        return None
    min_internal_layer = min(sec_layers[sid] for sid in internal_ids)
    head_count = sum(1 for sid in internal_ids if sec_layers[sid] == min_internal_layer)

    return _BypassSectionCtx(
        station_ids=station_ids,
        sec_layers=sec_layers,
        exit_port_ids=exit_port_ids,
        in_preds_by_target=in_preds_by_target,
        consumed_lines_by_target=consumed_lines_by_target,
        in_section_edges=in_section_edges,
        single_trunk_section=head_count <= 1,
    )


def _station_bypass_groups(
    graph: MetroGraph,
    sid: str,
    ctx: _BypassSectionCtx,
    edges_by_source: dict[str, list[tuple[int, Edge]]],
    pending_terminus_ids: set[str],
) -> dict[str, list[tuple[int, Edge]]]:
    """Bypassing exit edges grouped by predecessor for one station, or empty."""
    station = graph.stations.get(sid)
    if station is None or station.is_port or station.is_hidden:
        return {}
    if station.is_terminus or sid in pending_terminus_ids:
        return {}

    s_layer = ctx.sec_layers.get(sid)
    if s_layer is None:
        return {}
    s_lines = set(graph.station_lines(sid))

    in_section_preds = ctx.in_preds_by_target.get(sid, set())
    # In multi-trunk sections, only fan-in convergence points (S has >=2
    # in-section predecessors) need bypass help, and only from a direct
    # predecessor of S - other lines already have their own parallel tracks.
    if not ctx.single_trunk_section and len(in_section_preds) < 2:
        return {}

    # Skip stations that only consume a spur line - the bypass would snap S's
    # spur track to the section trunk Y, opening an unnecessary vertical gap to
    # S.  A consumed line is "trunk" when it has at least one in-section edge
    # that doesn't touch S.
    consumed_lines = ctx.consumed_lines_by_target.get(sid, set())
    trunk_lines = {
        e.line_id for e in ctx.in_section_edges if e.source != sid and e.target != sid
    }
    if consumed_lines and not (consumed_lines & trunk_lines):
        return {}

    candidate_preds = sorted(
        ctx.station_ids if ctx.single_trunk_section else in_section_preds
    )

    bypass_by_pred: dict[str, list[tuple[int, Edge]]] = {}
    for pred_id in candidate_preds:
        if pred_id == sid:
            continue
        pred_layer = ctx.sec_layers.get(pred_id)
        if pred_layer is None or pred_layer >= s_layer:
            continue
        for i, edge in edges_by_source.get(pred_id, []):
            if edge.target not in ctx.exit_port_ids:
                continue
            if edge.line_id in s_lines:
                continue
            t_layer = ctx.sec_layers.get(edge.target)
            if t_layer is None or t_layer <= s_layer:
                continue
            bypass_by_pred.setdefault(pred_id, []).append((i, edge))
    return bypass_by_pred


@dataclass(frozen=True, slots=True)
class ResolvedConnectorEndpoint:
    """Authoritative section and boundary sides for one inter-section edge."""

    edge: Edge
    source_section: str
    target_section: str
    exit_selection: EndpointSideSelection
    entry_selection: EndpointSideSelection
    connector_ids: tuple[ConnectorId, ...]

    @property
    def exit_side(self) -> PortSide:
        return self.exit_selection.side

    @property
    def entry_side(self) -> PortSide:
        return self.entry_selection.side


@dataclass(slots=True)
class SectionEndpointResolution:
    """Pre-port edge classification and current boundary endpoints."""

    internal_edges: list[Edge]
    inter_section_edges: list[Edge]
    connectors: tuple[ResolvedConnectorEndpoint, ...]


def resolve_section_endpoints(
    graph: MetroGraph,
    lineage: AuthoredEdgeLineage,
    commitments: AppliedLayoutCommitments = AppliedLayoutCommitments(),
) -> SectionEndpointResolution:
    """Resolve inter-section endpoint sides once, before creating synthetic ports."""
    internal_edges, inter_section_edges = _classify_edges(graph)
    transitions = [
        *_reside_folded_flow_ports_to_grid(graph, inter_section_edges),
        *_reanchor_flow_axis_ports(graph, inter_section_edges),
    ]
    provenance = _build_endpoint_provenance_context(graph, transitions)
    entry_side_for_line = _build_entry_side_mapping(
        graph, inter_section_edges, provenance
    )
    exit_sides = _build_exit_side_mapping(graph)
    connectors = _group_inter_section_edges(
        graph,
        inter_section_edges,
        entry_side_for_line,
        exit_sides,
        lineage,
        provenance,
        commitments,
    )
    return SectionEndpointResolution(
        internal_edges=internal_edges,
        inter_section_edges=inter_section_edges,
        connectors=connectors,
    )


def _resolve_sections(
    graph: MetroGraph,
    resolution: SectionEndpointResolution,
    topology: RouteTopology,
    authored_edges: tuple[ResolvedAuthoredEdge, ...],
) -> RouteResolutionTrace:
    """Post-parse: classify edges, create ports, rewrite inter-section edges.

    Key design: ONE exit port per source section. All lines leaving a section
    exit together, ensuring consistent ordering. Junctions handle fan-out
    to multiple target sections. ONE entry port per target section per side
    (side from hints or LEFT default).
    """
    if resolution.inter_section_edges:
        trace = _create_ports_and_junctions(graph, resolution, topology, authored_edges)
    else:
        trace = RouteResolutionTrace(authored_edges=authored_edges)

    _assign_section_numbers(graph)
    return trace


_LEADING_SIDE = {
    "LR": PortSide.LEFT,
    "RL": PortSide.RIGHT,
    "TB": PortSide.TOP,
    "BT": PortSide.BOTTOM,
}
_TRAILING_SIDE = {
    "LR": PortSide.RIGHT,
    "RL": PortSide.LEFT,
    "TB": PortSide.BOTTOM,
    "BT": PortSide.TOP,
}
# Horizontal flows only: a vertical reversal re-seats the trailing exit on the
# far edge, and the route out of it wraps around the section and back through
# its target's interior, which the re-anchor remedy avoids.
_HORIZONTAL_FLOW_REVERSAL = {"LR": "RL", "RL": "LR"}


def _flow_axis_is_x(direction: str) -> bool:
    """Whether *direction* runs its flow along X (LR/RL) rather than Y (TB/BT)."""
    from nf_metro.layout.geometry import AxisFrame

    return AxisFrame.axes_for_direction(direction)[0] == "x"


def _connecting_flow_side(
    graph: MetroGraph, near_id: str, far_id: str
) -> PortSide | None:
    """The flow-axis side ``far_id`` sits on relative to ``near_id``.

    ``LEFT``/``RIGHT`` for a horizontal ``near_id`` (by grid column), ``TOP``/
    ``BOTTOM`` for a vertical one (by grid row); ``None`` when the two share the
    axis coordinate, so neither side is implied.

    Reads positions through :func:`_effective_grid_pos`: an explicit
    ``%%metro grid:`` directive lands in ``graph.grid_overrides`` at parse
    time, and only reaches ``Section.grid_col``/``grid_row`` later, in
    section placement, so this stage must not read those fields directly.
    """
    from nf_metro.layout.auto_layout import _effective_grid_pos

    near = graph.sections[near_id]
    near_col, near_row, *_ = _effective_grid_pos(graph, near_id)
    far_col, far_row, *_ = _effective_grid_pos(graph, far_id)
    if _flow_axis_is_x(near.direction):
        low, high = PortSide.LEFT, PortSide.RIGHT
        near_pos, far_pos = near_col, far_col
    else:
        low, high = PortSide.TOP, PortSide.BOTTOM
        near_pos, far_pos = near_row, far_row
    if far_pos < near_pos:
        return low
    if far_pos > near_pos:
        return high
    return None


def _section_flow_ranks(section: Section) -> dict[str, int]:
    """Longest-path rank of each station along a section's internal flow.

    Rank 0 is the flow-source end (leading edge); the maximum rank is the
    flow-sink end (trailing edge).  Returns an empty dict if the internal
    edges form a cycle.
    """
    station_ids = list(section.station_ids)
    station_set = set(station_ids)
    section_graph = directed_graph(
        station_ids,
        (
            (edge.source, edge.target)
            for edge in section.internal_edges
            if edge.source in station_set and edge.target in station_set
        ),
    )
    try:
        return longest_path_layers(section_graph, station_ids)
    except nx.NetworkXUnfeasible:
        return {}


def _port_fold_target(
    side: PortSide,
    endpoints: set[str],
    ranks: dict[str, int],
    leading: PortSide,
    trailing: PortSide,
    *,
    is_entry: bool,
) -> PortSide | None:
    """The flow-axis edge a port should sit on, or ``None`` if it doesn't fold.

    The connecting leg runs from an entry port toward its consumer, or from a
    producer toward an exit port.  It folds against the flow whenever the
    connecting station is not the one adjacent to the port's edge: an entry on
    the trailing edge whose consumer is any station other than the flow-sink
    must reach inward past it and the line doubles back, and symmetrically an
    exit on the leading edge whose producer is any station other than the
    flow-source.  Either returns the opposite flow-axis edge, where the leg
    arrives with the flow (and wraps over the section to get there); everything
    that runs with the flow, is cross-axis, or straddles ranks returns ``None``.
    """
    if side not in (leading, trailing) or not endpoints:
        return None
    lo, hi = min(ranks.values()), max(ranks.values())
    if is_entry and side == trailing and all(ranks[s] < hi for s in endpoints):
        return leading
    if not is_entry and side == leading and all(ranks[s] > lo for s in endpoints):
        return trailing
    return None


def _expected_flow_side(cols: set[int], col: int) -> PortSide | None:
    """The horizontal side a flow-axis port should face for its connections.

    ``LEFT``/``RIGHT`` when every connecting section sits strictly to one side of
    column ``col``; ``None`` when there are no connections or they straddle it.
    """
    if not cols:
        return None
    if all(c < col for c in cols):
        return PortSide.LEFT
    if all(c > col for c in cols):
        return PortSide.RIGHT
    return None


def _reside_folded_flow_ports_to_grid(
    graph: MetroGraph, inter_section_edges: list[Edge]
) -> tuple[EndpointSideTransition, ...]:
    """Turn a fold-relocated section's flow-axis port toward its connecting sections.

    A lowered fold threshold relocates sections onto a return row, which can
    leave a left/right port -- authored for the unfolded grid -- facing away from
    the column its connecting sections occupy; the connecting leg then doubles
    back across the section's own box.  For each section the fold compressed, a
    left/right entry is turned toward the column its producers sit in and a
    left/right exit toward its consumers, but only when those connecting sections
    all sit strictly to one horizontal side.  Running before the intra-section
    re-anchor (:func:`_reanchor_flow_axis_ports`) lets that pass see the corrected
    side and stop mistaking the relocated geometry for an internal fold.
    """
    relocated = graph._fold_compressed_sections
    if not relocated:
        return ()

    transitions: list[EndpointSideTransition] = []

    exit_cols: dict[tuple[str, str], set[int]] = defaultdict(set)
    entry_cols: dict[tuple[str, str], set[int]] = defaultdict(set)
    for e in inter_section_edges:
        src = graph.section_for_station(e.source)
        tgt = graph.section_for_station(e.target)
        if not src or not tgt or src == tgt:
            continue
        exit_cols[(src, e.line_id)].add(graph.sections[tgt].grid_col)
        entry_cols[(tgt, e.line_id)].add(graph.sections[src].grid_col)

    for sec_id in relocated:
        section = graph.sections[sec_id]
        if not _flow_axis_is_x(section.direction):
            continue
        col = section.grid_col
        for hints, cols_by_line, is_entry in (
            (section.entry_hints, entry_cols, True),
            (section.exit_hints, exit_cols, False),
        ):
            for idx, (side, lines) in enumerate(hints):
                if side not in (PortSide.LEFT, PortSide.RIGHT):
                    continue  # cross-axis port, not on the flow axis
                cols = {c for lid in lines for c in cols_by_line.get((sec_id, lid), ())}
                target = _expected_flow_side(cols, col)
                if target is None or side == target:
                    continue
                hints[idx] = (target, lines)
                transitions.append(
                    EndpointSideTransition(
                        section_id=sec_id,
                        role=(
                            ConnectorEndpointRole.ENTRY
                            if is_entry
                            else ConnectorEndpointRole.EXIT
                        ),
                        line_ids=tuple(lines),
                        previous=side,
                        effective=target,
                        reason=DecisionReason.FOLD_RELOCATED_SIDE,
                    )
                )
                warnings.warn(
                    f"Section '{sec_id}': {'entry' if is_entry else 'exit'} port "
                    f"declared on {side.value} but the fold placed its connecting "
                    f"sections to the {target.value}; re-anchored to "
                    f"{target.value} so the line does not wrap back across the "
                    f"section.",
                    stacklevel=2,
                )
    return tuple(transitions)


def _reanchor_flow_axis_ports(
    graph: MetroGraph, inter_section_edges: list[Edge]
) -> tuple[EndpointSideTransition, ...]:
    """Keep a flow-axis entry/exit port on the same end as its consumer/producer.

    A LEFT/RIGHT (LR/RL) or TOP/BOTTOM (TB) port lies on the section's flow
    axis.  When an entry sits on the edge opposite its consumer -- or an exit
    opposite its producer -- the connecting leg runs the length of the trunk
    and doubles straight back, folding through every intervening station
    (#885).  Two corrections resolve it: a horizontal section whose direction
    was inferred is re-oriented (LR<->RL) so the flow runs toward the declared
    port and the connecting station lands beside it; a section with an explicit
    direction (or a vertical TB one, which has no horizontal twin) instead has
    the offending port moved to its connecting station's own edge.  Ports that
    run with the flow, and cross-axis ports, are left alone.
    """
    transitions: list[EndpointSideTransition] = []
    consumers: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    producers: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    # The flow-axis side each endpoint's connecting section sits on, so a port's
    # fold decision can ignore a fold-in feed arriving from the far side.
    consumer_sides: dict[str, dict[str, dict[str, set[PortSide | None]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    producer_sides: dict[str, dict[str, dict[str, set[PortSide | None]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    for e in inter_section_edges:
        tgt_sec = graph.section_for_station(e.target)
        src_sec = graph.section_for_station(e.source)
        if tgt_sec:
            consumers[tgt_sec][e.line_id].add(e.target)
            if src_sec:
                side = _connecting_flow_side(graph, tgt_sec, src_sec)
                consumer_sides[tgt_sec][e.line_id][e.target].add(side)
        if src_sec:
            producers[src_sec][e.line_id].add(e.source)
            if tgt_sec:
                side = _connecting_flow_side(graph, src_sec, tgt_sec)
                producer_sides[src_sec][e.line_id][e.source].add(side)

    for sec_id, section in graph.sections.items():
        leading = _LEADING_SIDE.get(section.direction)
        trailing = _TRAILING_SIDE.get(section.direction)
        if leading is None or trailing is None:
            continue
        ranks = _section_flow_ranks(section)
        if not ranks or min(ranks.values()) == max(ranks.values()):
            continue

        folds: list[tuple[list[tuple[PortSide, list[str]]], int, PortSide]] = []
        with_flow = False
        for hints, ep_map, side_map, is_entry in (
            (section.entry_hints, consumers.get(sec_id, {}), consumer_sides, True),
            (section.exit_hints, producers.get(sec_id, {}), producer_sides, False),
        ):
            sides_for_sec = side_map.get(sec_id, {})
            for idx, (side, lines) in enumerate(hints):
                if side not in (leading, trailing):
                    continue
                eps = {s for lid in lines for s in ep_map.get(lid, set()) if s in ranks}
                if not eps:
                    continue
                # A port on the flow axis carries only feeds arriving from its own
                # side; a same-line feed folding in from the far side must not mask
                # the fold of the feeds this port actually rakes.  Scope to the
                # port's own-side (or axis-aligned) feeds, unless every feed comes
                # from the far side -- then the whole port faces the wrong way and
                # the unscoped set drives the re-orient/re-anchor below.
                opposite = leading if side == trailing else trailing
                own_side = {
                    s
                    for s in eps
                    if {
                        d
                        for lid in lines
                        for d in sides_for_sec.get(lid, {}).get(s, set())
                    }
                    != {opposite}
                }
                if own_side:
                    eps = own_side
                if (is_entry and side == leading) or (
                    not is_entry and side == trailing
                ):
                    with_flow = True
                target = _port_fold_target(
                    side, eps, ranks, leading, trailing, is_entry=is_entry
                )
                if target is not None:
                    folds.append((hints, idx, target))
        if not folds:
            continue

        # Re-orienting flips every flow extreme to the opposite edge: a port
        # running with the flow would be pushed into a fold, so a flip is only
        # safe when no flow-axis port runs with the flow.  A reversed port whose
        # connecting station sits at the flow extreme (so it does not itself
        # double back) becomes a with-flow port once flipped, so it does not
        # block re-orientation.
        if (
            section.direction in _HORIZONTAL_FLOW_REVERSAL
            and not graph.layout_provenance.direction_is_locked(sec_id)
            and not with_flow
        ):
            new_dir = _HORIZONTAL_FLOW_REVERSAL[section.direction]
            warnings.warn(
                f"Section '{sec_id}': flow re-oriented {section.direction}->"
                f"{new_dir} so its declared port faces its connecting section "
                f"instead of routing back through the section's stations.",
                stacklevel=2,
            )
            section.direction = new_dir
            graph.layout_provenance.record_inferred_direction(
                sec_id,
                new_dir,
                DecisionReason.FLOW_REORIENTED_DIRECTION,
                locked=True,
            )
        else:
            for hints, idx, target in folds:
                side, lines = hints[idx]
                hints[idx] = (target, lines)
                transitions.append(
                    EndpointSideTransition(
                        section_id=sec_id,
                        role=(
                            ConnectorEndpointRole.ENTRY
                            if hints is section.entry_hints
                            else ConnectorEndpointRole.EXIT
                        ),
                        line_ids=tuple(lines),
                        previous=side,
                        effective=target,
                        reason=DecisionReason.FLOW_REANCHORED_SIDE,
                    )
                )
                warnings.warn(
                    f"Section '{sec_id}': port declared on {side.value} but its "
                    f"connecting station sits at the {target.value} end; "
                    f"re-anchored to {target.value} so the line does not route "
                    f"back through the section's other stations.",
                    stacklevel=2,
                )
    return tuple(transitions)


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


def _collapse_hint_sides(
    section: Section, dominant: PortSide | None
) -> PortSide | None:
    """The single entry side a section's entry hints collapse to.

    A section has one entry side, so hints naming more than one side are
    contradictory input and must collapse to one approach.  The section enters
    where a feed actually arrives (``dominant``), else on its flow-natural side
    -- and warns, naming the sides it drops.  The chosen side may be one the
    author did not hint when that is where the feed lands (e.g. a fed member of
    a packed cell entered from the edge its dominant feeder drops onto); the
    :func:`_guard_entry_port_not_opposite_targets` invariant is what forbids a
    genuinely flow-opposing entry, not the hint set.  Returns ``None`` for a
    section with no entry hints.
    """
    if not section.entry_hints:
        return None
    unique = {s for s, _ in section.entry_hints}
    if len(unique) == 1:
        return next(iter(unique))
    chosen = dominant or _natural_entry_side(section.direction)
    dropped = sorted(s.name for s in unique if s is not chosen)
    warnings.warn(
        f"section {section.id!r}: entry hints name multiple sides; keeping "
        f"{chosen.name}, dropping {', '.join(dropped)}",
        stacklevel=2,
    )
    return chosen


_SIDE_PRIORITY = (PortSide.LEFT, PortSide.RIGHT, PortSide.TOP, PortSide.BOTTOM)


def _priority_side(sides: set[PortSide]) -> PortSide:
    """Pick a single side from a set by a fixed priority (LEFT > RIGHT > ...)."""
    for side in _SIDE_PRIORITY:
        if side in sides:
            return side
    return PortSide.LEFT


def _dominant_entry_side(graph: MetroGraph, sec_id: str) -> PortSide | None:
    """Single entry side for a section, chosen from predecessor feed geometry.

    Each predecessor votes for the side of ``sec_id`` that faces it
    (``_relative_side`` on their grid cells).  The flow-natural side wins
    whenever it receives a feed -- a left-to-right section keeps a left entry
    unless nothing arrives there -- so internal flow enters at its source and
    off-side feeds route around.  Otherwise the most-fed side wins, ties broken
    by ``_priority_side``.  Returns ``None`` when no predecessor with known grid
    geometry feeds the section, leaving the caller to fall back.
    """
    from nf_metro.layout.auto_layout import _effective_grid_pos, _neighbour_side_votes

    dag = graph.section_dag
    if dag is None:
        return None
    my_col, _my_row, _row_span, _my_col_span = _effective_grid_pos(graph, sec_id)
    if my_col < 0:
        return None
    votes = _neighbour_side_votes(
        graph,
        sec_id,
        dag.predecessors.get(sec_id, set()),
        dag.edge_lines,
        edge_key=lambda src: (src, sec_id),
        skip_unplaced=True,
    )
    if not votes:
        return None
    natural = _natural_entry_side(graph.sections[sec_id].direction)
    if natural in votes:
        return natural
    best = max(votes.values())
    return _priority_side({s for s, count in votes.items() if count == best})


_LineEndpointKey = tuple[str, ConnectorEndpointRole, str]


@dataclass(frozen=True, slots=True)
class _EndpointProvenanceContext:
    """Resolver-local indexes for classifying endpoint decisions."""

    authored_line_sides: dict[_LineEndpointKey, tuple[PortSide, ...]]
    authored_endpoint_values: dict[ConnectorEndpointKey, tuple[PortSide, ...]]
    transition_reasons: dict[_LineEndpointKey, DecisionReason]


def _build_endpoint_provenance_context(
    graph: MetroGraph,
    transitions: tuple[EndpointSideTransition, ...] | list[EndpointSideTransition],
) -> _EndpointProvenanceContext:
    authored = graph.layout_provenance.authored
    authored_line_sides = authored.port_hint_index() if authored is not None else {}
    authored_endpoint_values = (
        authored.endpoint_values_index() if authored is not None else {}
    )
    transition_reasons: dict[_LineEndpointKey, DecisionReason] = {}
    for transition in transitions:
        for line_id in transition.line_ids:
            transition_reasons[(transition.section_id, transition.role, line_id)] = (
                transition.reason
            )
    return _EndpointProvenanceContext(
        authored_line_sides,
        authored_endpoint_values,
        transition_reasons,
    )


def _endpoint_selection(
    provenance: _EndpointProvenanceContext,
    section_id: str,
    role: ConnectorEndpointRole,
    line_id: str,
    side: PortSide,
    *,
    shared_connector: bool = False,
) -> EndpointSideSelection:
    """Classify a resolved side without inferring ownership from hint presence."""
    key = (section_id, role, line_id)
    transition = provenance.transition_reasons.get(key)
    if transition is not None:
        return EndpointSideSelection(side, DecisionOrigin.INFERRED, True, transition)

    authored = provenance.authored_line_sides.get(key, ())
    if authored and all(value is side for value in authored):
        return EndpointSideSelection(
            side,
            DecisionOrigin.AUTHORED,
            True,
            DecisionReason.AUTHOR_DIRECTIVE,
        )
    if authored:
        return EndpointSideSelection(
            side,
            DecisionOrigin.INFERRED,
            False,
            DecisionReason.RESOLUTION_SIDE_SELECTION,
        )
    if shared_connector:
        return EndpointSideSelection(
            side,
            DecisionOrigin.INFERRED,
            False,
            DecisionReason.SHARED_CONNECTOR_ENTRY_SIDE,
        )
    return EndpointSideSelection(
        side,
        DecisionOrigin.INFERRED,
        False,
        (
            DecisionReason.AUTO_ENTRY_SIDE
            if role is ConnectorEndpointRole.ENTRY
            else DecisionReason.AUTO_EXIT_SIDE
        ),
    )


def _build_entry_side_mapping(
    graph: MetroGraph,
    inter_section_edges: list[Edge],
    provenance: _EndpointProvenanceContext | None = None,
) -> dict[tuple[str, str], EndpointSideSelection]:
    """Build a per-line entry side lookup from hints and feed geometry.

    A line resolves to one entry side:

    - a line named on the section's entry hints uses the section's hinted side.
      A section has one entry side, so hints naming several sides are
      contradictory: they collapse to one side (:func:`_collapse_hint_sides`),
      the rest dropped with a warning;
    - an unhinted line riding the same (source station, target station)
      connector as a hinted line takes that hinted side too -- they cross the
      section boundary on the same edge and cannot arrive on different sides;
    - any other unhinted line takes its side from where its feeds arrive
      (``_dominant_entry_side``), falling back to the LEFT default at lookup
      (left unmapped here) when no predecessor geometry is available.

    Lines arriving via genuinely distinct connectors may resolve to different
    sides; a section fed from more than one approach direction is caught
    downstream by ``_guard_no_mixed_entry_directions``.  Returns dict mapping
    (section_id, line_id) -> PortSide.
    """
    if provenance is None:
        provenance = _build_endpoint_provenance_context(graph, ())
    dag = graph.section_dag
    connectors_by_line: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for edge in inter_section_edges:
        tgt_sec = graph.section_for_station(edge.target)
        if tgt_sec is not None:
            connectors_by_line[(tgt_sec, edge.line_id)].add((edge.source, edge.target))

    entry_side_for_line: dict[tuple[str, str], EndpointSideSelection] = {}
    for sec_id, section in graph.sections.items():
        incoming: set[str] = set()
        if dag is not None:
            for src in dag.predecessors.get(sec_id, set()):
                incoming.update(dag.edge_lines.get((src, sec_id), set()))
        hinted_lines: set[str] = set()
        for _hint_side, line_ids in section.entry_hints:
            hinted_lines.update(line_ids)
        incoming |= hinted_lines
        if not incoming:
            continue

        dominant = _dominant_entry_side(graph, sec_id)
        hinted_side = _collapse_hint_sides(section, dominant)

        hinted_connectors: set[tuple[str, str]] = set()
        for lid in hinted_lines:
            hinted_connectors.update(connectors_by_line.get((sec_id, lid), set()))

        for lid in incoming:
            shares_hinted_connector = hinted_side is not None and bool(
                connectors_by_line.get((sec_id, lid), set()) & hinted_connectors
            )
            side = (
                hinted_side
                if lid in hinted_lines or shares_hinted_connector
                else dominant
            )
            if side is not None:
                entry_side_for_line[(sec_id, lid)] = _endpoint_selection(
                    provenance,
                    sec_id,
                    ConnectorEndpointRole.ENTRY,
                    lid,
                    side,
                    shared_connector=shares_hinted_connector
                    and lid not in hinted_lines,
                )
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


def _build_exit_side_mapping(
    graph: MetroGraph,
) -> dict[tuple[str, str], set[PortSide]]:
    """Build per-line exit side options from exit hints.

    Maps (section_id, line_id) -> the set of sides that line may exit by.
    A line declared on more than one side (e.g. ``exit: right`` plus
    ``exit: bottom``) leaves by whichever side faces a given target; a line
    on a single side always uses it, routing around when that side does not
    face the target.
    """
    exit_sides: dict[tuple[str, str], set[PortSide]] = {}
    for sec_id, section in graph.sections.items():
        for side, line_ids in section.exit_hints:
            for lid in line_ids:
                exit_sides.setdefault((sec_id, lid), set()).add(side)
    return exit_sides


_PERP_DROP_PAIR = {
    PortSide.BOTTOM: PortSide.TOP,
    PortSide.TOP: PortSide.BOTTOM,
}


def _exit_side_for_edge(
    graph: MetroGraph,
    edge: Edge,
    src_sec: str,
    tgt_sec: str,
    exit_sides: dict[tuple[str, str], set[PortSide]],
    entry_side_for_line: dict[tuple[str, str], EndpointSideSelection],
    provenance: _EndpointProvenanceContext,
    entry_override: EndpointSideSelection | None = None,
) -> EndpointSideSelection:
    """Choose the exit side an inter-section edge leaves its source by.

    A perpendicular (TOP/BOTTOM) exit forms a clean vertical drop only when it
    pairs with the target's perpendicular entry (BOTTOM exit into a TOP entry,
    or TOP exit into a BOTTOM entry).  Such an edge gets its own perpendicular
    port and drops straight in.  Every other edge collapses to the section's
    single exit side -- the dominant side when one is declared, RIGHT when
    several are -- so folds (a BOTTOM exit into a sideways LEFT entry) and
    flow-aligned exits keep one shared port and route around.
    """
    from nf_metro.layout.auto_layout import _relative_side

    sides = exit_sides.get((src_sec, edge.line_id))
    if not sides:
        return _endpoint_selection(
            provenance,
            src_sec,
            ConnectorEndpointRole.EXIT,
            edge.line_id,
            PortSide.RIGHT,
        )

    entry_selection = entry_override or entry_side_for_line.get((tgt_sec, edge.line_id))
    entry_side = entry_selection.side if entry_selection is not None else PortSide.LEFT

    preferred: PortSide | None
    if len(sides) == 1:
        preferred = next(iter(sides))
    else:
        src = graph.sections[src_sec]
        tgt = graph.sections[tgt_sec]
        geo = _relative_side(
            src.grid_col,
            src.grid_row,
            tgt.grid_col,
            tgt.grid_row,
            src.grid_col_span,
            tgt.grid_col_span,
        )
        preferred = geo if geo in sides else None

    if preferred in _PERP_DROP_PAIR and _PERP_DROP_PAIR[preferred] == entry_side:
        selected = preferred
    else:
        section_sides = {s for s, _ in graph.sections[src_sec].exit_hints}
        selected = (
            next(iter(section_sides)) if len(section_sides) == 1 else PortSide.RIGHT
        )

    return _endpoint_selection(
        provenance,
        src_sec,
        ConnectorEndpointRole.EXIT,
        edge.line_id,
        selected,
    )


def _group_inter_section_edges(
    graph: MetroGraph,
    inter_section_edges: list[Edge],
    entry_side_for_line: dict[tuple[str, str], EndpointSideSelection],
    exit_sides: dict[tuple[str, str], set[PortSide]],
    lineage: AuthoredEdgeLineage,
    provenance: _EndpointProvenanceContext,
    commitments: AppliedLayoutCommitments,
) -> tuple[ResolvedConnectorEndpoint, ...]:
    """Resolve current inter-section endpoints and their authored identities."""
    connectors: list[ResolvedConnectorEndpoint] = []
    commitment_index = {item.endpoint: item.selection for item in commitments.endpoints}

    for edge in inter_section_edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)
        # _classify_edges only files an edge as inter-section when both
        # endpoints resolve to a section, so neither lookup is None here.
        assert src_sec is not None and tgt_sec is not None
        connector_ids = tuple(key.id for key in lineage.origins(edge))
        entry_selection = entry_side_for_line.get((tgt_sec, edge.line_id))
        if entry_selection is None:
            entry_selection = _endpoint_selection(
                provenance,
                tgt_sec,
                ConnectorEndpointRole.ENTRY,
                edge.line_id,
                PortSide.LEFT,
            )
        committed_entry = commitments.selection_for(
            connector_ids,
            ConnectorEndpointRole.ENTRY,
            commitment_index,
        )
        if committed_entry is not None:
            entry_selection = committed_entry
        committed_exit = commitments.selection_for(
            connector_ids,
            ConnectorEndpointRole.EXIT,
            commitment_index,
        )
        exit_selection = _exit_side_for_edge(
            graph,
            edge,
            src_sec,
            tgt_sec,
            exit_sides,
            entry_side_for_line,
            provenance,
            committed_entry,
        )
        if committed_exit is not None:
            exit_selection = committed_exit

        for connector_id in connector_ids:
            exit_endpoint = graph.layout_provenance.endpoint_key(
                connector_id, ConnectorEndpointRole.EXIT
            )
            graph.layout_provenance.record_endpoint_selection(
                exit_endpoint,
                exit_selection,
                provenance.authored_endpoint_values.get(exit_endpoint, ()),
            )
            entry_endpoint = graph.layout_provenance.endpoint_key(
                connector_id, ConnectorEndpointRole.ENTRY
            )
            graph.layout_provenance.record_endpoint_selection(
                entry_endpoint,
                entry_selection,
                provenance.authored_endpoint_values.get(entry_endpoint, ()),
            )

        connectors.append(
            ResolvedConnectorEndpoint(
                edge=edge,
                source_section=src_sec,
                target_section=tgt_sec,
                exit_selection=exit_selection,
                entry_selection=entry_selection,
                connector_ids=connector_ids,
            )
        )

    return tuple(connectors)


def _create_port_stations(
    graph: MetroGraph,
    topology: RouteTopology,
) -> tuple[
    dict[EndpointGroupId, str],
    dict[EndpointGroupId, str],
    int,
]:
    """Create exit and entry port stations on the graph.

    A section gets one exit port per side it leaves by, so a line declared on
    more than one side (e.g. ``exit: right`` plus ``exit: bottom``) emits from
    each.  Returns (exit_port_map, entry_port_map, next_port_counter).
    """
    port_counter = 0
    exit_port_map: dict[EndpointGroupId, str] = {}

    for group in topology.exit_groups:
        port_id = f"{group.section_id}__exit_{group.side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=group.section_id,
            side=group.side,
            is_entry=False,
        )
        graph.add_port(port)
        exit_port_map[group.id] = port_id
        port_counter += 1

    entry_port_map: dict[EndpointGroupId, str] = {}

    for group in topology.entry_groups:
        port_id = f"{group.section_id}__entry_{group.side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=group.section_id,
            side=group.side,
            is_entry=True,
        )
        graph.add_port(port)
        entry_port_map[group.id] = port_id
        port_counter += 1

    return exit_port_map, entry_port_map, port_counter


@dataclass(slots=True)
class _BoundaryRewriteState:
    """Mutable resolver state indexed by immutable topology identities."""

    exit_ports: dict[EndpointGroupId, str]
    entry_ports: dict[EndpointGroupId, str]
    connectors: dict[ConnectorId, RouteConnector]
    divergences_by_exit: dict[EndpointGroupId, DivergenceGroup]
    connector_paths: dict[ConnectorId, list[list[ResolvedEdge]]]
    divergence_junctions: dict[DivergenceId, str]
    fan_edge_order: list[tuple[ResolvedEdge, DivergenceId, EndpointGroupId]]

    def replace_connector_edge(
        self,
        connector_ids: tuple[ConnectorId, ...],
        old_edge: ResolvedEdge,
        replacement: tuple[ResolvedEdge, ...],
    ) -> None:
        """Replace one resolved edge in every named connector path."""
        for connector_id in connector_ids:
            replaced = False
            for path in self.connector_paths[connector_id]:
                for index, edge in enumerate(path):
                    if edge != old_edge:
                        continue
                    path[index : index + 1] = replacement
                    replaced = True
            if not replaced:
                raise ValueError("connector path is missing its convergence edge")


def _rewrite_edges_with_junctions(
    graph: MetroGraph,
    resolution: SectionEndpointResolution,
    topology: RouteTopology,
    exit_port_map: dict[EndpointGroupId, str],
    entry_port_map: dict[EndpointGroupId, str],
    port_counter: int,
) -> _BoundaryRewriteState:
    """Rewrite inter-section edges into 3-part chains with junctions."""
    new_edges: list[Edge] = list(resolution.internal_edges)
    state = _BoundaryRewriteState(
        exit_ports=exit_port_map,
        entry_ports=entry_port_map,
        connectors={connector.id: connector for connector in topology.connectors},
        divergences_by_exit={
            divergence.exit_group_id: divergence for divergence in topology.divergences
        },
        connector_paths={},
        divergence_junctions={},
        fan_edge_order=[],
    )
    connectors_by_id = state.connectors
    divergence_by_exit = state.divergences_by_exit
    boundary_groups: dict[
        EndpointGroupId,
        dict[EndpointGroupId, list[ResolvedConnectorEndpoint]],
    ] = {}

    for endpoint in resolution.connectors:
        connector_ids = endpoint.connector_ids
        if not connector_ids:
            raise ValueError("boundary connector has no authored topology identity")
        try:
            topology_connectors = [connectors_by_id[item] for item in connector_ids]
        except KeyError as error:
            raise ValueError(
                "boundary connector lineage is absent from RouteTopology"
            ) from error
        exit_group_ids = {item.exit_group_id for item in topology_connectors}
        entry_group_ids = {item.entry_group_id for item in topology_connectors}
        line_ids = {item.line_id for item in topology_connectors}
        if (
            len(exit_group_ids) != 1
            or len(entry_group_ids) != 1
            or line_ids != {endpoint.edge.line_id}
        ):
            raise ValueError("boundary connector lineage disagrees with RouteTopology")
        exit_group_id = next(iter(exit_group_ids))
        entry_group_id = next(iter(entry_group_ids))
        exit_port_id = exit_port_map[exit_group_id]
        entry_port_id = entry_port_map[entry_group_id]
        edge = endpoint.edge

        new_edges.append(
            Edge(source=edge.source, target=exit_port_id, line_id=edge.line_id)
        )
        new_edges.append(
            Edge(source=entry_port_id, target=edge.target, line_id=edge.line_id)
        )
        boundary_groups.setdefault(exit_group_id, {}).setdefault(
            entry_group_id, []
        ).append(endpoint)

    fan_edge_metadata: dict[ResolvedEdge, tuple[DivergenceId, EndpointGroupId]] = {}
    for exit_group_id, entry_targets in boundary_groups.items():
        exit_port_id = exit_port_map[exit_group_id]
        divergence = divergence_by_exit.get(exit_group_id)
        if divergence is None:
            if len(entry_targets) != 1:
                raise ValueError("resolved fan-out is absent from RouteTopology")
            for entry_group_id, endpoints in entry_targets.items():
                entry_port_id = entry_port_map[entry_group_id]
                for endpoint in endpoints:
                    edge = endpoint.edge
                    new_edges.append(
                        Edge(
                            source=exit_port_id,
                            target=entry_port_id,
                            line_id=edge.line_id,
                        )
                    )
                    path = [
                        ResolvedEdge(edge.source, exit_port_id, edge.line_id),
                        ResolvedEdge(exit_port_id, entry_port_id, edge.line_id),
                        ResolvedEdge(entry_port_id, edge.target, edge.line_id),
                    ]
                    for connector_id in endpoint.connector_ids:
                        state.connector_paths[connector_id] = [path.copy()]
        else:
            if len(entry_targets) <= 1 or set(divergence.entry_group_ids) != set(
                entry_targets
            ):
                raise ValueError("resolved fan-out disagrees with RouteTopology")
            junction_id = f"__junction_{port_counter}"
            port_counter += 1
            junction = Station(id=junction_id, label="", is_port=True, section_id=None)
            graph.add_station(junction)
            graph.add_junction(junction_id)
            state.divergence_junctions[divergence.id] = junction_id

            fan_line_ids = sorted(
                {connectors_by_id[item].line_id for item in divergence.connector_ids}
            )
            for lid in fan_line_ids:
                new_edges.append(
                    Edge(source=exit_port_id, target=junction_id, line_id=lid)
                )

            for entry_group_id, endpoints in entry_targets.items():
                entry_port_id = entry_port_map[entry_group_id]
                for endpoint in endpoints:
                    edge = endpoint.edge
                    new_edges.append(
                        Edge(
                            source=junction_id,
                            target=entry_port_id,
                            line_id=edge.line_id,
                        )
                    )
                    middle = ResolvedEdge(junction_id, entry_port_id, edge.line_id)
                    fan_edge_metadata.setdefault(
                        middle, (divergence.id, entry_group_id)
                    )
                    path = [
                        ResolvedEdge(edge.source, exit_port_id, edge.line_id),
                        ResolvedEdge(exit_port_id, junction_id, edge.line_id),
                        middle,
                        ResolvedEdge(entry_port_id, edge.target, edge.line_id),
                    ]
                    for connector_id in endpoint.connector_ids:
                        state.connector_paths[connector_id] = [path.copy()]

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
    state.fan_edge_order = [
        (ResolvedEdge(edge.source, edge.target, edge.line_id), *fan_edge_metadata[key])
        for edge in deduped
        if (key := ResolvedEdge(edge.source, edge.target, edge.line_id))
        in fan_edge_metadata
    ]
    if set(state.connector_paths) != set(connectors_by_id):
        raise ValueError("not every topology connector resolved to a synthetic chain")
    return state


def _create_ports_and_junctions(
    graph: MetroGraph,
    resolution: SectionEndpointResolution,
    topology: RouteTopology,
    authored_edges: tuple[ResolvedAuthoredEdge, ...],
) -> RouteResolutionTrace:
    """Create exit/entry ports and junctions, rewrite inter-section edges.

    Creates one exit port per (source_section, exit_side), one entry port per
    (target_section, entry_side), and inserts junction stations where an exit
    port fans out to multiple entry ports.
    """
    exit_port_map, entry_port_map, port_counter = _create_port_stations(graph, topology)
    state = _rewrite_edges_with_junctions(
        graph,
        resolution,
        topology,
        exit_port_map,
        entry_port_map,
        port_counter,
    )
    convergence_junctions = _insert_merge_junctions(graph, topology, state)
    frozen_paths: dict[
        tuple[tuple[ResolvedEdge, ...], ...],
        tuple[tuple[ResolvedEdge, ...], ...],
    ] = {}

    def connector_paths(
        connector_id: ConnectorId,
    ) -> tuple[tuple[ResolvedEdge, ...], ...]:
        paths = tuple(tuple(path) for path in state.connector_paths[connector_id])
        return frozen_paths.setdefault(paths, paths)

    endpoint_by_connector: dict[ConnectorId, ResolvedConnectorEndpoint] = {}
    for endpoint in resolution.connectors:
        for connector_id in endpoint.connector_ids:
            if connector_id in endpoint_by_connector:
                raise ValueError("authored connector has multiple boundary edges")
            endpoint_by_connector[connector_id] = endpoint

    resolved_authored_edges: list[ResolvedAuthoredEdge] = []
    for record in authored_edges:
        connector_endpoint = endpoint_by_connector.get(record.authored_edge_id)
        if connector_endpoint is None:
            paths = record.edge_paths
        else:
            current_edge = ResolvedEdge(
                connector_endpoint.edge.source,
                connector_endpoint.edge.target,
                connector_endpoint.edge.line_id,
            )
            paths = _expand_resolved_paths(
                record.edge_paths,
                {current_edge: list(connector_paths(record.authored_edge_id))},
            )
            if paths is record.edge_paths:
                raise ValueError("authored connector path is missing its boundary edge")
        paths = frozen_paths.setdefault(paths, paths)
        resolved_authored_edges.append(
            record
            if paths is record.edge_paths
            else ResolvedAuthoredEdge(record.authored_edge_id, paths)
        )

    if set(endpoint_by_connector) != set(state.connectors):
        raise ValueError("not every topology connector has one boundary edge")

    return RouteResolutionTrace(
        authored_edges=tuple(resolved_authored_edges),
        exit_ports=tuple(
            ResolvedEndpointPort(group.id, exit_port_map[group.id])
            for group in topology.exit_groups
        ),
        entry_ports=tuple(
            ResolvedEndpointPort(group.id, entry_port_map[group.id])
            for group in topology.entry_groups
        ),
        divergences=tuple(
            ResolvedDivergence(group.id, state.divergence_junctions[group.id])
            for group in topology.divergences
        ),
        convergences=tuple(
            ResolvedConvergence(group.id, convergence_junctions[group.id])
            for group in topology.convergences
        ),
    )


def _insert_merge_junctions(
    graph: MetroGraph,
    topology: RouteTopology,
    state: _BoundaryRewriteState,
) -> dict[ConvergenceId, str]:
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
    convergence_by_key = {
        (group.entry_group_id, group.line_id): group for group in topology.convergences
    }
    ordered_groups: dict[
        tuple[EndpointGroupId, str],
        list[tuple[ResolvedEdge, DivergenceId]],
    ] = {}
    for edge, divergence_id, entry_group_id in state.fan_edge_order:
        key = (entry_group_id, edge.line_id)
        if key in convergence_by_key:
            ordered_groups.setdefault(key, []).append((edge, divergence_id))

    if set(ordered_groups) != set(convergence_by_key):
        raise ValueError("resolved merge groups disagree with RouteTopology")

    counter = len(graph.junctions)
    edges_to_remove: set[tuple[str, str, str]] = set()
    new_edges: list[Edge] = []
    convergence_junctions: dict[ConvergenceId, str] = {}

    for (entry_group_id, line_id), edges in ordered_groups.items():
        convergence = convergence_by_key[(entry_group_id, line_id)]
        if {divergence_id for _, divergence_id in edges} != set(
            convergence.divergence_ids
        ):
            raise ValueError("resolved merge membership disagrees with RouteTopology")
        entry_port_id = state.entry_ports[entry_group_id]
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
        convergence_junctions[convergence.id] = merge_id

        for edge, _divergence_id in edges:
            edges_to_remove.add(edge)
            new_edges.append(Edge(source=edge.source, target=merge_id, line_id=line_id))

        # One edge: merge junction -> entry port
        new_edges.append(Edge(source=merge_id, target=entry_port_id, line_id=line_id))

    # Apply edge rewrites
    kept = [
        e for e in graph.edges if (e.source, e.target, e.line_id) not in edges_to_remove
    ]
    graph.replace_edges(kept + new_edges)

    for convergence in topology.convergences:
        merge_id = convergence_junctions[convergence.id]
        entry_port_id = state.entry_ports[convergence.entry_group_id]
        for connector_id in convergence.connector_ids:
            connector = state.connectors[connector_id]
            divergence = state.divergences_by_exit[connector.exit_group_id]
            junction_id = state.divergence_junctions[divergence.id]
            old_edge = ResolvedEdge(junction_id, entry_port_id, convergence.line_id)
            replacement = (
                ResolvedEdge(junction_id, merge_id, convergence.line_id),
                ResolvedEdge(merge_id, entry_port_id, convergence.line_id),
            )
            state.replace_connector_edge((connector_id,), old_edge, replacement)

    return convergence_junctions
