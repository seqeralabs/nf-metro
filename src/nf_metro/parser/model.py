"""Data model for metro map graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, TypedDict


class RowGridInfo(TypedDict):
    """Per-row grid metadata recorded by Stage 1.2 (``_align_row_y_grids``)."""

    section_ids: list[str]
    slot_count: int
    slot_spacing: float
    max_y_pad: float


@dataclass
class SectionDAG:
    """Section-level dependency graph built from inter-section edges.

    Built once during auto-layout (before _resolve_sections rewrites edges
    through ports and junctions) and stored on MetroGraph for reuse by
    section_placement and other layout phases.
    """

    successors: dict[str, set[str]]
    predecessors: dict[str, set[str]]
    edge_lines: dict[tuple[str, str], set[str]]

    @property
    def section_edges(self) -> set[tuple[str, str]]:
        """All (src_section, tgt_section) pairs."""
        return set(self.edge_lines.keys())


class PortSide(Enum):
    """Side of a section boundary where a port is located."""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class LineSpread(str, Enum):
    """How lines sharing a station relate to each other vertically.

    A single axis the user controls per graph and per section:

    - ``BUNDLE`` merges shared lines onto one trunk track; detours cascade
      downward from the top line (the default).
    - ``CENTERED`` also merges, but balances the bundle about the midline so
      the shared trunk sits centred and exclusive callers fan above and below.
    - ``RAILS`` does not merge: each line keeps its own parallel rail and a
      shared station renders as an interchange spanning the rails it uses.
    """

    BUNDLE = "bundle"
    CENTERED = "centered"
    RAILS = "rails"


VALID_LINE_STYLES = ("solid", "dashed", "dotted")

MARKER_SHAPE_CIRCLE = "circle"
MARKER_SHAPE_SQUARE = "square"
MARKER_SHAPE_PILL = "pill"
VALID_MARKER_SHAPES = (MARKER_SHAPE_CIRCLE, MARKER_SHAPE_SQUARE, MARKER_SHAPE_PILL)

# Fill keywords; any other value is treated as a literal colour (named or hex).
MARKER_FILL_OPEN = "open"
MARKER_FILL_SOLID = "solid"

ICON_TYPE_FILE = "file"
ICON_TYPE_FILES = "files"
ICON_TYPE_DIR = "dir"
VALID_ICON_TYPES = (
    ICON_TYPE_FILE,
    ICON_TYPE_FILES,
    ICON_TYPE_DIR,
)


@dataclass
class MetroLine:
    """A metro line (colored route through the graph)."""

    id: str
    display_name: str
    color: str
    style: str = "solid"


@dataclass(frozen=True)
class MarkerStyle:
    """Per-station marker shape and fill.

    ``shape`` is one of :data:`VALID_MARKER_SHAPES`: ``circle`` (fully rounded),
    ``square`` (sharp corners), or ``pill`` (a flat-edged capsule elongated
    along the line, used to flag a station whose detail is shown elsewhere).
    ``fill`` is ``open`` (background-coloured interior), ``solid`` (the theme's
    default station fill), or a literal colour (named or hex). When ``fill`` is
    a literal colour the marker is drawn filled with that colour.
    """

    shape: str = MARKER_SHAPE_CIRCLE
    fill: str = MARKER_FILL_SOLID


@dataclass
class MarkerLegendEntry:
    """A caption for a marker shape/fill combination in the marker key."""

    style: MarkerStyle
    caption: str


BYPASS_V_PREFIX = "__bypass_"


def is_bypass_v(station_id: str) -> bool:
    """True if *station_id* names a hidden bypass-V helper station.

    Bypass-V helpers are the routing-only nodes inserted by
    :mod:`nf_metro.parser.resolve` to route a line around a station it does
    not stop at.  Their ids are built with :data:`BYPASS_V_PREFIX`; this is
    the one place the prefix is interpreted.
    """
    return station_id.startswith(BYPASS_V_PREFIX)


@dataclass
class Station:
    """A node/station in the metro map."""

    id: str
    label: str
    section_id: str | None = None
    is_port: bool = False
    is_hidden: bool = False
    # For a hidden bypass-V helper station, the id of the real station whose
    # marker the routed line bypasses.  Lets the router seat the V's flat-run
    # corners clear of that station's name label (the V carries no label of its
    # own).  None for every non-bypass station.
    bypasses_station_id: str | None = None
    # Per-station marker style from %%metro marker:; None = default pill.
    marker: MarkerStyle | None = None
    # When True, the station is lifted above the section's top track in a
    # final layout phase.  Used for file-input nodes that would otherwise
    # consume a line-track Y slot.
    off_track: bool = False
    terminus_labels: list[str] = field(default_factory=list)
    terminus_icon_types: list[str] = field(default_factory=list)
    # Optional human-readable names for each terminus icon, rendered as a
    # caption outside the icon (e.g. "Samples" below a CSV file icon).
    # Parallel list to terminus_labels; empty string means no caption.
    terminus_names: list[str] = field(default_factory=list)
    # Per-icon flag (parallel to terminus_labels): render the format label as
    # bold white text on a dark banner inside the icon (transit-map style).
    # Default False keeps the plain centred label.
    terminus_icon_banners: list[bool] = field(default_factory=list)
    # Populated by layout engine
    x: float = 0.0
    y: float = 0.0
    layer: int = 0
    track: float = 0.0
    # Rail-mode span (set by the rail-mode layout for a section whose
    # ``line_spread`` resolves to ``rails``).  ``rail_top_y``/``rail_bottom_y``
    # are the Y of the topmost and bottommost rails this station's lines
    # occupy; when they differ the renderer draws one vertical pill spanning
    # that range.  None means the station was not laid out in rail mode
    # (normal pill rules apply).
    rail_top_y: float | None = None
    rail_bottom_y: float | None = None
    # Rail Ys this station actually *uses* (one per line it carries), set
    # alongside rail_top_y/rail_bottom_y in rail mode.  A spanning pill draws
    # a knob at each of these Ys; a rail that falls within the pill's span but
    # is absent here belongs to a line the station does not use and passes
    # behind the pill with no knob.  Empty when not laid out in rail mode.
    rail_used_ys: list[float] = field(default_factory=list)
    # Set on the sub-stations an interchange expands into (see
    # :class:`Interchange`): the ``node_id`` of the owning interchange.  The
    # renderer draws the whole interchange as one glyph anchored on its first
    # member and skips the others.  None for ordinary stations.
    interchange_id: str | None = None

    @property
    def is_terminus(self) -> bool:
        """Station has one or more file icons."""
        return len(self.terminus_labels) > 0

    @property
    def is_blank_terminus(self) -> bool:
        """A file-icon station with no text label of its own.

        Rendered as its icon (and an unrounded nub) at the line convergence
        rather than a labelled pill, so layout and routing treat it specially.
        """
        return self.is_terminus and not self.label.strip()

    @property
    def is_captioned_terminus(self) -> bool:
        """A blank file-icon terminus carrying an under-icon caption."""
        return self.is_blank_terminus and any(self.terminus_names)


@dataclass
class Edge:
    """A directed edge between stations, belonging to a metro line."""

    source: str
    target: str
    line_id: str
    source_line: int | None = None


@dataclass
class StationGroup:
    """An annotative caption spanning a set of stations within a section.

    Purely decorative: it groups related stations (e.g. variant-caller
    families) under a shared caption rendered beneath (or above) the
    spanned stations' x-extent.  It does not influence layout coordinates.
    """

    label: str
    station_ids: list[str] = field(default_factory=list)
    position: Literal["above", "below"] = "below"


@dataclass
class Interchange:
    """A cross-track interchange: one logical step several lines pass through
    on their own tracks, drawn as a single connector glyph rather than a point
    the lines converge to.

    Authored as ``%%metro interchange: node | rail-1 lines | rail-2 lines`` (or
    inferred by auto-layout).  In :func:`resolve._expand_interchanges` the named
    node is expanded into one ordinary sub-station per rail (``member_ids``, top
    to bottom), each carrying that rail's lines; its edges are repointed to the
    sub-station whose rail owns the edge's line.  The renderer then draws the
    members as a single glyph (link bar + per-line knobs) under the shared
    ``label``.
    """

    node_id: str
    rails: list[list[str]] = field(default_factory=list)
    label: str = ""
    # Sub-station ids created for each rail, parallel to ``rails`` (filled in by
    # the expansion pass); empty until the node has been expanded.
    member_ids: list[str] = field(default_factory=list)
    # True when auto-layout inferred this interchange rather than the author
    # writing it; carried only for diagnostics (``info``/``explain``).
    inferred: bool = False


@dataclass
class Port:
    """A synthetic entry/exit point on a section boundary.

    Ports are created from inter-section edges and explicit %%metro entry/exit
    directives. They become invisible stations that participate in layout but
    are skipped during rendering.
    """

    id: str
    section_id: str
    side: PortSide
    is_entry: bool = True
    x: float = 0.0
    y: float = 0.0


@dataclass
class Section:
    """A first-class visual grouping of stations (subgraph).

    Used with the new subgraph-based format where sections are explicit
    Mermaid subgraphs with entry/exit port directives.
    """

    id: str
    name: str
    number: int = 0
    station_ids: list[str] = field(default_factory=list)
    internal_edges: list[Edge] = field(default_factory=list)
    entry_ports: list[str] = field(default_factory=list)  # port IDs
    exit_ports: list[str] = field(default_factory=list)  # port IDs

    @property
    def port_ids(self) -> set[str]:
        """Set of all entry- and exit-port IDs on this section."""
        return set(self.entry_ports).union(self.exit_ports)

    # Hints from %%metro entry/exit directives: list of (side, [line_ids])
    exit_hints: list[tuple[PortSide, list[str]]] = field(default_factory=list)
    entry_hints: list[tuple[PortSide, list[str]]] = field(default_factory=list)
    # Internal flow direction ("LR" = left-to-right, "TB" = top-to-bottom)
    direction: str = "LR"
    # Bounding box (set by layout engine)
    bbox_x: float = 0.0
    bbox_y: float = 0.0
    bbox_w: float = 0.0
    bbox_h: float = 0.0
    # Grid position (for section placement)
    grid_col: int = -1  # -1 means auto
    grid_row: int = -1
    grid_row_span: int = 1
    grid_col_span: int = 1
    # Global offset (set by section placement)
    offset_x: float = 0.0
    offset_y: float = 0.0
    # Implicit sections are auto-created for loose stations; no visible box
    is_implicit: bool = False
    # Extra runway, in whole grid columns, that the strike-clearance loop grows
    # on the entry/exit side when a boundary-fan diagonal rakes a station's name
    # label.  Sides are independent so the loop grows only the struck one.  The
    # column pitch is left fixed; the section's flat run lengthens so the
    # transition seats outside the label's x-extent.
    label_strike_entry_cols: int = 0
    label_strike_exit_cols: int = 0
    # Extra columns of intra-section gap the strike-clearance loop inserts
    # before a given layer (keyed by layer index; pushes that layer and every
    # downstream layer along the flow axis).  Lengthens the flat run into a
    # station whose own descent/ascent diagonal would otherwise rake its label.
    label_strike_layer_gaps: dict[int, int] = field(default_factory=dict)


@dataclass
class RouteSegment:
    """A segment of a routed edge path (populated by routing engine)."""

    x1: float
    y1: float
    x2: float
    y2: float
    line_id: str
    edge: Edge | None = None


@dataclass
class MetroGraph:
    """Complete metro map graph definition."""

    title: str = ""
    caption: str = ""
    style: str = "dark"
    lines: dict[str, MetroLine] = field(default_factory=dict)
    stations: dict[str, Station] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    sections: dict[str, Section] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)
    junctions: list[str] = field(default_factory=list)
    groups: list[StationGroup] = field(default_factory=list)
    grid_overrides: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    # Section IDs that received an explicit %%metro grid: directive (i.e.
    # the user laid out the grid manually, as opposed to auto_layout
    # filling in the placement).  Used to gate alignment polish passes
    # that would distort auto-layout pipelines.
    _explicit_grid: set[str] = field(default_factory=set)
    line_order: str = "definition"  # "definition" or "span"
    diamond_style: str = "straight"  # "straight" or "symmetric"
    compact_offsets: bool = False
    center_ports: bool = False
    # None = auto; compute_layout resolves spacing and section gaps.
    x_spacing: float | None = None
    y_spacing: float | None = None
    section_x_gap: float | None = None
    section_y_gap: float | None = None
    # None/False = auto/off; the renderer resolves these.
    width: int | None = None
    height: int | None = None
    animate: bool = False
    directional: bool = False
    embed_manifest: bool = True
    # Max station-columns a section row may reach before the auto-layout wraps
    # it onto the next row. None falls back to 15; raise it to keep a long
    # horizontal trunk of sections on a single row. Overridden by the
    # --fold-threshold CLI flag.
    fold_threshold: int | None = None
    # Vertical relationship between lines sharing a station (see LineSpread).
    # ``line_spread`` is the graph-wide default; ``line_spread_overrides`` maps
    # a section id to a mode that wins over the default for that section.
    # Query via ``section_line_spread`` / ``is_rail_section`` / ``station_is_rail``.
    line_spread: LineSpread = LineSpread.BUNDLE
    line_spread_overrides: dict[str, LineSpread] = field(default_factory=dict)
    # Cross-track interchanges (see :class:`Interchange`), authored via
    # ``%%metro interchange:`` or inferred by auto-layout; expanded into
    # co-column sub-stations in ``resolve._expand_interchanges``.
    interchanges: list["Interchange"] = field(default_factory=list)
    legend_position: str = "bottom"
    legend_min_height: float = 0.0
    # Opt-in diagonal station labels. None means "use the theme
    # default" (0 = horizontal); a directive value overrides the theme.
    label_angle: float | None = None
    # Maps a hidden bypass-V station id to the glyph-ink bbox of the label of
    # the station it bypasses, in rendered (offset-applied) coordinates.
    # Populated by ``compute_layout`` once the layout settles; the router reads
    # it to seat the V's flat-run corners clear of that label.  Empty until a
    # bypass V's diagonal would otherwise rake the bypassed station's name.
    bypass_label_obstacles: dict[str, tuple[float, float, float, float]] = field(
        default_factory=dict
    )
    # %%metro legend_combo entries: (line_ids, label) pairs.
    legend_combos: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    # Placement modifiers for the bundled legend+logo block. The corner/edge
    # keyword lives in legend_position; these refine where that block lands.
    legend_anchor: str = "content"  # "content" (section bbox) or "canvas"
    legend_offset: tuple[float, float] | None = None  # nudge applied to anchor
    legend_at: tuple[float, float] | None = None  # absolute top-left override
    logo_path: str = ""
    logo_scale: float = 1.0  # multiplies the logo size within the legend block
    legend_logo_gap: float | None = None  # px gap between logo and legend entries
    # Multiplies every text size for the render (station labels, title,
    # section labels, legend, terminus/icon captions) and the label-width
    # metrics that drive layout spacing, so render and layout scale together.
    font_scale: float = 1.0
    # Marker-key captions from %%metro marker_legend:. When
    # non-empty, the legend renders a marker key below the line key.
    marker_legend: list[MarkerLegendEntry] = field(default_factory=list)
    # Section dependency graph (populated by auto_layout)
    section_dag: SectionDAG | None = None
    # Section IDs that had explicit %%metro direction: directives
    _explicit_directions: set[str] = field(default_factory=set)
    # Section IDs whose flow direction was flipped at resolve time to keep a
    # flow-axis port on its consumer/producer's end (see resolve.py
    # _reanchor_flow_axis_ports).  Their exit-port offsets anchor on the
    # feeder bundle frame rather than re-centring on zero.
    _fold_reoriented_sections: set[str] = field(default_factory=set)
    # Section IDs whose entry/exit port sides were author-specified via
    # %%metro entry:/exit: directives (tracked separately because auto_layout
    # fills entry_hints/exit_hints for sections that have none, so the hint
    # list alone cannot tell an author side from an inferred one).
    _explicit_entry: set[str] = field(default_factory=set)
    _explicit_exit: set[str] = field(default_factory=set)
    # Section IDs whose perpendicular (TOP/BOTTOM) connection had to be bridged
    # across grid columns because its feeding source sits outside the section's
    # own column.  The run/trunk is held on the section's column (in-bbox) and
    # routing draws a best-effort L-shaped lead-in; the multi-line bundle
    # through such a forced-perpendicular drop may not satisfy the strict
    # render-curve invariants, so their presence relaxes that check to a
    # warning instead of a hard render abort.
    _cross_column_perp_bridges: set[str] = field(default_factory=set)
    # Pending terminus designations: station_id ->
    # list of (label, icon_type, name, banner)
    _pending_terminus: dict[str, list[tuple[str, str, str, bool]]] = field(
        default_factory=dict
    )
    # Pending off-track marks: station_ids to lift above section top track
    _pending_off_track: list[str] = field(default_factory=list)
    # Pending per-station marker styles: station_id -> MarkerStyle, applied
    # after parse so directives may precede or follow the node definition.
    _pending_markers: dict[str, MarkerStyle] = field(default_factory=dict)
    # Pending %%metro process: marks buffered during parse as
    # (station_id, regex) pairs, validated and folded into process_mapping
    # after parse so directives may precede or follow the node definition.
    _pending_process: list[tuple[str, str]] = field(default_factory=list)
    # station_id -> list of regexes matching the Nextflow process names that
    # station represents. Pure metadata: never read by layout or render, used
    # only by the live-progress server and the check-mapping linter.
    process_mapping: dict[str, list[str]] = field(default_factory=dict)
    # Lazy caches keyed off the edge list; invalidated on edge mutation.
    _station_lines_cache: dict[str, list[str]] | None = field(default=None, repr=False)
    _edges_from_cache: dict[str, list[Edge]] | None = field(default=None, repr=False)
    _edges_to_cache: dict[str, list[Edge]] | None = field(default=None, repr=False)
    # Lazy set view of `junctions`, invalidated on junction mutation.
    _junction_ids_cache: set[str] | None = field(default=None, repr=False)
    # Grid alignment metadata (populated by Stage 1.2 _align_row_y_grids)
    _row_y_grid_info: dict[int, RowGridInfo] = field(default_factory=dict, repr=False)
    # Cross-phase channel: station IDs placed at half-pitch offsets
    # relative to the row grid by Stage 6.3
    # (``_apply_half_grid_2branch_symfan``) for 2-branch symfan sections.
    # Stage 6.4 (``_snap_all_y_to_grid``) reads this set and skips those
    # stations so they keep their intentional half-grid Y.
    half_grid_station_ids: set[str] = field(default_factory=set, repr=False)
    # Precondition flag for the off-track reanchor: set True right after the
    # Stage 6.4 grid snap so on-track consumer Ys are final.  The reanchor
    # (``_reanchor_off_track_to_consumer``) refuses to run while False,
    # making its dependence on snapped consumers explicit rather than
    # implicit in call position.
    _consumers_grid_snapped: bool = field(default=False, repr=False)
    # Structural height-below-bbox-top per section, captured before the
    # opportunistic Pass C content-compaction phases run.  The inter-row
    # cascade (``_tighten_lower_rows_after_shrink``) stacks lower rows from
    # this structural extent so row offsets resolve from the anchors-first
    # prediction rather than the post-compaction settled extent.  Empty
    # until the snapshot at the start of ``_place_pass_c_content``.
    _struct_height_below_top: dict[str, float] = field(default_factory=dict, repr=False)
    # Frozen station Ys / section bbox tops captured before a content-balancing
    # phase, read for its slack and arrangement decisions; see
    # _snapshot_placement_refs.
    _placement_ref_y: dict[str, float] = field(default_factory=dict, repr=False)
    _placement_ref_bbox_top: dict[str, float] = field(default_factory=dict, repr=False)
    # Per-phase coordinate-snapshot enable flag.  Set once in
    # compute_layout from the NF_METRO_PHASE_SNAPSHOTS env var; read by the
    # _snap hook after each phase.  Off by default (pure observation).
    _phase_snapshots_enabled: bool = field(default=False, repr=False)
    # Per-section rail-Y map (section_id -> {line_id: rail_y}), set by the
    # rail-mode layout so the dedicated router can resolve a port's per-line
    # rail Y.  Empty when rail mode is off.
    _rail_y: dict[str, dict[str, float]] = field(default_factory=dict, repr=False)
    # Content pitch from compute_min_y_spacing, before the spread loop widens
    # y_spacing for diagonal labels.  A single-trunk section's off-track lift
    # step uses this base pitch so a widened pitch doesn't strand the icon far
    # above the trunk.  None until compute_layout records it.
    _base_y_spacing: float | None = field(default=None, repr=False)

    def _invalidate_edge_caches(self) -> None:
        """Reset caches that depend on the edge list."""
        self._station_lines_cache = None
        self._edges_from_cache = None
        self._edges_to_cache = None

    def add_line(self, line: MetroLine) -> None:
        self.lines[line.id] = line

    def add_station(self, station: Station) -> None:
        self.stations[station.id] = station

    def register_station(self, station: Station) -> None:
        """Add a station and register it with its section if applicable."""
        self.add_station(station)
        if station.section_id and station.section_id in self.sections:
            self.sections[station.section_id].station_ids.append(station.id)

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)
        self._invalidate_edge_caches()

    def replace_edges(self, new_edges: list[Edge]) -> None:
        """Replace the entire edge list and invalidate dependent caches."""
        self.edges = new_edges
        self._invalidate_edge_caches()

    def add_junction(self, junction_id: str) -> None:
        """Append a junction ID and invalidate the junction-set cache."""
        self.junctions.append(junction_id)
        self._junction_ids_cache = None

    @property
    def junction_ids(self) -> set[str]:
        """Set view of ``self.junctions`` for O(1) membership checks."""
        if self._junction_ids_cache is None:
            self._junction_ids_cache = set(self.junctions)
        return self._junction_ids_cache

    def add_section(self, section: Section) -> None:
        self.sections[section.id] = section

    def add_port(self, port: Port) -> None:
        self.ports[port.id] = port
        # Also add as a station so it participates in layout
        self.stations[port.id] = Station(
            id=port.id,
            label="",
            section_id=port.section_id,
            is_port=True,
        )
        # Register with the section
        section = self.sections.get(port.section_id)
        if section:
            section.station_ids.append(port.id)
            if port.is_entry:
                section.entry_ports.append(port.id)
            else:
                section.exit_ports.append(port.id)

    def station_lines(self, station_id: str) -> list[str]:
        """Return sorted line IDs that pass through a station.

        Uses a lazily-built cache that indexes all edges by their
        source and target stations.  The cache is invalidated
        whenever edges are added or replaced.
        """
        if self._station_lines_cache is None:
            from collections import defaultdict

            idx: dict[str, set[str]] = defaultdict(set)
            for e in self.edges:
                idx[e.source].add(e.line_id)
                idx[e.target].add(e.line_id)
            self._station_lines_cache = {sid: sorted(lids) for sid, lids in idx.items()}
        return self._station_lines_cache.get(station_id, [])

    def station_lines_ordered(self, station_id: str) -> list[str]:
        """Line IDs on a station in line-definition priority order.

        ``station_lines`` returns them alphabetically; rail-mode rendering and
        routing need definition order so a multi-line station's knobs and slots
        line up with the legend and the parallel rails.
        """
        lines = set(self.station_lines(station_id))
        return [lid for lid in self.lines if lid in lines]

    def edges_from(self, station_id: str) -> list[Edge]:
        """Return edges whose ``source`` is *station_id* (lazy adjacency cache)."""
        if self._edges_from_cache is None:
            idx: dict[str, list[Edge]] = {}
            for e in self.edges:
                idx.setdefault(e.source, []).append(e)
            self._edges_from_cache = idx
        return self._edges_from_cache.get(station_id, [])

    def edges_to(self, station_id: str) -> list[Edge]:
        """Return edges whose ``target`` is *station_id* (lazy adjacency cache)."""
        if self._edges_to_cache is None:
            idx: dict[str, list[Edge]] = {}
            for e in self.edges:
                idx.setdefault(e.target, []).append(e)
            self._edges_to_cache = idx
        return self._edges_to_cache.get(station_id, [])

    def line_stations(self, line_id: str) -> list[str]:
        """Return station IDs on a line, in edge order."""
        stations = []
        seen = set()
        for edge in self.edges:
            if edge.line_id == line_id:
                if edge.source not in seen:
                    stations.append(edge.source)
                    seen.add(edge.source)
                if edge.target not in seen:
                    stations.append(edge.target)
                    seen.add(edge.target)
        return stations

    def section_line_spread(self, section_id: str | None) -> LineSpread:
        """Resolved line-spread mode for a section (override beats the default)."""
        if section_id is not None and section_id in self.line_spread_overrides:
            return self.line_spread_overrides[section_id]
        return self.line_spread

    @property
    def has_rail_sections(self) -> bool:
        """True if any section is laid out in rail mode (global default or override)."""
        return self.line_spread is LineSpread.RAILS or any(
            mode is LineSpread.RAILS for mode in self.line_spread_overrides.values()
        )

    def is_rail_section(self, section_id: str | None) -> bool:
        """True if *section_id* resolves to rail mode."""
        return self.section_line_spread(section_id) is LineSpread.RAILS

    def station_is_rail(self, station_id: str) -> bool:
        """True if a station belongs to a rail-mode section."""
        st = self.stations.get(station_id)
        section_id = st.section_id if st is not None else None
        return self.is_rail_section(section_id)

    def section_for_station(self, station_id: str) -> str | None:
        """Return the section ID containing a station, or None."""
        station = self.stations.get(station_id)
        if station:
            return station.section_id
        return None

    @property
    def real_sections(self) -> dict[str, Section]:
        """Sections that draw a visible box (excludes implicit holders)."""
        return {sid: sec for sid, sec in self.sections.items() if not sec.is_implicit}
