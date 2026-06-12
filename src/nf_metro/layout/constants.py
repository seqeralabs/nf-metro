"""Layout constants used across layout modules.

Centralizes magic numbers from engine.py, routing.py, labels.py,
section_placement.py, and ordering.py.
"""

# ---------------------------------------------------------------------------
# Font / text metrics
# ---------------------------------------------------------------------------
CHAR_WIDTH: float = 9.0
"""Approximate pixel width of a single character at default font size.

A generous fixed per-character budget used to *reserve* collision room
(:func:`~nf_metro.layout.labels.label_text_width`).  Where the *rendered*
glyph extent matters -- strike detection, fan-run sizing -- use
:data:`GLYPH_ADVANCE_EM` via
:func:`~nf_metro.layout.labels.label_glyph_advance_width` instead, which
tracks the proportional font and so neither over-reserves narrow strings nor
under-measures wide all-caps ones."""

FONT_HEIGHT: float = 14.0
"""Approximate pixel height of default font."""

LABEL_FONT_SIZE: float = 13.0
"""Pixel font size the proportional glyph-advance model measures against.

Matches the theme ``label_font_size`` the renderer draws station names at;
kept here as a theme-agnostic layout proxy (like :data:`CHAR_WIDTH`), with the
``font_scale`` directive applied on top at measurement time."""

GLYPH_ADVANCE_EM: dict[str, float] = {
    " ": 0.278,
    "!": 0.333,
    '"': 0.474,
    "#": 0.556,
    "$": 0.556,
    "%": 0.889,
    "&": 0.722,
    "'": 0.238,
    "(": 0.333,
    ")": 0.333,
    "*": 0.389,
    "+": 0.584,
    ",": 0.278,
    "-": 0.333,
    ".": 0.278,
    "/": 0.278,
    "0": 0.556,
    "1": 0.556,
    "2": 0.556,
    "3": 0.556,
    "4": 0.556,
    "5": 0.556,
    "6": 0.556,
    "7": 0.556,
    "8": 0.556,
    "9": 0.556,
    ":": 0.333,
    ";": 0.333,
    "<": 0.584,
    "=": 0.584,
    ">": 0.584,
    "?": 0.611,
    "@": 0.975,
    "A": 0.722,
    "B": 0.722,
    "C": 0.722,
    "D": 0.722,
    "E": 0.667,
    "F": 0.611,
    "G": 0.778,
    "H": 0.722,
    "I": 0.278,
    "J": 0.556,
    "K": 0.722,
    "L": 0.611,
    "M": 0.833,
    "N": 0.722,
    "O": 0.778,
    "P": 0.667,
    "Q": 0.778,
    "R": 0.722,
    "S": 0.667,
    "T": 0.611,
    "U": 0.722,
    "V": 0.667,
    "W": 0.944,
    "X": 0.667,
    "Y": 0.667,
    "Z": 0.611,
    "[": 0.333,
    "\\": 0.278,
    "]": 0.333,
    "^": 0.584,
    "_": 0.556,
    "`": 0.333,
    "a": 0.556,
    "b": 0.611,
    "c": 0.556,
    "d": 0.611,
    "e": 0.556,
    "f": 0.333,
    "g": 0.611,
    "h": 0.611,
    "i": 0.278,
    "j": 0.278,
    "k": 0.556,
    "l": 0.278,
    "m": 0.889,
    "n": 0.611,
    "o": 0.611,
    "p": 0.611,
    "q": 0.611,
    "r": 0.389,
    "s": 0.556,
    "t": 0.333,
    "u": 0.611,
    "v": 0.556,
    "w": 0.778,
    "x": 0.556,
    "y": 0.556,
    "z": 0.500,
    "{": 0.389,
    "|": 0.280,
    "}": 0.389,
    "~": 0.584,
}
"""Per-character advance width in em units for Helvetica-Bold (Adobe AFM).

The themes draw station names in bold Helvetica/Arial; these advances scaled
by :data:`LABEL_FONT_SIZE` reproduce the rendered glyph extent to within ~1px,
so a wide all-caps name (``MERGE_RUNS``) is measured as wide as it draws and a
narrow one (``lll``) is not over-claimed."""

GLYPH_ADVANCE_DEFAULT_EM: float = 0.6
"""Advance for a character absent from :data:`GLYPH_ADVANCE_EM`."""

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

MIN_Y_SPACING_FLOOR: float = 40.0
"""Floor for auto-computed y_spacing.

When ``compute_layout`` is called without an explicit ``y_spacing`` it
calls ``compute_min_y_spacing`` to widen the grid for content-rich maps
(captioned file icons, dense labels).  The result is clamped to at
least this floor so simple maps don't collapse to an unreadably tight
grid."""

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

Single source of truth for the default station radius: ``Theme.station_radius``
defaults to this value (see ``render.style.Theme``).  Themes may override
the radius, but the layout uses this approximation for spacing math so it
stays decoupled from the theme layer.
"""

SECTION_GAP: float = 3.0
"""Spacing between stations within a section."""

SECTION_X_PADDING: float = 50.0
"""Horizontal padding around section content."""

SECTION_Y_PADDING: float = 50.0
"""Vertical padding around section content."""

RAIL_ABOVE_LABEL_TOP_PAD: float = 20.0
"""Padding between a rail section's box top and its above-rail label band.

The angled above-rail labels already reserve their full tilted footprint as a
band; only a thin label corner reaches the band's top, so the box hugs that
corner with less room than the full SECTION_Y_PADDING used above flat content.
"""

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

MIN_CORRIDOR_Y_OVERLAP: float = 2 * CURVE_RADIUS
"""Minimum vertical overlap for two gap channels to share one corridor.

Channels in the same inter-section gap and direction are grouped into a
concentric corridor only when their vertical spans overlap by more than
this much.  Two channels that overlap by less are not running parallel -
they are stacked segments meeting at a single elbow (a deep descender
landing on a port lane that another channel then leaves), and their
turning corners sit within one corner-radius zone of each other.  Packing
them into one ``OFFSET_STEP`` corridor makes those opposing elbows graze;
treating them as separate corridors lets the gap layout distribute them
across the gap width so the elbows stay clear.  Sized at twice the corner
radius: an overlap that small is entirely inside the two corners' rounding
zones, never a real parallel run."""

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

SAME_COORD_TOLERANCE: float = 0.5
"""Sub-pixel tolerance for treating two coordinates as the same assigned
row / track / value.

Layout phases assign coordinates onto integer-ish grids; this half-pixel
band absorbs float drift so "is this station on that trunk / row / column?"
and threshold-residual checks (``slack <= SAME_COORD_TOLERANCE``) answer
consistently across call sites.  It must stay well below
:data:`OFFSET_STEP` (3.0) so adjacent per-line offset slots are never merged
into one coordinate."""

SAME_Y_TOLERANCE: float = 0.1
"""Tolerance for treating two stations as sharing a base Y row."""

DIAGONAL_SLOPE_RATIO: float = 0.05
"""Slope above which a route segment counts as a diagonal (``|dy| >= |dx| *
this``) rather than a flat trunk run.  A diagonal is what can rake a label."""

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

RAIL_TERMINUS_FAN_LEAD: float = 16.0
"""Flat lead a rail-mode blank terminus's fan runs along its convergence Y
before fanning out to the rails, so the bundle reads as entering/leaving it."""

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

ROW_BAND_SLACK: float = BYPASS_CLEARANCE + Y_SPACING
"""Vertical slack a same-row inter-section route may extend past the row band.

A same-row wrap routes below the row's tallest section through a bypass
channel sitting ``BYPASS_CLEARANCE`` below the band bottom, then stacks the
bundle's per-line nest offsets (a few ``OFFSET_STEP`` each) on top, and adds
up to one ``Y_SPACING`` for the diagonal corner approach.  This slack bounds
that legitimate excursion so the band guard / invariant test admit a clean
below-row wrap while still rejecting a route that dips a full row down."""

SECTION_ROUTE_CLEARANCE: float = 16.0
"""Minimum gap between a section bbox edge and an external route channel.

External routes (wrap channels, around routes, inter-row bypasses) choose
their channel position relative to nearby section bboxes.  Without this
floor the channel may sit one curve_radius + offset_step (~13 px) past
the edge, which reads as flush against the section in renders.  This
floor gives a small but visible breathing space.

Kept as an alias of :data:`EDGE_TO_BUNDLE_CLEARANCE` (the principled
"constant A" of the inter-section gap geometry) so legacy call sites
continue to compile while the new geometry rolls out."""

EDGE_TO_BUNDLE_CLEARANCE: float = 16.0
"""Constant A: minimum distance between a section bbox edge and the
nearest line of an adjacent route bundle.

Used as the single source of truth for two related clearances:

- The leftmost (resp. rightmost) line of a bundle running vertically
  in an inter-section gap sits at least ``A`` from the right (resp.
  left) edge of the neighbouring section.  Section-placement enforces
  this via ``_enforce_min_column_gaps`` (gap width >= ``A + Σ widths
  + (count-1)*B + A``) so renders honour the symmetric geometry without
  the channel ever being pushed against a section edge.
- Bypass / around-section routes maintain at least ``A`` from any
  intervening section's nearest edge.

Equal to :data:`SECTION_ROUTE_CLEARANCE` (the legacy name) so existing
clearance code paths continue to honour the same physical distance."""

BUNDLE_TO_BUNDLE_CLEARANCE: float = 12.0
"""Constant B: minimum distance between two adjacent bundles sharing
the same inter-section gap.

When *N* concentric bundles travel down the same gap (typically a
``trunk_v_up_pull_away`` bypass paired with an around-section V_up
channel), the required gap width is
``A + Σ bundle_widths + (count-1)*B + A`` where bundle width is
``(n_i - 1) * OFFSET_STEP`` for ``n_i`` lines.  ``B`` gives bundles a
breathing space that reads visually as a separate stream rather than
a single fatter bundle."""

BYPASS_NEST_STEP: float = 8.0
"""Per-line vertical offset for stacking multiple bypass routes."""

HEADER_CLEARANCE: float = 30.0
"""Clearance above/below section headers for inter-row routing channels.

Section headers (numbered circle + label) are rendered above bbox_y by
approximately SECTION_HEADER_PROTRUSION (~26px).  This constant adds a
small margin so routing channels don't overlap the header zone."""

INTER_ROW_EDGE_CLEARANCE: float = 26.0
"""Minimum distance between an inter-row wrap channel and the *box edge*
it runs beneath (the upper section's bbox bottom).

The universal ``EDGE_TO_BUNDLE_CLEARANCE`` (16px) is the floor for a line
sitting beside a bundle; a horizontal inter-row run sitting that close to
a *section box edge* reads as running flush along the underside of the
box.  This wider margin gives the run a visibly clear gap below the box.
It is the box-edge counterpart of ``INTER_ROW_HEADER_CLEARANCE`` on the
lower side, keeping the channel's two margins symmetric about the real
obstacles (box edge above, header badge below)."""

INTER_ROW_HEADER_CLEARANCE: float = SECTION_HEADER_PROTRUSION + INTER_ROW_EDGE_CLEARANCE
"""Distance from a section's bbox top to an inter-row channel above it.

An adjacent-row wrap channel approaching a section from above must clear
the header *badge* (which protrudes ``SECTION_HEADER_PROTRUSION`` above
``bbox_y``) by ``INTER_ROW_EDGE_CLEARANCE``, the same margin the source
side keeps from its bbox bottom.  Section placement reserves this band
(``_wrap_bundle_row_minimums``) and routing centres within it
(``_center_inter_row_channel``); the single definition keeps the two in
lockstep."""

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

Applied after port positioning (Stage 3.5+) so that internal stations move
inward while ports stay put, creating a gap between the perpendicular
entry port and the first internal station.  Mirrors ENTRY_SHIFT_TB."""

EXIT_GAP_MULTIPLIER: float = 0.6
"""Exit gap multiplier for flow-side exits."""

JUNCTION_MARGIN: float = 10.0
"""Baseline margin for positioning junctions in inter-section gaps.

Junction placement helpers in ``layout/engine.py`` derive the actual margin
from this baseline and the junction's fan-out width via
``_required_junction_margin(n)``: a single-line junction (n=1) uses this
baseline directly, while wider fans extend the margin to
``CURVE_RADIUS + (n-1)*OFFSET_STEP + OFFSET_STEP/2`` so the leftmost
line's curve start lands clear of the source section's bbox.  Keeping
the baseline small preserves centred channel positions for simple
fixtures (n=1, 2) while still ensuring clearance for wide fans (n>=3).
"""

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

DIAGONAL_LABEL_OFFSET: float = 9.0
"""Extra downward drop for angled labels (#527), on top of ``LABEL_OFFSET``.

Angled labels are anchored below the pill and tilt down-right; their text
baseline starts at the anchor, so without an extra drop the first glyph
sits too close to the marker.  This bumps the anchor down a touch so there
is a clear gap between the pill and the tilted text."""

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

LABEL_OVERLAP_TOL: float = 2.0
"""Minimum per-axis intrusion (px) before a label box counts as overlapping
another label or a station marker.

A box pair is treated as overlapping only when it intrudes by more than this
on *both* axes, so a label whose edge merely grazes a neighbouring marker
(e.g. the 1px vertical touch between tightly stacked parallel lines) is not
flagged.  Used by the overlap detector that drives the wrapping pass, the
runtime guard, and the layout validator."""

LABEL_WRAP_MIN_LINE_CHARS: int = 4
"""Floor on the per-line character budget when wrapping a colliding label.

Wrapping narrows a label to clear a collision; this stops it shrinking past
a legible width.  A single word longer than the budget is hard-broken with a
hyphen (the last-resort split), but never below this many characters."""

LABEL_GLYPH_INK_RATIO: float = 0.75
"""Fraction of the reserved label width occupied by drawn glyph ink.

``label_text_width`` reserves ``CHAR_WIDTH`` (9px) per character so labels
get generous collision room, but the rendered text at ``label_font_size``
covers appreciably less than that.  Routing-vs-label crossing checks model
the glyph ink as the centred sub-box ``center +/- half_width * this`` so a
line clipping only the empty reserved margin is not mistaken for one striking
through the text."""

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

ICON_TERMINUS_FORK_LEAD: float = 38.0
"""Pre-fork straight run for diagonals leaving a file-input station.

When a file-input (terminus) station fans out to multiple downstream
stations at different Ys, the diagonal placement must not start
inside the file icon's drawn area.  This constant is the minimum
horizontal run from the station marker out past the icon before the
diagonal begins, so the line visually leaves the file before
branching.  Set to TERMINUS_WIDTH + ICON_STATION_GAP (28 + 6) plus a
small visual cushion."""

ICON_INTER_GAP: float = 4.0
"""Gap between adjacent file icons when a station has multiple icons."""

ICON_STACK_LABEL_CLEARANCE: float = 2.0
"""Vertical clearance between a captioned file icon and the next icon below.

Two vertically-adjacent file-input stations whose icons carry under-icon
captions need enough Y spacing for the upper caption to clear the lower
icon.  The required centre-to-centre gap is

    2 * icon_half + caption_gap + caption_font_height + clearance

where ``clearance`` is this constant - the small extra cushion so the
caption text doesn't visually crash into the icon stroke."""

ICON_HALF_HEIGHT: float = 16.0
"""Half-height of a terminus file icon for layout calculations.

Single source of truth for the default icon half-height: ``Theme.terminus_height``
defaults to ``2 * ICON_HALF_HEIGHT`` (see ``render.style.Theme``)."""

ICON_CAPTION_GAP: float = 4.0
"""Gap between the bottom of a terminus icon and its name caption.

Single source of truth: re-exported by ``render.constants`` as
``ICON_NAME_GAP`` so layout spacing math and renderer placement stay
in lockstep without the layout layer depending on render."""

ICON_CAPTION_FONT_HEIGHT: float = FONT_HEIGHT * 0.6
"""Approximate caption font height for layout spacing calculations.

The render side draws under-icon captions at ``label_font_size *
ICON_NAME_FONT_SCALE``; using ``FONT_HEIGHT`` as an upper-bound for
the theme label_font_size keeps the calculation theme-agnostic."""

TERMINUS_ICON_CLEARANCE: float = 58.0
"""Minimum clearance from terminus station center to section bbox edge.

Accounts for station_radius (~5px) + icon gap (6px) + icon width (28px) = 39px
extent, plus ~19px visual margin so icons don't crowd the section border.
"""

TERMINUS_ICON_CLEARANCE_V: float = (
    STATION_RADIUS_APPROX
    + 6.0  # icon gap (render-side ICON_STATION_GAP)
    + 2 * ICON_HALF_HEIGHT
    + ICON_CAPTION_GAP
    + ICON_CAPTION_FONT_HEIGHT
    + 14.0  # visual margin so captions don't crowd the section border
)
"""Minimum clearance from a terminus station center to the section bbox
edge along the *vertical* (flow) axis, used by TB/BT sections.

TB/BT termini stack their file icon (and under-icon caption) below or
above the station instead of beside it, so the reservation uses icon
height + caption height rather than icon width.
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
# Stage-boundary guards
# ---------------------------------------------------------------------------
GUARD_TOLERANCE: float = 5.0
"""Tolerance for stage-boundary invariant checks (port-on-boundary, etc.)."""

COMPONENT_BAND_OVERLAP_TOLERANCE: float = 0.5
"""Slack permitted when checking that independently-stacked disconnected
components occupy disjoint vertical bands."""

# ---------------------------------------------------------------------------
# Canvas-wide grid snap
# ---------------------------------------------------------------------------
CANVAS_GRID_SHIFT_THRESHOLD: float = 0.85
"""Minimum fraction of real stations sharing one ``y % y_spacing`` residue
to trigger a final canvas-wide shift back onto the grid.

Above this threshold, the canvas is treated as uniformly off-grid by a
late helper (typically ``_shift_graph_into_canvas`` shifting by a non-
grid amount): a single shift restores every station to integer
multiples of ``y_spacing``.  Below the threshold, sections sit at
multiple distinct residues by construction, so
no single shift can align them all and the per-section snap from Stage
6.4 is honoured as the best-effort alignment."""


# ---------------------------------------------------------------------------
# Cross-constant relations
# ---------------------------------------------------------------------------
class ConstantRelationError(ValueError):
    """A layout constant violates a required cross-constant ordering."""


def _check_constant_relations() -> None:
    """Enforce the geometric orderings that independently-set constants depend on.

    Unlike the derived constants above (expressed as formulas of their
    parents), each of these holds only *relative to another*.  Explicit
    raises (not ``assert``) keep the checks live under ``python -O``.
    """

    def require(ok: bool, msg: str) -> None:
        if not ok:
            raise ConstantRelationError(msg)

    # Coordinate-tolerance tiers must stay strictly ordered so each answers
    # "same coordinate?" at its own precision without colliding with the next.
    require(
        COORD_TOLERANCE_FINE < SAME_COORD_TOLERANCE < COORD_TOLERANCE,
        "coordinate tolerances must be strictly ordered "
        f"fine ({COORD_TOLERANCE_FINE}) < same ({SAME_COORD_TOLERANCE}) "
        f"< coarse ({COORD_TOLERANCE})",
    )
    # A same-coordinate test must never swallow an adjacent per-line offset
    # slot, or two parallel lines collapse onto one coordinate.
    require(
        SAME_COORD_TOLERANCE < OFFSET_STEP,
        f"SAME_COORD_TOLERANCE ({SAME_COORD_TOLERANCE}) must stay below "
        f"OFFSET_STEP ({OFFSET_STEP}) so offset slots are not merged",
    )
    # Bypass corners turn with CURVE_RADIUS each side; without 2*CURVE_RADIUS
    # of vertical clearance the two corners overlap the channel.
    require(
        BYPASS_CLEARANCE >= 2 * CURVE_RADIUS,
        f"BYPASS_CLEARANCE ({BYPASS_CLEARANCE}) must be >= 2*CURVE_RADIUS "
        f"({2 * CURVE_RADIUS}) so bypass corners clear the channel",
    )
    # Nesting multiple bypass routes must step them apart by more than the
    # per-line offset, else stacked bypasses read as one bundle.
    require(
        OFFSET_STEP < BYPASS_NEST_STEP,
        f"OFFSET_STEP ({OFFSET_STEP}) must stay below BYPASS_NEST_STEP "
        f"({BYPASS_NEST_STEP}) so nested bypass routes separate visibly",
    )
    # A port offset from a station by up to the bundle's offset span must not
    # be mistaken for an elbow at that station.  The current value is
    # 4 * OFFSET_STEP; only the floor (>= OFFSET_STEP) is required to hold.
    require(
        STATION_ELBOW_TOLERANCE >= OFFSET_STEP,
        f"STATION_ELBOW_TOLERANCE ({STATION_ELBOW_TOLERANCE}) must be "
        f">= OFFSET_STEP ({OFFSET_STEP})",
    )


_check_constant_relations()
