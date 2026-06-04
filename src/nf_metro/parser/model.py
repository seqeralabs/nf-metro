"""Data model for metro map graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict


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


VALID_LINE_STYLES = ("solid", "dashed", "dotted")

ICON_TYPE_FILE = "file"
ICON_TYPE_FILES = "files"
ICON_TYPE_DIR = "dir"
VALID_ICON_TYPES = (ICON_TYPE_FILE, ICON_TYPE_FILES, ICON_TYPE_DIR)


@dataclass
class MetroLine:
    """A metro line (colored route through the graph)."""

    id: str
    display_name: str
    color: str
    style: str = "solid"


@dataclass
class Station:
    """A node/station in the metro map."""

    id: str
    label: str
    section_id: str | None = None
    is_port: bool = False
    is_hidden: bool = False
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
    # Populated by layout engine
    x: float = 0.0
    y: float = 0.0
    layer: int = 0
    track: float = 0.0
    # Rail-mode span (set by the rail-mode layout when MetroGraph.rail_mode
    # is on).  ``rail_top_y``/``rail_bottom_y`` are the Y of the topmost and
    # bottommost rails this station's lines occupy; when they differ the
    # renderer draws one vertical pill spanning that range.  None means the
    # station was not laid out in rail mode (normal pill rules apply).
    rail_top_y: float | None = None
    rail_bottom_y: float | None = None

    @property
    def is_terminus(self) -> bool:
        """Station has one or more file icons."""
        return len(self.terminus_labels) > 0


@dataclass
class Edge:
    """A directed edge between stations, belonging to a metro line."""

    source: str
    target: str
    line_id: str


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
    style: str = "dark"
    lines: dict[str, MetroLine] = field(default_factory=dict)
    stations: dict[str, Station] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    sections: dict[str, Section] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)
    junctions: list[str] = field(default_factory=list)
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
    # Opt-in "rail mode": LR sections are laid out as fixed parallel
    # horizontal rails (one per line) and a multi-line station renders as a
    # single vertical pill spanning the rails it serves, instead of the
    # lines converging to a point.  Off by default; the normal layout /
    # render path is untouched when False.  See docs and examples/rail_mode.mmd.
    rail_mode: bool = False
    legend_position: str = "bottom"
    legend_min_height: float = 0.0
    # Placement modifiers for the bundled legend+logo block. The corner/edge
    # keyword lives in legend_position; these refine where that block lands.
    legend_anchor: str = "content"  # "content" (section bbox) or "canvas"
    legend_offset: tuple[float, float] | None = None  # nudge applied to anchor
    legend_at: tuple[float, float] | None = None  # absolute top-left override
    logo_path: str = ""
    logo_scale: float = 1.0  # multiplies the logo size within the legend block
    # Section dependency graph (populated by auto_layout)
    section_dag: SectionDAG | None = None
    # Section IDs that had explicit %%metro direction: directives
    _explicit_directions: set[str] = field(default_factory=set)
    # Pending terminus designations: station_id -> list of (label, icon_type)
    _pending_terminus: dict[str, list[tuple[str, str, str]]] = field(
        default_factory=dict
    )
    # Pending off-track marks: station_ids to lift above section top track
    _pending_off_track: list[str] = field(default_factory=list)
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
    # Per-phase coordinate-snapshot enable flag (issue #363).  Set once in
    # compute_layout from the NF_METRO_PHASE_SNAPSHOTS env var; read by the
    # _snap hook after each phase.  Off by default (pure observation).
    _phase_snapshots_enabled: bool = field(default=False, repr=False)

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

    def section_for_station(self, station_id: str) -> str | None:
        """Return the section ID containing a station, or None."""
        station = self.stations.get(station_id)
        if station:
            return station.section_id
        return None
