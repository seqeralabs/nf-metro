# Routing

Routing turns the laid-out `MetroGraph` (stations, ports, and junctions
with coordinates) into a list of `RoutedPath` polylines, one per edge.
The entry point is `route_edges` in
[`src/nf_metro/layout/routing/core.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/routing/core.py).

Lines are drawn as horizontal runs joined by 45-degree diagonal
transitions; inter-section edges use L-shaped (horizontal + vertical)
routing.

## Rail mode short-circuit

Before the normal dispatch, `route_edges` checks the graph's
`line_spread` (a `LineSpread` of `BUNDLE` / `CENTERED` / `RAILS`):

- When `line_spread is LineSpread.RAILS`, the whole graph is routed by
  `route_rail_edges` in
  [`routing/rail.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/routing/rail.py)
  and `route_edges` returns early.
- When only some sections opt into rails (`has_rail_sections`), the
  edges internal to those rail sections are routed by `route_rail_edges`
  up front; the rest fall through to the normal handler chain below.

Rail routing does not bundle: each line runs along a single fixed
horizontal rail Y (assigned in `layout/rail_mode.py`), so each edge is a
straight horizontal run at its line's rail Y, and shared stations render
as interchange pills bridging the rails.

## Dispatch order

`route_edges` first builds a `_RoutingCtx` (a dataclass of shared
pre-computed state: merge-junction classification, fold X, bundle info,
per-station offsets, fork stations, etc.), then routes each edge by
trying a fixed sequence of handler functions in priority order.  **The
first handler that returns a `RoutedPath` wins**; handlers that do not
apply return `None`.

The order in `route_edges` is:

1. `_route_inter_section` - edges crossing a section boundary
   (port/junction to port/junction).  Dispatches internally to a large
   family of sub-handlers (L-shape, top-entry L-shape, left/right-entry
   wraps, TB bottom-exit, merge trunk/branch, bypass, stepped descent,
   inter-row corridors, around-section-below).
2. `_route_tb_section` - edges touching a `TB` section.  Dispatches over
   the ordered `_TB_SECTION_SHAPES` tuple (first match wins): internal
   vertical drops (`_route_tb_internal`), internal station to a LEFT/RIGHT
   exit port (`_route_tb_lr_exit`), LEFT/RIGHT entry port to an internal
   station (`_route_tb_lr_entry`), and TOP/BOTTOM port to an internal
   station (`_route_perp_entry`).  Each shape describes a centreline and
   fans it through the bundle builder (`build_tapered_bundle` /
   `build_offset_bundle`), so no handler hand-assembles per-line points or
   curve radii; the perpendicular-entry corridor variant
   (`_route_perp_entry_from_corridor`) routes the same way.
3. `_route_entry_runway` - flow-side entry port to a deep internal
   station: compresses the diagonal into the entry region and runs a
   horizontal runway past the bypassed early-layer stations.
4. `_route_intra_section` - the general intra-section case: diagonals,
   cross-row fold routing, and straight lines.  This is also the
   fallback for port/junction-to-port/junction edges that the inter-
   section family did not claim.

After all edges are routed, `route_edges` runs a series of post-passes
that adjust the assembled polylines as a set (for example
`_spread_diagonal_bundles`, `_materialize_gap_slots`,
`_materialize_trunk_slots`, `_coincide_same_line_tracks`).

## Bundles and offsets

When several lines travel between the same pair of endpoints they form a
**bundle**.  Per-line offsets (computed by `compute_station_offsets`,
applied through `_RoutingCtx.station_offsets`) fan the bundle out into
parallel tracks so individual lines stay visually distinct.  Bundle
ordering is preserved across multi-corner paths by handedness-aware
offset propagation at each corner (the corner-radius helpers live in
`routing/corners.py`).  The runtime guard
`check_bundle_order_preserved` (in `routing/invariants.py`) catches any
regression where a line crosses over its bundle-mates.

## The descriptor catalogue (`WRAP_TABLE`)

[`routing/inter_section.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/routing/inter_section.py)
defines `WRAP_TABLE`, a declarative mirror of the inter-section
if-cascade.  It is a **documentation reference only**: nothing in
routing consumes it at runtime.  Each entry is keyed by

```
(exit_side, entry_side, drow_sign, dcol_sign)
```

where `exit_side` is `None` when the source is a junction without a port
side, and `drow_sign` / `dcol_sign` are `sign()` values in
`{-1, 0, +1}` describing the section-grid step.  Each value is a
`WrapDescriptor` recording the `RouteKind` (e.g. `L_SHAPE`,
`TOP_ENTRY_L_SHAPE`, `LEFT_ENTRY_WRAP`, `RIGHT_ENTRY_WRAP`,
`TB_BOTTOM_EXIT`), a `ChannelKind`, and a `TurnSequence` - the ordered
list of `Corner`s (each an `(incoming, outgoing)` `Direction` pair).

`TurnSequence.parity` counts how many times the corner handedness
(`CW` / `CCW`) flips along the path; it is the contract pinned by
[`tests/test_inter_section_descriptor.py`](https://github.com/pinin4fjords/nf-metro/blob/main/tests/test_inter_section_descriptor.py),
which also sanity-checks that every key is well-formed and that the
table stays non-empty.  Same-row L-shapes and the straight TB
`(BOTTOM, TOP, 1, 0)` drop are intentionally absent because the
dispatcher handles those degenerate cases directly.

## Module map

| Module | Responsibility |
| --- | --- |
| `core.py` | `route_edges` dispatcher; re-exports handlers from sibling modules for backward-compatible imports |
| `context.py` | `_RoutingCtx` dataclass and `_build_routing_context`; per-station offset helpers; shared section-geometry helpers (`_resolve_section_col`, `_has_intervening_sections`, `compute_junction_fan_info`, …) |
| `inter_section_handlers.py` | handler 1 family: bypass, left/right entry wraps, around-section, inter-row corridors, stepped descent, L-shape |
| `tb_handlers.py` | TB section shapes dispatched by `_route_tb_section` over `_TB_SECTION_SHAPES` (`_route_tb_internal`, `_route_tb_lr_exit`, `_route_tb_lr_entry`, `_route_perp_entry`, `_route_perp_entry_from_corridor`) and `_compute_diagonal_placement` |
| `intra_handlers.py` | `_route_entry_runway` and `_route_intra_section` (the general intra-section fallback) |
| `bundle.py` | constructive bundle-curve builders (`build_concentric_bundle`, `build_tapered_bundle`, `build_offset_bundle`); fans a centreline into per-line offset paths with concentric corners |
| `centrelines.py` | centreline templates and bundle-gathering helpers (`gather_member_edges`, `route_along`, `route_tapered`, …) layered over `bundle.py` |
| `postprocess.py` | post-routing passes: diagonal bundle spread and bubble-station centring |
| `normalize.py` | channel and trunk normalization passes (`_materialize_gap_slots`, htrunk restacking, riser/port-approach alignment, …) |
| `common.py` | `RoutedPath`, `Direction`, bundle/channel helpers |
| `corners.py` | corner radii and curve smoothing |
| `inter_section.py` | `WRAP_TABLE` descriptor catalogue (documentation reference; not used at runtime) |
| `offsets.py` | per-station Y offsets for parallel lines |
| `reversal.py` | fold/reversal (serpentine row) routing |
| `invariants.py` | runtime routing guards (`check_bundle_order_preserved`) |
| `rail.py` | `route_rail_edges` straight-rail router for rail mode |
