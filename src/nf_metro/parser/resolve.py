"""Post-parse graph rewrites.

After the driver in :mod:`nf_metro.parser.mermaid` has applied every statement,
these helpers reshape the :class:`MetroGraph`: dropping empty sections, wrapping
loose stations in an implicit section, inserting convergence/bypass/merge
junctions, and resolving inter-section edges into port-to-port chains.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import networkx as nx

from nf_metro.parser.model import (
    Edge,
    MetroGraph,
    Port,
    PortSide,
    Section,
    Station,
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


def _expand_interchanges(graph: MetroGraph) -> None:
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
            warnings.warn(
                f"interchange: node {ic.node_id!r} is not a defined station; ignoring",
                stacklevel=2,
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
            warnings.warn(
                f"interchange: node {ic.node_id!r} resolves to fewer than two "
                "rails carrying its lines; ignoring",
                stacklevel=2,
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
    graph.replace_edges(
        [
            Edge(
                source=moved.get((e.source, e.line_id), e.source),
                target=moved.get((e.target, e.line_id), e.target),
                line_id=e.line_id,
            )
            for e in graph.edges
        ]
    )


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

    pending_terminus_ids: set[str] = set(graph._pending_terminus.keys())

    edges_by_source: dict[str, list[tuple[int, Edge]]] = {}
    for i, edge in enumerate(graph.edges):
        edges_by_source.setdefault(edge.source, []).append((i, edge))

    new_stations: list[Station] = []
    new_edges: list[Edge] = []
    edges_to_remove: set[int] = set()
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
                v_id = f"__bypass_{sid}_{pred_id}_{bypass_count}"
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


def _section_topo_layers(graph: MetroGraph, section_ids: set[str]) -> dict[str, int]:
    """Longest-path layer index for the in-section subgraph (empty if cyclic)."""
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
        layers[node] = max((layers[p] for p in preds), default=-1) + 1 if preds else 0
    return layers


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
    entry_side_for_line: dict[tuple[str, str], PortSide],
) -> PortSide:
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
        return PortSide.RIGHT

    entry_side = entry_side_for_line.get((tgt_sec, edge.line_id), PortSide.LEFT)

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
        return preferred

    section_sides = {s for s, _ in graph.sections[src_sec].exit_hints}
    return next(iter(section_sides)) if len(section_sides) == 1 else PortSide.RIGHT


def _group_inter_section_edges(
    graph: MetroGraph,
    inter_section_edges: list[Edge],
    entry_side_for_line: dict[tuple[str, str], PortSide],
    exit_sides: dict[tuple[str, str], set[PortSide]],
) -> tuple[
    dict[tuple[str, PortSide], list[Edge]],
    dict[tuple[str, PortSide], list[Edge]],
]:
    """Group inter-section edges by (exit section, side) and (entry section, side)."""
    exit_group_edges: dict[tuple[str, PortSide], list[Edge]] = {}
    entry_group_edges: dict[tuple[str, PortSide], list[Edge]] = {}

    for edge in inter_section_edges:
        src_sec = graph.section_for_station(edge.source)
        tgt_sec = graph.section_for_station(edge.target)
        # _classify_edges only files an edge as inter-section when both
        # endpoints resolve to a section, so neither lookup is None here.
        assert src_sec is not None and tgt_sec is not None
        entry_side = entry_side_for_line.get((tgt_sec, edge.line_id), PortSide.LEFT)
        exit_side = _exit_side_for_edge(
            graph, edge, src_sec, tgt_sec, exit_sides, entry_side_for_line
        )

        exit_group_edges.setdefault((src_sec, exit_side), []).append(edge)
        entry_group_edges.setdefault((tgt_sec, entry_side), []).append(edge)

    return exit_group_edges, entry_group_edges


def _create_port_stations(
    graph: MetroGraph,
    exit_group_edges: dict[tuple[str, PortSide], list[Edge]],
    entry_group_edges: dict[tuple[str, PortSide], list[Edge]],
) -> tuple[dict[tuple[str, PortSide], str], dict[tuple[str, PortSide], str], int]:
    """Create exit and entry port stations on the graph.

    A section gets one exit port per side it leaves by, so a line declared on
    more than one side (e.g. ``exit: right`` plus ``exit: bottom``) emits from
    each.  Returns (exit_port_map, entry_port_map, next_port_counter).
    """
    port_counter = 0
    exit_port_map: dict[tuple[str, PortSide], str] = {}

    for sec_id, side in exit_group_edges:
        port_id = f"{sec_id}__exit_{side.value}_{port_counter}"
        port = Port(
            id=port_id,
            section_id=sec_id,
            side=side,
            is_entry=False,
        )
        graph.add_port(port)
        exit_port_map[(sec_id, side)] = port_id
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
    exit_sides: dict[tuple[str, str], set[PortSide]],
    exit_port_map: dict[tuple[str, PortSide], str],
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
        exit_side = _exit_side_for_edge(
            graph, edge, src_sec, tgt_sec, exit_sides, entry_side_for_line
        )

        exit_port_id = exit_port_map[(src_sec, exit_side)]
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

    Creates one exit port per (source_section, exit_side), one entry port per
    (target_section, entry_side), and inserts junction stations where an exit
    port fans out to multiple entry ports.
    """
    exit_sides = _build_exit_side_mapping(graph)
    exit_groups, entry_groups = _group_inter_section_edges(
        graph, inter_section_edges, entry_side_for_line, exit_sides
    )
    exit_port_map, entry_port_map, port_counter = _create_port_stations(
        graph, exit_groups, entry_groups
    )
    _rewrite_edges_with_junctions(
        graph,
        internal_edges,
        inter_section_edges,
        entry_side_for_line,
        exit_sides,
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
