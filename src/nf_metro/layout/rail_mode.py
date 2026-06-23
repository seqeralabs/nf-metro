"""Opt-in "rail mode" layout (the nf-core/sarek subway idiom).

In normal layout a station shared by several metro lines is a single point
and the lines converge (bundle) to that one Y.  Rail mode instead lays each
line out as a fixed, evenly-spaced horizontal *rail* across the section and
renders a station several lines *pass through* as the classic metro
*interchange*: a circle on each rail the station uses, joined by a straight
connector segment.  Co-travelling lines stay on their own rails rather than
bundling to one Y; they converge only at a genuine fan-in/out to a single
node (e.g. a file terminus all lines reach), eased in with 45-degree
diagonals.

This module is a self-contained pipeline, run by ``compute_layout`` only for
sections whose ``line_spread`` resolves to ``rails`` (``graph.is_rail_section``),
so the normal layout path is untouched for every other section.

Scope (MVP): LR sections.  Each section's lines get rails centred about the
section trunk Y; stations are placed one per column in declaration-priority
topological order (X) and anchored to span their lines' rail range (Y).
Sections are stacked vertically in grid-row
order.  Ports/junctions are positioned at their connecting rail Y so that the
dedicated rail-mode router (see ``routing/rail.py``) can draw straight rails.
"""

from __future__ import annotations

__all__ = ["compute_rail_layout"]

from nf_metro.layout.constants import (
    DIAGONAL_RUN,
    INTER_ROW_EDGE_CLEARANCE,
    INTER_ROW_HEADER_CLEARANCE,
    MIN_STRAIGHT_EDGE,
    OFFSET_STEP,
    OFFTRACK_TERMINUS_NUB_CLEARANCE,
    RAIL_ABOVE_LABEL_TOP_PAD,
    SECTION_HEADER_PROTRUSION,
    SECTION_X_PADDING,
    SECTION_Y_GAP,
    SECTION_Y_PADDING,
    X_OFFSET,
    X_SPACING,
    Y_OFFSET,
)
from nf_metro.parser.model import MetroGraph, PortSide, Section

# Horizontal room a blank terminus's fan-out/fan-in needs to ease between the
# convergence point and the rails without compressing: a flat lead plus the
# 45-degree diagonal plus a straight stub each side.  Reserved as the gap
# between a fanning terminus and its neighbour column so the S-curves keep
# their shape no matter how tight the shared (diagonal-label) column pitch is.
_TERMINUS_FAN_ROOM = DIAGONAL_RUN + 3.0 * MIN_STRAIGHT_EDGE


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
    for idx, section in enumerate(ordered):
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
        # A multi-line inter-section connector wraps its cross leg through the
        # gap below this section; widen the gap so the whole bundle clears both
        # box edges (the default rail gap is tighter than the connector needs).
        gap = section_y_gap
        if idx + 1 < len(ordered):
            gap = max(gap, _connector_gap(graph, rail_y, section, ordered[idx + 1]))
        cursor_top = bottom + gap

    # Stash the per-section rail map so the dedicated router can resolve a
    # port's Y to its line's rail (rather than the port's stored average Y),
    # keeping inter-section legs on the right rail per line.
    graph._rail_y = rail_y

    _position_ports_and_junctions(graph, rail_y)


def _connector_gap(
    graph: MetroGraph,
    rail_y: dict[str, dict[str, float]],
    upper: Section,
    lower: Section,
) -> float:
    """Section gap below *upper* needed to seat a multi-line connector bundle.

    A whole-graph rail connector from *upper* to *lower* wraps its cross leg
    through the gap between them, spread vertically over the connector's rails.
    The band must fit that spread between ``INTER_ROW_EDGE_CLEARANCE`` below the
    upper box and ``INTER_ROW_HEADER_CLEARANCE`` above the lower box's header
    badge.  Returns the gap that satisfies this, or ``0`` when fewer than two
    lines connect the two sections (a single track needs no extra room).
    """
    per_line = rail_y.get(upper.id, {})
    ys: list[float] = []
    for e in graph.edges:
        sp = graph.ports.get(e.source)
        tp = graph.ports.get(e.target)
        if sp is None or tp is None:
            continue
        if sp.section_id != upper.id or tp.section_id != lower.id:
            continue
        y = per_line.get(e.line_id)
        if y is not None:
            ys.append(y)
    if len(ys) < 2:
        return 0.0
    spread = max(ys) - min(ys)
    band = INTER_ROW_EDGE_CLEARANCE + spread + INTER_ROW_HEADER_CLEARANCE
    return band - SECTION_HEADER_PROTRUSION


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

    Used by ``compute_layout`` for a section that resolves to ``rails`` while
    the graph-wide default is some other mode (the per-section override case).
    """
    # ``_layout_section_rails`` anchors the box top at
    # ``section_top + SECTION_HEADER_PROTRUSION`` and positions stations from
    # ``x_offset``; feeding it the placement-chosen bbox top-left (offsetting
    # the header protrusion back out) keeps the box where placement put it
    # while the rails fill it.
    box_left = section.bbox_x
    box_top = section.bbox_y
    rail_y = graph._rail_y
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
        lines = graph.station_lines_ordered(port_id)
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
    # Lines sharing a legend_combo collapse onto a single rail slot (a tight
    # bundle), so the slot count - not the line count - drives rail spacing and
    # the bbox height.  With no combos there is one slot per line.
    slot_offset, _n_slots = _rail_slot_offsets(graph, lines, y_spacing)

    # The section-number badge renders just above the bbox top edge (outside
    # the box), so the box itself hugs content: the top rail sits exactly
    # section_y_padding below the bbox top.  A header band above the box is
    # reserved by advancing section_top by SECTION_HEADER_PROTRUSION before
    # this section's bbox begins.
    box_top = section_top + SECTION_HEADER_PROTRUSION
    rails_top = box_top + section_y_padding
    per_line_y = {lid: rails_top + slot_offset[lid] for lid in lines}
    rail_y[section.id] = per_line_y

    # Longest-path layer over this section's internal stations, used below for
    # head/tail terminus depth checks (column X is assigned separately).
    import networkx as nx

    from nf_metro.layout.layers import assign_layers, build_station_digraph
    from nf_metro.layout.phases._common import _build_section_subgraph

    section_dag = _build_section_subgraph(graph, section)
    layers = assign_layers(section_dag)

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
        per_line_y = {lid: rails_top + slot_offset[lid] for lid in lines}
        rail_y[section.id] = per_line_y
    off_track_y = box_top + section_y_padding

    # Top-rail station labels hang ABOVE the top rail (see labels._rail_label_side),
    # so reserve a band for them between the box top and the top rail.  Pushing
    # the rails down by the band keeps the labels inside the box -- the box top
    # (and thus the gap to the section above) stays put, instead of place_labels
    # growing the box upward at render time and overlapping the section above.
    above_ids = _rail_above_label_stations(graph, real_ids, per_line_y)
    above_band = _rail_label_band(graph, above_ids)
    # The angled band only reaches the box top with a thin label corner, so the
    # box hugs it with RAIL_ABOVE_LABEL_TOP_PAD rather than the full content
    # padding.  Off-track inputs anchor their own band to box_top +
    # section_y_padding, so leave the larger pad when they are present.
    if above_band and not off_track_ids:
        rails_top -= max(0.0, section_y_padding - RAIL_ABOVE_LABEL_TOP_PAD)
    rails_top += above_band
    if above_band:
        per_line_y = {lid: rails_top + slot_offset[lid] for lid in lines}
        rail_y[section.id] = per_line_y

    # Rails place one distinct station per column.  Order the columns by a
    # topological sort that breaks ties on declaration order, so the columns
    # follow the order stations are authored in (the intended reading) while
    # always respecting edge direction; each on-rail station gets its own column
    # and no two distinct stations share an X.
    decl_index = {sid: i for i, sid in enumerate(section.station_ids)}
    real_set = set(real_ids)
    dag = build_station_digraph(section_dag)
    topo = nx.lexicographical_topological_sort(dag, key=lambda n: decl_index.get(n, 0))
    on_rail_ids = [
        sid for sid in topo if sid in real_set and not graph.stations[sid].off_track
    ]
    seen = set(on_rail_ids)
    on_rail_ids += [
        sid for sid in real_ids if sid not in seen and not graph.stations[sid].off_track
    ]
    cols: dict[str, int] = {sid: i for i, sid in enumerate(on_rail_ids)}
    max_col = len(on_rail_ids) - 1 if on_rail_ids else 0

    # A blank terminus that fans across rails needs its own horizontal room so
    # the fan S-curves keep their shape regardless of the shared column pitch.
    # Reserve it as extra gap AFTER the first column (a head/source terminus)
    # and BEFORE the last column (a tail/sink terminus); the gap is the larger
    # of the column pitch and the fan room, so it never shrinks the fan.
    max_layer = max((layers.get(sid, 0) for sid in real_ids), default=0)

    def _fans(sid: str) -> bool:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.off_track:
            return False
        if not (st.is_blank_terminus):
            return False
        lns = graph.station_lines_ordered(sid)
        return len({per_line_y[lid] for lid in lns if lid in per_line_y}) > 1

    head_fan = any(_fans(sid) for sid in real_ids if layers.get(sid, 0) == 0)
    tail_fan = any(_fans(sid) for sid in real_ids if layers.get(sid, 0) == max_layer)
    head_extra = max(0.0, _TERMINUS_FAN_ROOM - x_spacing) if head_fan else 0.0
    tail_extra = max(0.0, _TERMINUS_FAN_ROOM - x_spacing) if tail_fan else 0.0

    # A sink terminus marches its file icons rightward from the last column.
    # The normal-section layout reserves room for them via
    # _adjust_terminus_icon_clearance; the rail pipeline otherwise leaves only
    # the standard padding, so a multi-icon terminus would clamp its icons on
    # top of one another at the section's right edge.  Reserve the icon
    # clearance beyond the standard padding (right side only, without shifting
    # the terminus itself).
    from nf_metro.layout.phases.single_section import _terminus_icon_clearance

    tail_icon_extra = 0.0
    for sid in real_ids:
        st = graph.stations.get(sid)
        if (
            st is None
            or st.is_port
            or st.off_track
            or len(st.terminus_labels) <= 1
            or layers.get(sid, 0) != max_layer
            or graph.edges_from(sid)  # sink only: its icons extend rightward
        ):
            continue
        need = _terminus_icon_clearance(
            len(st.terminus_labels), st.terminus_names or None
        )
        tail_icon_extra = max(tail_icon_extra, need - section_x_padding)
    tail_icon_extra = max(0.0, tail_icon_extra)

    def _col_x(col: float) -> float:
        x = x_offset + section_x_padding + col * x_spacing
        if col >= 1:
            x += head_extra
        if col == max_col:
            x += tail_extra
        return x

    for sid in real_ids:
        st = graph.stations[sid]

        if st.off_track:
            # Park above the rails just to the left of the consumer column, so
            # the router draws a short, clean S-curve down into the rail rather
            # than a long diagonal traverse from the section head.  The consumer's
            # column determines the X; the off-track input sits half a column
            # before it.
            consumer_col = min(
                (cols[e.target] for e in graph.edges_from(sid) if e.target in cols),
                default=max_col + 1,
            )
            feed_col = max(0.0, consumer_col - 0.5)
            st.layer = consumer_col
            st.x = _col_x(feed_col)
            st.y = off_track_y + (
                OFFTRACK_TERMINUS_NUB_CLEARANCE if st.is_captioned_terminus else 0.0
            )
            st.track = 0.0
            st.rail_used_ys = []
            st.rail_top_y = None
            st.rail_bottom_y = None
            continue

        col = cols[sid]
        st.layer = col
        st.x = _col_x(col)

        st_lines = graph.station_lines_ordered(sid)
        ys = [per_line_y[lid] for lid in st_lines if lid in per_line_y]
        if not ys:
            # A station with no recognised line (shouldn't happen post-parse);
            # park it on the first rail.
            ys = [rails_top]
        top_y = min(ys)
        bot_y = max(ys)
        st.y = (top_y + bot_y) / 2
        st.track = 0.0
        # A blank terminus (file/dir/report icon) converges its lines to a tight
        # BUNDLE -- parallel lines a small offset apart, not all merged onto one
        # Y -- so the fan to the rails leaves/enters a bundle (several distinct
        # line widths) rather than a single line.  The slots are ordered by each
        # line's rail Y so the fan never crosses; the terminus bar caps the
        # bundle.  A single-line terminus stays a point.
        is_blank_terminus = st.is_blank_terminus
        if is_blank_terminus:
            n = len(ys)
            if n > 1:
                rank = {
                    i: r for r, i in enumerate(sorted(range(n), key=lambda i: ys[i]))
                }
                slots = [
                    st.y + (rank[i] - (n - 1) / 2.0) * OFFSET_STEP for i in range(n)
                ]
                st.rail_used_ys = slots
                st.rail_top_y = min(slots)
                st.rail_bottom_y = max(slots)
            else:
                st.rail_used_ys = [st.y]
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
    # the box height covers padding + band + rails + padding.  The terminus fan
    # room widens the column span, so include it.
    bbox_x = x_offset
    bbox_w = (
        section_x_padding * 2
        + max_col * x_spacing
        + head_extra
        + tail_extra
        + tail_icon_extra
    )
    # The lowest rail Y is the largest per-line offset below rails_top (which
    # is a bundle sub-rail when the bottom slot is a combo), not simply the
    # last slot centre.
    rails_bottom = rails_top + (max(slot_offset.values()) if slot_offset else 0.0)
    bbox_y = box_top
    # Below-rail station labels hang under the bottom rail; the bbox must
    # contain them so a section stacked below this one clears them (the
    # stacking gap is measured bbox-to-bbox).  Reserve max(section_y_padding,
    # below-rail label band) so the box always wraps its labels.  With the
    # default 50px padding this is a no-op for short/level labels, but it grows
    # the box for a long or steeply-angled label whose footprint exceeds the
    # padding.
    below_ids = [sid for sid in real_ids if sid not in above_ids]
    bottom_pad = max(section_y_padding, _rail_label_band(graph, below_ids))
    bbox_h = (rails_bottom - box_top) + bottom_pad
    section.bbox_x = bbox_x
    section.bbox_y = bbox_y
    section.bbox_w = bbox_w
    section.bbox_h = bbox_h
    section.direction = "LR"

    return bbox_y + bbox_h


def _rail_label_band(
    graph: MetroGraph,
    ids: list[str] | set[str],
) -> float:
    """Vertical room a set of rail-station labels need clear of their rail.

    A rail label is offset ``LABEL_OFFSET`` from the rail then occupies its own
    text footprint.  For an angled (diagonal) label the footprint is
    ``height*cos(angle) + width*sin(angle)``.  Returns the worst such band over
    the given stations (0 if none), so the caller can reserve enough room above
    the top rail (for above-labels) or below the bottom rail (for below-labels)
    that the section bbox contains every label.
    """
    import math

    from nf_metro.layout.constants import LABEL_OFFSET
    from nf_metro.layout.labels import _label_text_height, label_text_width

    angle = abs(graph.label_angle or 0.0)
    cos_a = abs(math.cos(math.radians(angle)))
    sin_a = abs(math.sin(math.radians(angle)))

    band = 0.0
    for sid in ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.off_track:
            continue
        if st.is_blank_terminus:
            continue
        h = _label_text_height(st.label)
        w = label_text_width(st.label)
        footprint = h * cos_a + w * sin_a if angle else h
        band = max(band, LABEL_OFFSET + footprint)
    return band


def _rail_above_label_stations(
    graph: MetroGraph,
    real_ids: list[str],
    per_line_y: dict[str, float],
) -> set[str]:
    """Stations whose labels hang above the top rail (so the box reserves room).

    Mirrors ``labels._rail_label_side``: only a single-rail station sitting on
    the topmost rail labels above; every other single-rail station labels below
    and a spanning (multi-rail) station keeps layer alternation, so both are
    excluded.  Uses each station's line rail Y (station ``y`` is not yet
    assigned when this runs).
    """
    from nf_metro.layout.labels import _rail_above_threshold

    def _labelled(sid: str) -> bool:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.off_track or st.is_blank_terminus:
            return False
        return bool(st.label.strip())

    threshold = _rail_above_threshold(per_line_y)
    if threshold is None:
        return set()
    above: set[str] = set()
    for sid in real_ids:
        if not _labelled(sid):
            continue
        station_ys = {
            per_line_y[lid]
            for lid in graph.station_lines_ordered(sid)
            if lid in per_line_y
        }
        if len(station_ys) == 1 and next(iter(station_ys)) < threshold:
            above.add(sid)
    return above


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

    # Diagonal labels are all drawn at the same angle, so adjacent columns'
    # labels are parallel and collide only by their perpendicular separation,
    # not along their length.  They therefore share the one graph-wide pitch
    # compute_layout already resolved (labels.diagonal_label_pitch, passed in as
    # x_spacing), so every section -- rail and normal -- packs to the same tight
    # column step instead of each rail widening to seat its label's full width.
    if graph.label_angle:
        return x_spacing

    # Horizontal labels render centred on the station X, so the step must seat
    # the full widest label (plus a small gap) to keep every label on one line.
    widest = 0.0
    for sid in real_ids:
        st = graph.stations.get(sid)
        if st is None or st.is_port or st.off_track:
            continue
        # Blank termini render as icons, not text labels, so don't size to them.
        if st.is_blank_terminus:
            continue
        widest = max(widest, label_text_width(st.label))
    if widest <= 0.0:
        return x_spacing
    return max(x_spacing, widest + LABEL_MARGIN * 2)


def _rail_slot_offsets(
    graph: MetroGraph,
    lines: list[str],
    y_spacing: float,
) -> tuple[dict[str, float], int]:
    """Map each line to a Y offset (from the top rail) and return the slot count.

    Each line normally occupies its own evenly-spaced rail slot.  Lines that
    are members of the same ``legend_combo`` instead share a SINGLE slot,
    drawn as a tight adjacent bundle: the slot's lines hug each other with a
    small sub-offset about the slot centre rather than spreading across full
    rail pitches.  With no combos this is exactly ``i * y_spacing`` per line in
    order, identical to the un-bundled layout.

    Returns ``(per_line_offset, n_slots)`` where ``n_slots`` is the number of
    distinct rail slots (non-combo lines + one per combo with members present).
    """
    # Build line -> combo-key, keeping only combos whose members appear here.
    line_combo: dict[str, int] = {}
    for ci, (combo_ids, _label) in enumerate(graph.legend_combos):
        members = [lid for lid in combo_ids if lid in lines]
        if len(members) >= 2:
            for lid in members:
                line_combo[lid] = ci

    # Walk the lines in order, allocating one slot per non-combo line and one
    # slot per combo (at the position of its first-encountered member).
    slot_of_combo: dict[int, int] = {}
    slot_index: dict[str, int] = {}
    n_slots = 0
    for lid in lines:
        combo_key = line_combo.get(lid)
        if combo_key is None:
            slot_index[lid] = n_slots
            n_slots += 1
        elif combo_key in slot_of_combo:
            slot_index[lid] = slot_of_combo[combo_key]
        else:
            slot_of_combo[combo_key] = n_slots
            slot_index[lid] = n_slots
            n_slots += 1

    # A combo's members hug within their shared slot: spread them symmetrically
    # about the slot centre, one OFFSET_STEP apart (the same pitch the normal
    # router uses for parallel lines in a bundle), so the sub-lines abut and the
    # bundle reads as a single track rather than separate rails.
    from nf_metro.layout.constants import OFFSET_STEP

    bundle_gap = OFFSET_STEP
    members_in_slot: dict[int, list[str]] = {}
    for lid in lines:
        members_in_slot.setdefault(slot_index[lid], []).append(lid)

    per_line_offset: dict[str, float] = {}
    for lid in lines:
        slot = slot_index[lid]
        members = members_in_slot[slot]
        base = slot * y_spacing
        if len(members) == 1:
            per_line_offset[lid] = base
        else:
            k = members.index(lid)
            per_line_offset[lid] = base + (k - (len(members) - 1) / 2.0) * bundle_gap
    return per_line_offset, max(1, n_slots)


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
        lines = graph.station_lines_ordered(port_id)
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
        line_ids = graph.station_lines_ordered(jid)
        jys: list[float] = []
        for per_line in rail_y.values():
            jys.extend(per_line[lid] for lid in line_ids if lid in per_line)
        st.y = sum(jys) / len(jys) if jys else 0.0
