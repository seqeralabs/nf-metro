"""Layout constants used across layout modules.

Centralizes magic numbers from engine.py, routing.py, labels.py,
section_placement.py, and ordering.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

FLOW_ALIGNED_PORT_ADVICE: str = (
    "Give the section a flow-aligned entry/exit port "
    "(left/right for LR/RL, top/bottom for TB/BT) or change "
    "its '%%metro direction:'."
)
"""Actionable advice for a section whose ports are all perpendicular to its
flow, or whose connection has to be bridged across grid columns.  Shared by the
bbox-containment guard and the render-curve invariant so the two surface one
consistent fix."""

# ---------------------------------------------------------------------------
# Font / text metrics
# ---------------------------------------------------------------------------
FONT_HEIGHT: float = 14.0
"""Conservative station-label height used by theme-agnostic layout."""

LABEL_FONT_SIZE: float = 13.0
"""Theme-agnostic station-label size used by layout measurement."""

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

STATION_STROKE_APPROX: float = 1.5
"""Approximate station marker stroke width for layout spacing calculations.

Matches the nfcore theme; other themes use 2 px, a difference small enough to
absorb in the label-clearance math (mirrors :data:`DEFAULT_LINE_WIDTH`).  A
plain marker's outline is centred on its radius (outer edge at
``radius + stroke/2``); an interchange knob's outline is drawn outside the knob
(outer edge at ``radius * RAIL_KNOB_RADIUS_RATIO + stroke``).
"""

RAIL_KNOB_RADIUS_RATIO: float = 1.35
"""Interchange knob circle radius, as a multiple of the station radius.

A spanning interchange draws each rail's knob larger than a bare marker.  The
renderer re-exports this ratio for the glyph, and label placement uses it to
clear a spanning-interchange label off the enlarged end knob rather than only
off the member centre.
"""

SECTION_X_PADDING: float = 50.0
"""Horizontal padding around section content."""

SECTION_Y_PADDING: float = 50.0
"""Vertical padding around section content."""

MIN_BUNDLE_EDGE_CLEARANCE: float = 28.0
"""Minimum room a station's drawn multi-line bundle pill keeps from its
section's bbox edge, independent of ``SECTION_Y_PADDING``.

``SECTION_Y_PADDING`` is measured from a station's anchor lane (offset 0),
not its drawn pill edge, so a wide bundle (many co-routed lines, offsets
priority-ordered rather than centred on the anchor) can leave far less
room than intended.  This floor guarantees enough clearance for a label
(``LABEL_OFFSET`` + ``LABEL_FONT_SIZE`` + ``DESCENDER_CLEARANCE``) to sit
off the pill without crowding the edge, for every multi-line bundle -- not
only ones a symmetric-diamond fan places at mirrored offsets."""

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

SECTION_HEADER_ROUTE_CLEARANCE: float = 4.0
"""Minimum gap between routed ink and a drawn section-header keepout."""

MIN_INTER_SECTION_ROW_GAP: float = 12.0
"""Minimum visual gap between section bottom and the next section's header.

Applied after accounting for SECTION_HEADER_PROTRUSION, so the actual
bbox-to-bbox distance will be MIN_INTER_SECTION_ROW_GAP + protrusion.
"""

TITLE_BAND_BOTTOM: float = 36.0
"""Lowest y a map title's glyphs reach, measured from the canvas top.

The title baseline sits at render's ``TITLE_Y_OFFSET`` (30) and the largest
title font across themes is ~26px, whose descenders drop ~6px below the
baseline.  Mirrors render geometry the way ``SECTION_HEADER_PROTRUSION``
does; layout has no theme, so it reserves against the tallest title.
"""

TITLE_BAND_OVERLAP_FLOOR: float = TITLE_BAND_BOTTOM + SECTION_HEADER_PROTRUSION
"""``bbox_y`` at which a drawn section's header badge stops overlapping the title.

The badge protrudes ``SECTION_HEADER_PROTRUSION`` above the box top, so a top
below this sits its badge above the title's lowest glyph -- the defect.  This
is the hard floor a titled map must never breach; a map already at or below it
(box top above it) is left alone so the fix never over-pads a map that was
already clear.
"""

TITLE_BAND_CLEARANCE: float = TITLE_BAND_OVERLAP_FLOOR + 8.0
"""``bbox_y`` a titled map's topmost drawn section is lifted to when it overlaps.

Only a map whose header actually overlaps the title (top above
``TITLE_BAND_OVERLAP_FLOOR``) is moved, and it lands here -- a small band above
the floor so the badge sits a visible gap below the title rather than flush
against it.  Untitled maps keep the tighter ``SECTION_Y_PADDING`` top.
"""

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
DIAGONAL_RUN: float = 30.0
"""Length of the diagonal segment in direction changes."""

OFF_TRACK_OUTPUT_TAIL: float = X_SPACING / 2
"""Flat run into an off-track output icon, after the diagonal.

Half a station gap: enough for the line to read as a settled horizontal approach
into the icon without stretching the section.  Shared by the layout phase that
places the icon and the router that seats the diagonal so the tail is uniform.
"""

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

PERP_PORT_EDGE_CLEARANCE: float = CURVE_RADIUS
"""Minimum room a port keeps from the two bbox edges it is *not* anchored to.

A port is pinned to one edge (a LEFT/RIGHT port to a vertical one, a TOP/BOTTOM
port to a horizontal one) and is free along its other axis.  Flush against a
second edge, its inbound run is drawn along the box border and the two read as
one stroke; it also blocks the section header's above-left position, pushing
the badge away from the corner it labels.  One curve radius is the shortest
separation that still reads as a route inside the box rather than on its edge.
"""

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

OFFSET_STEP: float = 4.0
"""Per-line offset increment for parallel lines in bundles."""

DEFAULT_LINE_WIDTH: float = 3.0
"""Stroke width used when converting a visual *track_gap* to an offset step.

Matches the nfcore theme (3 px). The seqera theme uses 4 px; that 1 px
difference is absorbed by rounding elsewhere in the layout pipeline.
"""


def resolve_offset_step(
    track_gap: float | None, line_width: float = DEFAULT_LINE_WIDTH
) -> float:
    """Return the centre-to-centre bundle offset step for *track_gap*.

    ``track_gap`` is the user-visible *visual* gap -- the empty space between
    adjacent line stroke edges, not between their centres.  The centre-to-centre
    offset is ``track_gap + line_width``.

    ``None``  → built-in default (``OFFSET_STEP``, currently 4 px).
    ``0``     → lines touch (zero gap between edges); offset = ``line_width``.
    ``X > 0`` → ``X + line_width`` centre-to-centre.
    """
    if track_gap is None:
        return OFFSET_STEP
    return track_gap + line_width


def graph_offset_step(
    graph: "MetroGraph", drawn_line_width: float | None = None
) -> float:
    """Return *graph*'s bundle offset step under its ``stroke_scale``.

    Layout and render both resolve the step here so the pitch they assume can
    never diverge.  *drawn_line_width* is the width strokes are actually painted
    at, ``stroke_scale`` already applied -- the render side passes its scaled
    theme's value; the layout side omits it and gets the theme-agnostic
    :data:`DEFAULT_LINE_WIDTH` proxy, scaled to match.

    ``stroke_scale`` widens the gap as well as the stroke.  Thickening tracks
    while holding the inter-track gap at its absolute default would close that
    gap up as the map is downscaled, merging a bundle into one fat stroke and
    costing exactly the line-counting legibility the scaling is meant to buy.
    """
    scale = graph.stroke_scale
    if graph.track_gap is None:
        return OFFSET_STEP * scale
    width = DEFAULT_LINE_WIDTH * scale if drawn_line_width is None else drawn_line_width
    return graph.track_gap * scale + width


COORD_TOLERANCE: float = 1.0
"""Tolerance for coordinate comparison (same X or same Y)."""

COORD_TOLERANCE_FINE: float = 0.01
"""Fine tolerance for detecting nearly identical Y coordinates."""

SETTLEMENT_QUANTUM: float = 1.0
"""Granularity of an envelope-settlement translation.

Settlement rounds positive deficits to whole quanta and at least two quanta.
That minimum lands the settled boundary clear of the ``COORD_TOLERANCE`` band
the reservation ledger measures against, so a second settlement pass sees no
residual deficit.
"""

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

COORD_GROUP_DIGITS_COARSE: int = 1
"""Decimal places for the coarser of the two coordinate-grouping precisions.

Several layout passes group stations into a column/row bucket by rounding a
coordinate to a stable dict/set key (absorbing float drift from arithmetic
like averaging).  This precision is used where the grouped axis only needs to
resolve to sub-pixel-visible differences (e.g. off-track column stacking)."""

COORD_GROUP_DIGITS_FINE: int = 3
"""Decimal places for the finer of the two coordinate-grouping precisions.

Used where a coordinate-grouping key must distinguish stations placed by
finer arithmetic (e.g. sub-pixel column alignment during bundle balancing)
than :data:`COORD_GROUP_DIGITS_COARSE` would resolve."""

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

EDGE_TO_BUNDLE_CLEARANCE: float = 16.0
"""Constant A: minimum distance between a section bbox edge and the
nearest line of an adjacent route bundle or external route channel.

Used as the single source of truth for three related clearances:

- The leftmost (resp. rightmost) line of a bundle running vertically
  in an inter-section gap sits at least ``A`` from the right (resp.
  left) edge of the neighbouring section.  Section-placement enforces
  this via ``_enforce_min_column_gaps`` (gap width >= ``A + Σ widths
  + (count-1)*B + A``) so renders honour the symmetric geometry without
  the channel ever being pushed against a section edge.
- Bypass / around-section routes maintain at least ``A`` from any
  intervening section's nearest edge.
- An external route channel (a wrap channel, an around route, an
  inter-row bypass) sits at least ``A`` beyond the bbox edge it runs
  past.  Without the floor such a channel can land one curve radius
  plus one offset step (~13 px) from the edge, which reads as flush
  against the section."""

SECTION_ROUTE_CLEARANCE: float = EDGE_TO_BUNDLE_CLEARANCE
"""Alias of :data:`EDGE_TO_BUNDLE_CLEARANCE` for external-route call sites."""

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

NEXT_ROW_HEADER_BADGE_CLEARANCE: float = 12.0
"""Minimum vertical gap a near-horizontal inter-section routed segment must
keep above a next-row section header badge.

A segment doglegged down into the inter-row gap sits above the lower row's
header badge (which protrudes ``SECTION_HEADER_PROTRUSION`` above its
``bbox_y``).  This margin exceeds the stacked-bundle half-width yet stays
below the band where TOP-entry channel routes legitimately approach the
badge, so the segment reads as clearly separate from the header rather than
grazing it.  Enforced by ``test_routed_paths_clear_next_row_headers``."""

DIRECTIONAL_MARKER_HALF_EXTENT: float = 4.0
"""Half-extent of a direction chevron about the path point carrying it.

Mirrors ``Theme.directional_marker_size`` (the arm half-length and
half-width), which the reservation ledger cannot read: a corridor's
clearances are a property of its region, resolved before any theme is in
hand."""

WIDEST_THEME_LINE_WIDTH: float = 4.0
"""The widest ``Theme.line_width`` any registered brand sets.

A corridor's clearances are a property of its region, resolved before a theme
is in hand, so a demand that has to hold for every brand takes the widest
stroke rather than the default one.  Pinned to the registry by
``test_the_canvas_edge_clearance_bounds_every_theme``."""

CANVAS_EDGE_CLEARANCE: float = (
    DIRECTIONAL_MARKER_HALF_EXTENT + WIDEST_THEME_LINE_WIDTH / 2
)
"""Minimum distance between a canvas-margin corridor and the canvas edge.

The only thing drawn beyond a corridor's centreline on its canvas side is
the stroke's own half-width plus, where ``directional: true`` is set, a
direction chevron; a corner arc is inscribed *inboard* of the centreline and
never reaches past it.  So this is what has to fit, not a curve radius:
demanding one flags every run that turns beside the canvas as short of room
it does not need."""

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

BYPASS_CLEARANCE: float = INTER_ROW_EDGE_CLEARANCE
"""Vertical clearance below the lowest intervening section for bypass routes.

A bypass channel is a horizontal run drawn beneath a section's box edge, which
is the relationship ``INTER_ROW_EDGE_CLEARANCE`` states, and the reservation
ledger insets every row-gap corridor by that same margin.  A narrower margin
here would seat a bypass inside the band its own reservation allocates it."""

ROW_BAND_SLACK: float = BYPASS_CLEARANCE + Y_SPACING
"""Vertical slack a same-row inter-section route may extend past the row band.

A same-row wrap routes below the row's tallest section through a bypass
channel sitting ``BYPASS_CLEARANCE`` below the band bottom, then stacks the
bundle's per-line nest offsets (a few ``OFFSET_STEP`` each) on top, and adds
up to one ``Y_SPACING`` for the diagonal corner approach.  This slack bounds
that legitimate excursion so the band guard / invariant test admit a clean
below-row wrap while still rejecting a route that dips a full row down."""

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

ENTRY_SHIFT_LR: float = 0.5
"""Station shift multiplier for LR/RL sections with perpendicular entry.

Applied after port positioning (Stage 3.4+) so that internal stations move
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

PERP_PORT_EDGE_INSET: float = SECTION_Y_PADDING - MIN_PORT_STATION_GAP
"""Room a perpendicular port keeps from the flow-axis bbox edge it faces.

A port seated the minimum station gap beyond the outermost station already earns
this much, because the edge itself trails that station by ``SECTION_Y_PADDING``.
A port seated further out -- a perpendicular entry sits a whole entry shift clear
of the row -- would otherwise be left with only the remainder and read as pinched
against the border, so the box extends to give it the same room instead.

Distinct from ``PERP_PORT_EDGE_CLEARANCE``: that is the hard floor a runtime
guard enforces, this is the wider spacing placement aims for.
"""

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
a legible width.  Wrapping breaks only on whitespace, so a label whose
longest word is already wider than this budget keeps that word intact and
takes its width as the floor instead."""

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

OFFTRACK_TERMINUS_NUB_CLEARANCE: float = 8.0
"""Extra drop applied to a captioned off-track rail terminus station.

A rail-mode off-track file terminus stacks its icon vertically above the line
stub, with the buffer-stop nub seated at the stub end (``station.y``) and the
under-icon caption hanging toward it.  Unlike an on-rail terminus -- where the
caption sits perpendicular to the nub -- here both share the vertical axis, so
the caption lands on the nub.  Dropping the station by this amount while the
renderer lifts the icon by the same amount keeps the icon fixed and slides the
nub clear below the caption.  Shared by ``rail_mode`` (the drop) and
``render.svg`` (the matching icon lift) so the two stay in lockstep."""

OFF_TRACK_TRUNK_CLEARANCE_MARGIN: float = 2.0
"""Small margin so an off-track icon's stroke doesn't touch a trunk line
track it is being bumped clear of."""

TERMINUS_ICON_GAP: float = 6.0
"""Gap between a terminus station pill and its first file icon.

Layout-side mirror of the renderer's ``ICON_STATION_GAP`` so the two
agree on where the icon stack starts without layout depending on render."""

TERMINUS_ICON_CLEARANCE: float = 58.0
"""Minimum clearance from terminus station center to section bbox edge.

Accounts for station_radius (~5px) + icon gap (6px) + icon width (28px) = 39px
extent, plus ~19px visual margin so icons don't crowd the section border.
"""

TERMINUS_ICON_CLEARANCE_V: float = (
    STATION_RADIUS_APPROX
    + TERMINUS_ICON_GAP
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

EXIT_CORRIDOR_ICON_CLEARANCE: float = CURVE_RADIUS
"""Gap a flow-axis exit corridor keeps below a terminus icon hanging into
the exit row of a vertical-flow (TB/BT) section.

The exit port (and the horizontal corridor a route turns onto to leave
the section) is clamped to at least the icon's drawn far edge plus this
margin, so a route does not graze the file icon on its way out.  Sized
to clear the bundle-offset spread of the lines on the corridor."""

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

COLLINEAR_AXIS_TOL: float = 0.5
"""Maximum constant-axis separation for two axis-aligned legs to count as
sharing one track.  Tight (sub-pixel) so legs on a single track register as
collinear while neighbouring legs one slot apart in a routing channel do not."""

PORT_BOUNDARY_CROSSING_TOL: float = 24.0
"""Distance from a declared port within which a routed segment crossing a
section bbox edge is treated as that port's legitimate entry/exit, not a
guard-forbidden cut through the box."""

BOUNDARY_CROSSING_INSET: float = 4.0
"""Inward slack on a section bbox edge when checking whether a crossing
point falls within the edge's perpendicular extent, absorbing routed
segments that land a few pixels past the box's own corner."""

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
