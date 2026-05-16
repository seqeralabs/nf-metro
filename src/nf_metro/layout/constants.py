"""Layout constants used across layout modules.

Centralizes magic numbers from engine.py, routing.py, labels.py,
section_placement.py, and ordering.py.
"""

# ---------------------------------------------------------------------------
# Font / text metrics
# ---------------------------------------------------------------------------
CHAR_WIDTH: float = 9.0
"""Approximate pixel width of a single character at default font size."""

FONT_HEIGHT: float = 14.0
"""Approximate pixel height of default font."""

LABEL_LINE_HEIGHT: float = 1.2
"""Line-height multiplier for multi-line labels (em units)."""

LABEL_PAD: float = 6.0
"""Padding added to label width when computing section bounds."""

# ---------------------------------------------------------------------------
# Global spacing defaults (used as function parameter defaults)
# ---------------------------------------------------------------------------
X_SPACING: float = 60.0
"""Horizontal spacing between layers."""

Y_SPACING: float = 40.0
"""Vertical spacing between tracks."""

X_OFFSET: float = 80.0
"""Left padding from canvas edge to first layer."""

Y_OFFSET: float = 120.0
"""Top padding from canvas edge to first track."""

ROW_GAP: float = 120.0
"""Vertical gap between fold rows."""

# ---------------------------------------------------------------------------
# Section sizing / padding (engine defaults)
# ---------------------------------------------------------------------------
STATION_RADIUS_APPROX: float = 5.0
"""Approximate station pill radius for layout spacing calculations.

This avoids importing the theme-dependent render value into the layout
layer.  Must stay in sync with Theme.station_radius.
"""

SECTION_GAP: float = 3.0
"""Spacing between stations within a section."""

SECTION_X_PADDING: float = 50.0
"""Horizontal padding around section content."""

SECTION_Y_PADDING: float = 50.0
"""Vertical padding around section content."""

SECTION_X_GAP: float = 50.0
"""Horizontal gap between section columns (engine-level)."""

SECTION_Y_GAP: float = 50.0
"""Vertical gap between section rows (engine-level)."""

# ---------------------------------------------------------------------------
# Section placement defaults
# ---------------------------------------------------------------------------
PLACEMENT_X_GAP: float = 80.0
"""Horizontal gap between section columns in meta-graph placement."""

PLACEMENT_Y_GAP: float = 70.0
"""Vertical gap between section rows in meta-graph placement."""

PORT_MIN_GAP: float = 15.0
"""Minimum spacing between adjacent ports on a section boundary."""

SECTION_HEADER_PROTRUSION: float = 26.0
"""Distance the section header protrudes above bbox_y.

The numbered circle center sits at bbox_y - circle_r - Y_OFFSET
(bbox_y - 11 - 4 = bbox_y - 15), and the circle top is another
11px above that, totaling 26px above bbox_y.
"""

MIN_INTER_SECTION_ROW_GAP: float = 12.0
"""Minimum visual gap between section bottom and the next section's header.

Applied after accounting for SECTION_HEADER_PROTRUSION, so the actual
bbox-to-bbox distance will be MIN_INTER_SECTION_ROW_GAP + protrusion.
"""

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
DIAGONAL_RUN: float = 30.0
"""Length of the diagonal segment in direction changes."""

CURVE_RADIUS: float = 10.0
"""Default corner radius for routed paths."""

MERGE_ROUTE_MARGIN: float = 2 * CURVE_RADIUS
"""Distance between a section bbox edge and any merge branch/trunk
vertical line in the inter-section gap."""

MERGE_LINE_GAP: float = CURVE_RADIUS
"""Minimum gap between a merge branch descent and trunk ascent."""

MERGE_GAP_MIN: float = 2 * MERGE_ROUTE_MARGIN + MERGE_LINE_GAP
"""Minimum inter-section gap for column pairs that have merge routing.

Only applied to gaps where merge branches and trunks coexist."""

MIN_INTER_SECTION_GAP: float = 4 * CURVE_RADIUS
"""Minimum physical gap between adjacent section bboxes.

Ensures the gap midpoint is at least 2*CURVE_RADIUS from each section
edge, giving enough horizontal run for smooth curves at bypass route
corners.  Derived as 4 * CURVE_RADIUS."""

OFFSET_STEP: float = 3.0
"""Per-line offset increment for parallel lines in bundles."""

COORD_TOLERANCE: float = 1.0
"""Tolerance for coordinate comparison (same X or same Y)."""

COORD_TOLERANCE_FINE: float = 0.01
"""Fine tolerance for detecting nearly identical Y coordinates."""

CROSS_ROW_THRESHOLD: float = 80.0
"""Y gap threshold for detecting cross-row (fold) edges."""

FOLD_MARGIN: float = 30.0
"""Offset from fold edge for cross-row routing."""

MIN_STRAIGHT_INTER: float = 15.0
"""Minimum straight track length for inter-section routing."""

MIN_STRAIGHT_PORT: float = 5.0
"""Curve radius offset for port-adjacent edges."""

MIN_STRAIGHT_EDGE: float = 10.0
"""Minimum straight track for non-port edges."""

MIN_STATION_FLAT_LENGTH: float = 20.0
"""Minimum length of the visible horizontal flat segment THROUGH a station.

A station sitting on the polyline corner where two paths meet would
otherwise have its flat fully consumed by the curve corner (CURVE_RADIUS
pixels each side).  This constant ensures the flat segment around a
visible station, measured as the polyline run reaching the station X,
exceeds the curve radius by a meaningful amount so a visible flat is
drawn through the station (matching how regular fork/join stations
present a clear horizontal segment through their X coordinate)."""

BYPASS_CLEARANCE: float = 25.0
"""Vertical clearance below the lowest intervening section for bypass routes."""

BYPASS_NEST_STEP: float = 8.0
"""Per-line vertical offset for stacking multiple bypass routes."""

HEADER_CLEARANCE: float = 30.0
"""Clearance above/below section headers for inter-row routing channels.

Section headers (numbered circle + label) are rendered above bbox_y by
approximately SECTION_HEADER_PROTRUSION (~26px).  This constant adds a
small margin so routing channels don't overlap the header zone."""

# ---------------------------------------------------------------------------
# Engine: entry/exit alignment
# ---------------------------------------------------------------------------
TB_LINE_Y_OFFSET: float = 3.0
"""Per-line Y offset increment in TB sections."""

ENTRY_SHIFT_TB: float = 1.0
"""Entry shift multiplier for TB sections with perpendicular entry."""

ENTRY_SHIFT_TB_CROSS: float = 1.0
"""Entry shift multiplier for TB sections with cross-column TOP entry."""

ENTRY_INSET_LR: float = 0.3
"""Entry inset multiplier for LR/RL sections with perpendicular entry."""

ENTRY_SHIFT_LR: float = 0.5
"""Station shift multiplier for LR/RL sections with perpendicular entry.

Applied after port positioning (Phase 9+) so that internal stations move
inward while ports stay put, creating a gap between the perpendicular
entry port and the first internal station.  Mirrors ENTRY_SHIFT_TB."""

EXIT_GAP_MULTIPLIER: float = 0.6
"""Exit gap multiplier for flow-side exits."""

JUNCTION_MARGIN: float = 10.0
"""Margin for positioning junctions in inter-section gaps."""

MIN_PORT_STATION_GAP: float = 16.0
"""Minimum gap between entry port and internal stations (TB perpendicular)."""

STATION_ELBOW_TOLERANCE: float = 12.0
"""Tolerance for station-as-elbow detection."""

MAX_PORT_ALIGN_BBOX_EXPANSION_FRAC: float = 0.5
"""Maximum bbox expansion for port alignment, as a fraction of section bbox_h.

When aligning an entry port Y with the source exit port Y, the source Y
may fall outside the entry section's bounding box (because the source
section has more tracks).  Allow the bbox to expand up to this fraction
of its height to accommodate the aligned port."""

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
LABEL_MARGIN: float = 2.0
"""Overlap detection margin for labels."""

LABEL_OFFSET: float = 11.0
"""Vertical distance from pill edge to label."""

DESCENDER_CLEARANCE: float = 3.0
"""Extra upward shift for above labels so descenders (g, p, y) clear the pill.

SVG ``dominant-baseline: auto`` places the alphabetic baseline at the
label's Y coordinate; descenders extend below.  This constant accounts
for that so the visual gap matches ``LABEL_OFFSET``."""

TB_PILL_EDGE_OFFSET: float = 5.0
"""Pill edge offset for TB vertical station labels."""

TB_LABEL_H_SPACING: float = 6.0
"""Horizontal spacing for TB vertical station labels."""

COLLISION_MULTIPLIER: float = 2.2
"""Label offset multiplier when resolving collisions."""

LABEL_NUDGE_MAX: float = 20.0
"""Maximum horizontal shift (px) to resolve a label collision before flipping."""

LABEL_BBOX_MARGIN: float = 4.0
"""Margin for clamping labels within section bounding box."""

# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
LINE_GAP: float = 1.0
"""Fixed gap between line base tracks."""

DIAMOND_COMPRESSION: float = 0.25
"""Compression factor toward trunk for diamond (fork-join) paths."""

SIDE_BRANCH_NUDGE: float = 1.0
"""Nudge amount for side-branch tracking."""

FANOUT_SPACING: float = 1.5
"""Spacing multiplier for fan-out node layout."""

TERMINUS_WIDTH: float = 28.0
"""Default width of terminus (file) icons.

Used by both layout (for clearance calculations) and render (as the
Theme.terminus_width default).
"""

ICON_INTER_GAP: float = 4.0
"""Gap between adjacent file icons when a station has multiple icons."""

TERMINUS_ICON_CLEARANCE: float = 58.0
"""Minimum clearance from terminus station center to section bbox edge.

Accounts for station_radius (~5px) + icon gap (6px) + icon width (28px) = 39px
extent, plus ~19px visual margin so icons don't crowd the section border.
"""

PORT_LABEL_MAX_DX: float = 120.0
"""Max horizontal distance for port-route label override.

Only stations within this distance of their connected port get their
label flipped to avoid overlapping the diagonal route to the port.
Stations further away have enough horizontal room for the route to
clear the label without overriding alternation.
"""

DEFAULT_LINE_PRIORITY: int = 999
"""Sentinel priority for lines not in the explicit line order."""

# ---------------------------------------------------------------------------
# Bubble centering
# ---------------------------------------------------------------------------
STATION_MOVE_TOLERANCE: float = 0.5
"""Minimum absolute shift to consider a station as having moved.

Used by bubble-centering post-processing to distinguish moved stations
from untouched ones when checking column-companion consensus."""

# ---------------------------------------------------------------------------
# Phase-boundary guards
# ---------------------------------------------------------------------------
GUARD_TOLERANCE: float = 5.0
"""Tolerance for phase-boundary invariant checks (port-on-boundary, etc.)."""
