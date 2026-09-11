"""Stable identities for production inter-section route families."""

from enum import Enum


class RouteFamilyId(str, Enum):
    """A classified or rail family that emitted an inter-section route."""

    PERP_EXIT = "perp-exit"
    TB_PERP_EXIT_OVER = "tb-perp-exit-over"
    SAME_Y_STRAIGHT = "same-y-straight"
    PERP_EXIT_FAR_SIDE_WRAP = "perp-exit-far-side-entry-wrap"
    TB_BOTTOM_EXIT_AROUND_STACK = "tb-bottom-exit-around-stack"
    TB_BOTTOM_EXIT = "tb-bottom-exit"
    TOP_ENTRY_L_SHAPE = "top-entry-l-shape"
    BOTTOM_ENTRY_L_SHAPE = "bottom-entry-l-shape"
    SAME_X_VERTICAL_DROP = "same-x-vertical-drop"
    BOTTOM_EXIT_JUNCTION = "bottom-exit-junction"
    BOTTOM_EXIT_JUNCTION_RIGHT_LANDINGS = "bottom-exit-junction-right-landings"
    BOTTOM_EXIT_JUNCTION_VIA_GAP = "bottom-exit-junction-via-gap"
    MERGE_TRUNK = "merge-trunk"
    MERGE_TRUNK_AROUND_BELOW = "merge-trunk-around-below"
    MERGE_BRANCH = "merge-branch"
    BYPASS_FAMILY = "bypass-family"
    BYPASS_L_SHAPE = "bypass-l-shape"
    BYPASS_LEFT_ENTRY = "bypass-left-entry"
    BYPASS_LEFT_EXIT_AROUND_BELOW = "bypass-left-exit-around-below"
    BYPASS_CELLMATE_GAP_DROP = "bypass-cellmate-gap-drop"
    BYPASS_PACKED_CELL_SAME_ROW = "bypass-packed-cell-same-row"
    BYPASS_RIGHT_ENTRY_CROSS_ROW = "bypass-right-entry-cross-row"
    NEAR_VERTICAL_JUNCTION = "near-vertical-same-col-junction"
    RIGHT_ENTRY_WRAP = "right-entry-wrap"
    LEFT_ENTRY_WRAP = "left-entry-wrap-family"
    LEFT_ENTRY_CORRIDOR = "left-entry-corridor"
    SERPENTINE_LEFT = "serpentine-left-exit-left-entry"
    LEFT_EXIT_FAR_SIDE_WRAP = "left-exit-far-side-left-entry-wrap"
    MERGE_ENTRY = "merge-entry-family"
    MERGE_ENTRY_STRAIGHT = "merge-entry-straight"
    MERGE_ENTRY_CORRIDOR = "merge-entry-corridor"
    MERGE_ENTRY_AROUND_BELOW = "merge-entry-around-below"
    MERGE_ENTRY_PERPENDICULAR = "merge-entry-perpendicular"
    RIGHT_ENTRY_PLOUGH_BYPASS = "right-entry-plough-bypass"
    RIGHT_ENTRY_CROSS_ROW_WRAP = "right-entry-cross-row-wrap"
    STANDARD_L_SHAPE = "standard-l-shape"
    RAIL_INTER_SECTION = "rail-inter-section"


BYPASS_ROUTE_FAMILIES = frozenset(
    {
        RouteFamilyId.BYPASS_FAMILY,
        RouteFamilyId.BYPASS_L_SHAPE,
        RouteFamilyId.BYPASS_LEFT_ENTRY,
        RouteFamilyId.BYPASS_LEFT_EXIT_AROUND_BELOW,
        RouteFamilyId.BYPASS_CELLMATE_GAP_DROP,
        RouteFamilyId.BYPASS_PACKED_CELL_SAME_ROW,
        RouteFamilyId.BYPASS_RIGHT_ENTRY_CROSS_ROW,
    }
)
BYPASS_ROUTE_FAMILY_VALUES = frozenset(
    family_id.value for family_id in BYPASS_ROUTE_FAMILIES
)

LANDING_POINT_SETTLED_LATER_FAMILIES = frozenset(
    {
        RouteFamilyId.MERGE_BRANCH,
        RouteFamilyId.LEFT_ENTRY_WRAP,
        RouteFamilyId.RIGHT_ENTRY_CROSS_ROW_WRAP,
        RouteFamilyId.RIGHT_ENTRY_WRAP,
        RouteFamilyId.TOP_ENTRY_L_SHAPE,
        RouteFamilyId.BOTTOM_ENTRY_L_SHAPE,
        *BYPASS_ROUTE_FAMILIES,
    }
)
"""Families whose turn leg ends in a channel seated after gap allocation.

The planner holds the axis the turn stands on but not how far along it the leg
runs, so the landing-side (destination) corner is settled by a later pass. Any
validator that inspects a settled turn's corner geometry can only hold the
entry-side corner for these families; the landing-side corner does not yet
exist.
"""

LANDING_POINT_SETTLED_LATER_FAMILY_VALUES = frozenset(
    family_id.value for family_id in LANDING_POINT_SETTLED_LATER_FAMILIES
)
