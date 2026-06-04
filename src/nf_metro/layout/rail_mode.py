"""Opt-in "rail mode" layout (the nf-core/sarek subway idiom).

In normal layout a station shared by several metro lines is a single point
and the lines converge (bundle) to that one Y.  Rail mode instead lays each
line out as a fixed, evenly-spaced horizontal *rail* across the section and
renders a multi-line station as the classic metro *interchange*: a circle on
each rail the station uses, joined by a straight connector segment.  The
rails do not converge: a line runs straight along its rail and only the
interchange connector bridges across rails.

This module is a self-contained pipeline, run by ``compute_layout`` only when
``MetroGraph.rail_mode`` is True, so the normal layout path is untouched.

Scope (MVP): LR sections.  Each section's lines get rails centred about the
section trunk Y; stations are placed by longest-path layer (X) and anchored to
span their lines' rail range (Y).  Sections are stacked vertically in grid-row
order.  Ports/junctions are positioned at their connecting rail Y so that the
dedicated rail-mode router (see ``routing/rail.py``) can draw straight rails.
"""

from __future__ import annotations

__all__ = ["compute_rail_layout"]

from nf_metro.layout.constants import (
    SECTION_HEADER_PROTRUSION,
    SECTION_X_PADDING,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    X_OFFSET,
    X_SPACING,
    Y_OFFSET,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section


def _section_lines_in_order(graph: MetroGraph, section: Section) -> list[str]:
    """Lines present in *section*, ordered by line-definition priority.

    A line is "present" if any of the section's stations carry it.  Ports
    and junctions don't define which lines belong to the section's rail
    set on their own, so they're excluded here.
    """
    present: set[str] = set()
    for sid in section.station_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port:
            continue
        present.update(graph.station_lines(sid))
    return [lid for lid in graph.lines if lid in present]


def _station_lines_in_order(graph: MetroGraph, station_id: str) -> list[str]:
    """Lines on a station, in line-definition priority order."""
    lines = set(graph.station_lines(station_id))
    return [lid for lid in graph.lines if lid in lines]


def compute_rail_layout(
    graph: MetroGraph,
    *,
    x_spacing: float = X_SPACING,
    y_spacing: float,
    x_offset: float = X_OFFSET,
    y_offset: float = Y_OFFSET,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
    section_y_gap: float = SECTION_Y_GAP,
) -> None:
    """Lay the whole graph out in rail mode.

    Writes ``x``/``y`` onto every station, ``rail_top_y``/``rail_bottom_y``
    onto multi-line stations, port/junction Ys onto their connecting rail,
    and section bboxes.  Sections stack top-to-bottom in grid-row order
    (then by grid-col, then by section number) so the result is a vertically
    stacked set of parallel-rail panels.
    """
    # Sort sections into a stable top-to-bottom reading order.  When the
    # auto-layout / explicit grid set grid_row, honour it; otherwise fall
    # back to declaration order via the section number.
    sections = [
        s for s in graph.sections.values() if not s.is_implicit or s.station_ids
    ]
    ordered = sorted(
        sections,
        key=lambda s: (
            s.grid_row if s.grid_row >= 0 else 0,
            s.grid_col if s.grid_col >= 0 else 0,
            s.number,
        ),
    )

    # Per-section rail Y maps, keyed by section id then line id.
    rail_y: dict[str, dict[str, float]] = {}

    cursor_top = y_offset
    for section in ordered:
        bottom = _layout_section_rails(
            graph,
            section,
            rail_y,
            x_spacing=x_spacing,
            x_offset=x_offset,
            section_top=cursor_top,
            section_x_padding=section_x_padding,
            section_y_padding=section_y_padding,
            y_spacing=y_spacing,
        )
        cursor_top = bottom + section_y_gap

    # Stash the per-section rail map so the dedicated router can resolve a
    # port's Y to its line's rail (rather than the port's stored average Y),
    # keeping inter-section legs on the right rail per line.
    graph._rail_y = rail_y  # type: ignore[attr-defined]

    _position_ports_and_junctions(graph, rail_y)


def retrofit_section_rails(
    graph: MetroGraph,
    section: Section,
    *,
    x_spacing: float = X_SPACING,
    y_spacing: float,
    section_x_padding: float = SECTION_X_PADDING,
    section_y_padding: float = SECTION_Y_PADDING,
) -> None:
    """Re-lay one already-placed section as parallel rails (per-section mode).

    The normal layout pipeline has already positioned this section's bbox via
    section placement.  This function overwrites the section's *internal*
    geometry (station X/Y, rail spans, used-Ys) and its internal edge routing
    with the rail-mode layout, anchored at the section's existing bbox
    top-left, and resizes the bbox to hug the resulting rails.  Inter-section
    placement, ports, and routing are left to the normal machinery.

    Used by ``compute_layout`` when ``graph.is_rail_section(section.id)`` is
    True but the graph is not in the legacy global rail mode.
    """
    # ``_layout_section_rails`` anchors the box top at
    # ``section_top + SECTION_HEADER_PROTRUSION`` and positions stations from
    # ``x_offset``; feeding it the placement-chosen bbox top-left (offsetting
    # the header protrusion back out) keeps the box where placement put it
    # while the rails fill it.
    box_left = section.bbox_x
    box_top = section.bbox_y
    rail_y = graph._rail_y  # type: ignore[attr-defined]
    _layout_section_rails(
        graph,
        section,
        rail_y,
        x_spacing=x_spacing,
        x_offset=box_left,
        section_top=box_top - SECTION_HEADER_PROTRUSION,
        section_x_padding=section_x_padding,
        section_y_padding=section_y_padding,
        y_spacing=y_spacing,
    )

    # Re-position this section's own ports onto their line rails so the normal
    # router still draws sensible inter-section legs (no-op when the rail
    # section is disconnected, which is the supported case).
    _position_section_ports(graph, section, rail_y.get(section.id, {}))


def _position_section_ports(
    graph: MetroGraph,
    section: Section,
    per_line: dict[str, float],
) -> None:
    """Snap one rail section's boundary ports onto their connecting line rail."""
    for port_id in section.port_ids:
        port = graph.ports.get(port_id)
        st = graph.stations.get(port_id)
        if port is None or st is None:
            continue
        lines = _station_lines_in_order(graph, port_id)
        ys = [per_line[lid] for lid in lines if lid in per_line]
        if ys:
            st.y = sum(ys) / len(ys)
        if port.side is PortSide.LEFT:
            st.x = section.bbox_x
        elif port.side is PortSide.RIGHT:
            st.x = section.bbox_x + section.bbox_w
        else:
            st.x = section.bbox_x + section.bbox_w / 2
        port.x = st.x
        port.y = st.y


def _layout_section_rails(
    graph: MetroGraph,
    section: Section,
    rail_y: dict[str, dict[str, float]],
    *,
    x_spacing: float,
    x_offset: float,
    section_top: float,
    section_x_padding: float,
    section_y_padding: float,
    y_spacing: float,
) -> float:
    """Lay out one section's rails and stations; return its bbox bottom Y."""
    lines = _section_lines_in_order(graph, section)
    n_rails = max(1, len(lines))

    # The section-number badge renders just above the bbox top edge (outside
    # the box), so the box itself hugs content: the top rail sits exactly
    # section_y_padding below the bbox top.  A header band above the box is
    # reserved by advancing section_top by SECTION_HEADER_PROTRUSION before
    # this section's bbox begins.
    box_top = section_top + SECTION_HEADER_PROTRUSION
    rails_top = box_top + section_y_padding
    per_line_y = {lid: rails_top + i * y_spacing for i, lid in enumerate(lines)}
    rail_y[section.id] = per_line_y

    # X by longest-path layer over the *whole* graph restricted to this
    # section's stations, so a station's column reflects its in-section depth.
    layers = _section_layers(graph, section)

    # Place real stations.
    real_ids = [
        sid
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None and not st.is_port
    ]

    # Widen the column step so a column's widest label fits between its
    # neighbours without wrapping.  Labels sit above/below the rails centred
    # on the station X, so a column needs roughly half the widest label of it
    # and of each neighbour to clear; using the per-column widest label as the
    # step keeps the rails evenly spaced while giving long names room.
    x_spacing = _label_aware_x_spacing(graph, real_ids, layers, x_spacing)
    # Off-track input stations sit ABOVE the top rail and feed in with an
    # S-curve (see routing/rail.py), so reserve a band above the rails for
    # them.  rails_top is shifted down by that band; the off-track band Y is
    # computed from the original box-content top.
    off_track_ids = [sid for sid in real_ids if graph.stations[sid].off_track]
    off_track_band = y_spacing if off_track_ids else 0.0
    rails_top += off_track_band
    if off_track_band:
        per_line_y = {lid: rails_top + i * y_spacing for i, lid in enumerate(lines)}
        rail_y[section.id] = per_line_y
    off_track_y = box_top + section_y_padding

    for sid in real_ids:
        st = graph.stations[sid]
        layer = layers.get(sid, 0)
        st.layer = layer
        st.x = x_offset + section_x_padding + layer * x_spacing

        if st.off_track:
            # Park above the rails just to the left of the consumer column, so
            # the router draws a short, clean S-curve down into the rail rather
            # than a long diagonal traverse from layer 0.  The consumer's layer
            # determines the X; the off-track input sits half a column before it.
            consumer_layer = min(
                (
                    layers.get(e.target, layer)
                    for e in graph.edges_from(sid)
                    if e.target in layers
                ),
                default=layer + 1,
            )
            feed_layer = max(0.0, consumer_layer - 0.5)
            st.x = x_offset + section_x_padding + feed_layer * x_spacing
            st.y = off_track_y
            st.track = 0.0
            st.rail_used_ys = []
            st.rail_top_y = None
            st.rail_bottom_y = None
            continue

        st_lines = _station_lines_in_order(graph, sid)
        ys = [per_line_y[lid] for lid in st_lines if lid in per_line_y]
        if not ys:
            # A station with no recognised line (shouldn't happen post-parse);
            # park it on the first rail.
            ys = [rails_top]
        top_y = min(ys)
        bot_y = max(ys)
        st.y = (top_y + bot_y) / 2
        st.track = 0.0
        # A blank terminus (file/dir/report icon with no text label) renders
        # as its icon at the rail convergence, so the lines meet at a single
        # point rather than spanning a pill: no span, no per-rail knobs.
        is_blank_terminus = st.is_terminus and not st.label.strip()
        if is_blank_terminus:
            st.rail_used_ys = [st.y for _ in ys]
            st.rail_top_y = None
            st.rail_bottom_y = None
        else:
            # Record the rails this station actually uses (in line order) so
            # the renderer can draw a knob on each; rails that merely pass
            # behind the pill get no knob.
            st.rail_used_ys = list(ys)
            if len(set(ys)) > 1:
                st.rail_top_y = top_y
                st.rail_bottom_y = bot_y
            else:
                st.rail_top_y = None
                st.rail_bottom_y = None

    # Section bbox: span columns and rails with symmetric padding.  rails_top
    # already includes the off-track band, so the box top stays at box_top and
    # the box height covers padding + band + rails + padding.
    max_layer = max((layers.get(sid, 0) for sid in real_ids), default=0)
    bbox_x = x_offset
    bbox_w = section_x_padding * 2 + max_layer * x_spacing
    rails_bottom = rails_top + (n_rails - 1) * y_spacing
    bbox_y = box_top
    bbox_h = (rails_bottom - box_top) + section_y_padding
    section.bbox_x = bbox_x
    section.bbox_y = bbox_y
    section.bbox_w = bbox_w
    section.bbox_h = bbox_h
    section.direction = "LR"

    return bbox_y + bbox_h


def _label_aware_x_spacing(
    graph: MetroGraph,
    real_ids: list[str],
    layers: dict[str, int],
    x_spacing: float,
) -> float:
    """Return a column step wide enough that no column's label wraps.

    Labels render centred on a station's X, so two adjacent columns' labels
    each consume half their own width either side of the column line.  Taking
    the widest label across the section and requiring the step to seat half of
    it plus half its neighbour (i.e. the full widest label, plus a small gap)
    keeps every label on one line while the rails stay evenly spaced.
    """
    from nf_metro.layout.constants import LABEL_MARGIN
    from nf_metro.layout.labels import label_text_width

    widest = 0.0
    for sid in real_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.off_track:
            continue
        # Blank termini render as icons, not text labels, so don't size to them.
        if st.is_terminus and not st.label.strip():
            continue
        widest = max(widest, label_text_width(st.label))
    if widest <= 0.0:
        return x_spacing
    return max(x_spacing, widest + LABEL_MARGIN * 2)


def _section_layers(graph: MetroGraph, section: Section) -> dict[str, int]:
    """Longest-path layers for a single section's internal real stations."""
    import networkx as nx

    station_ids = {
        sid
        for sid in section.station_ids
        if (st := graph.stations.get(sid)) is not None and not st.is_port
    }
    sub: nx.DiGraph[str] = nx.DiGraph()
    for sid in station_ids:
        sub.add_node(sid)
    for edge in graph.edges:
        if edge.source in station_ids and edge.target in station_ids:
            sub.add_edge(edge.source, edge.target)
    try:
        topo = list(nx.topological_sort(sub))
    except nx.NetworkXUnfeasible:
        return dict.fromkeys(station_ids, 0)
    layers: dict[str, int] = {}
    for node in topo:
        preds = list(sub.predecessors(node))
        layers[node] = max((layers[p] for p in preds), default=-1) + 1 if preds else 0
    return layers


def _position_ports_and_junctions(
    graph: MetroGraph,
    rail_y: dict[str, dict[str, float]],
) -> None:
    """Place ports and junctions at the rail Y of the line(s) they carry.

    Ports sit on their section's boundary edge (X from the bbox) at the Y of
    their connecting line's rail.  Junctions sit in the inter-section gap at
    the Y of one of their lines.  This keeps the dedicated rail router's
    inter-section legs horizontal where possible.
    """
    for port_id, port in graph.ports.items():
        st = graph.stations.get(port_id)
        if st is None:
            continue
        section = graph.sections.get(port.section_id)
        per_line = rail_y.get(port.section_id, {})
        lines = _station_lines_in_order(graph, port_id)
        ys = [per_line[lid] for lid in lines if lid in per_line]
        st.y = sum(ys) / len(ys) if ys else (section.bbox_y if section else 0.0)
        if section is not None:
            if port.side in (PortSide.LEFT,):
                st.x = section.bbox_x
            elif port.side in (PortSide.RIGHT,):
                st.x = section.bbox_x + section.bbox_w
            else:
                st.x = section.bbox_x + section.bbox_w / 2
        port.x = st.x
        port.y = st.y

    for jid in graph.junctions:
        st = graph.stations.get(jid)
        if st is None:
            continue
        # Place the junction midway between its predecessors and successors
        # in X, at the average rail Y of the lines passing through it.
        neighbours = [graph.stations.get(e.source) for e in graph.edges_to(jid)] + [
            graph.stations.get(e.target) for e in graph.edges_from(jid)
        ]
        xs = [n.x for n in neighbours if n is not None]
        st.x = sum(xs) / len(xs) if xs else 0.0
        # Junction Y: average of its lines' rails across whichever section
        # rail map contains them (use the target section's map if present).
        line_ids = _station_lines_in_order(graph, jid)
        jys: list[float] = []
        for per_line in rail_y.values():
            jys.extend(per_line[lid] for lid in line_ids if lid in per_line)
        st.y = sum(jys) / len(jys) if jys else 0.0
