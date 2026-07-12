---
title: "Guard tiers"
description: The layered guard system in nf-metro — pre/post invariant checks, ratchets, and how violations surface during development.
sidebar:
  order: 7
---

nf-metro defends its layout output with two families of runtime invariant:

- **`_guard_*` functions** in `src/nf_metro/layout/phases/guards.py` - raise
  `PhaseInvariantError` on a violation.
- **`check_*` functions** in `src/nf_metro/layout/routing/invariants.py` -
  return a list of violation objects; a caller decides whether to raise.

The render path runs the cheap **Tier-A** subset of both families on the
settled geometry, so a layout defect reaches the end user as a warning (or a
`--strict` error) instead of a silently-broken SVG:

- `assert_render_layout_invariants` (`phases/guards.py`) runs the Tier-A
  `_guard_*` postconditions, and
- `assert_render_curve_invariants` (`routing/invariants.py`) runs the Tier-A
  routing `check_*` invariants.

The **Tier-B** remainder of the `_guard_*` suite is gated behind
`compute_layout(validate=True)`, which the `nf-metro render` path never sets;
it is costlier or depends on a mid-pipeline reroute.

Tier A is the always-on render-path set; Tier B is the `validate=True` set.

## Tiers

| Tier  | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Promotion intent                                                                 |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| **A** | Cheap, observational structural checks: placement (finite coords, bbox containment with marker overhang, station overlap, ports on boundaries) **plus** `_guard_stations_within_bbox`, the inter-section backtrack/wrap guards (`_guard_inter_section_route_no_backtrack`, `_guard_inter_section_route_no_full_width_backtrack`, `_guard_serpentine_no_backtrack`, `_guard_inter_section_route_clears_own_section_interior`), the non-consumer-section breeze-through guards (`_guard_no_route_through_section`, `_guard_no_line_crosses_non_consumer`), and the routing `check_*` invariants already always-on via the render chokepoint. | Run on the default render path, warning by default with a `--strict` escalation. |
| **B** | The remaining `validate=True` set: route-shape, bundle-order, label, rail, and merge-port geometry. Correct but either costlier or dependent on a mid-pipeline reroute (they consume `route_edges` output), so not cheap-always-on.                                                                                                                                                                                                                                                                                                                                                                                                        | Stay behind `validate=True` / `--strict`.                                        |
| **C** | Test-only oracles: too slow, too fixture-specific, or non-observational.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Live in the test suite, not the runtime suite.                                   |

Tier A is mostly the cheap-observational set the cost audit confirms runs in
low tens of microseconds, with a handful of routed-geometry sweeps in the
~10-90 us range whose visibility matters more than their cost: the
inter-section backtrack/wrap guards (the dearest ~11 us) and the
non-consumer-section breeze-through guards (`_guard_no_route_through_section`
~86 us, `_guard_no_line_crosses_non_consumer` ~75 us) — a line plotted over a
section or station it never touches is at least as visibly broken as a
backtracking bundle, so it stays on the always-on path despite the cost. Tier
B holds everything else that is materially more expensive or depends on a
mid-pipeline reroute; its most expensive members
(`_guard_no_opposing_line_overlap` ~86 us, `_guard_trunk_bands_crossing_optimal`
~57 us) are routed-geometry sweeps. The always-on `check_*` routing invariants
(`routing/invariants.py`) run through a separate chokepoint and include
members up to `check_no_hanging_routes` at ~470 us; cost alone does not gate
Tier-A membership in either family, visibility of the defect does. **Tier C**
holds the two seam oracle checks - `check_seam_approach_equals_departure` and
`check_seam_segments_meet_at_port` - which verify the rotation-unification
property: at every inter-section seam the approach must place each line on the
lane coordinate that `lane_x` assigns it. Both are correctness oracles for the
rotation series rather than runtime guards, so they live in the test suite
(`tests/test_seam_lane_x.py`). Every other guard and check is reachable from
`compute_layout` or the render chokepoint.

## Registries

The classification is data, not prose:

- **`GUARD_REGISTRY`** (`phases/guards.py`) - the single ordered source of
  truth for the `validate=True` guard call sequence. Its order _is_ the call
  order; `run_validate_guards` iterates it. Each entry is a `GuardSpec` with
  `tier`, the `needs` set (which of `offsets` / `routes` / `section_y_*` the
  guard takes), `bisection_safe` (runs at every Pass C checkpoint, gated by
  `first_valid_stage`) vs final-only, and the `_BISECTION_FIRST_VALID` data
  derived back out for the engine re-export.
- **`INLINE_GUARD_REGISTRY`** (`phases/guards.py`) - the same `GuardSpec`
  schema applied to the guards `engine.py` invokes directly at a specific
  pipeline stage rather than through the Pass C / final runner. Like
  `CHECK_REGISTRY` it is _classification_ only (no `needs` / `bisection_safe`
  dispatch data), so every defined `_guard_*` lives in exactly one of the two
  guard registries and none escapes tier / issue-pin classification.

The Tier-A `_guard_*` postconditions across both registries are run on the
render path by `assert_render_layout_invariants`, the sibling of the routing
chokepoint. `render_layout_invariant_specs` selects them: every Tier-A guard
except `_RENDER_CHOKEPOINT_AUTHORING_GUARDS`, the two authoring-error guards
(`_guard_no_same_row_backward_feed`, `_guard_no_mixed_entry_directions`) that
raise a `ValueError` on un-renderable input and stay always-on hard fails in
the engine rather than joining the warn-by-default chokepoint.

- **`CHECK_REGISTRY`** (`routing/invariants.py`) - the same `GuardSpec` schema
  applied to the routing checks. Because checks return lists rather than
  raising, this is a _classification_ registry, not a dispatcher; the runtime
  chokepoint stays `assert_render_curve_invariants`. The registries are
  unified through the shared schema and this page, **not** by merging the
  raise-vs-return error protocols.

Each `GuardSpec` also carries `issue_pin` (the `#NNN` issues a guard was born
from, kept as data so consolidation cannot lose the regression trail) and
`narrow_reason` (why an issue-pinned guard stays scoped to its case rather than
being a general property). `tests/test_guard_registry.py` keeps these honest:
every guard and check is registered exactly once, the Tier-A check set equals
the curve render chokepoint exactly, the render-layout chokepoint equals the
Tier-A guard set minus the authoring guards, no validate-only guard duplicates
an always-on check, and every issue-pinned guard records its issue and a
`narrow_reason`.

## Cost audit

`scripts/guard_cost_audit.py` lays out every fixture under `examples/` +
`examples/topologies/` once, then times each registered guard and check against
the final geometry. Mean microseconds-per-fixture below are from that run;
regenerate with:

```bash frame="terminal"
python scripts/guard_cost_audit.py --json /tmp/guard_cost.json
```

### Validate-suite guards (`GUARD_REGISTRY`)

| guard                                                    | tier | mean us | dispatch                                  |
| -------------------------------------------------------- | ---- | ------: | ----------------------------------------- |
| `_guard_coordinates_finite`                              | A    |     1.5 | bisection (first valid: start)            |
| `_guard_section_bboxes_positive`                         | A    |     0.4 | bisection (first valid: start)            |
| `_guard_stations_in_sections`                            | A    |     3.5 | bisection (first valid: after Stage 5.3)  |
| `_guard_ports_on_boundaries`                             | A    |     1.3 | bisection (first valid: start)            |
| `_guard_no_station_overlap`                              | A    |    13.5 | bisection (first valid: after Stage 6.4)  |
| `_guard_no_coincident_station_coords`                    | A    |     3.3 | bisection (first valid: after Stage 6.4)  |
| `_guard_no_line_crosses_non_consumer`                    | A    |    74.7 | bisection (first valid: after Stage 6.14) |
| `_guard_station_x_column_drift`                          | A    |     6.0 | bisection (first valid: start)            |
| `_guard_row_trunk_cy_consistent`                         | B    |    12.1 | final-only                                |
| `_guard_off_track_clear_of_anchor`                       | B    |     3.5 | final-only                                |
| `_guard_fanout_junction_shares_exit_port_y`              | B    |     0.9 | final-only                                |
| `_guard_fanout_junction_resolves_upstream`               | B    |     0.7 | final-only                                |
| `_guard_entry_port_fed_only_by_ports`                    | B    |     0.7 | final-only                                |
| `_guard_flow_exit_anchored_to_carrier`                   | B    |     3.5 | final-only                                |
| `_guard_perp_entry_feed_not_collinear`                   | B    |     0.7 | final-only                                |
| `_guard_merge_port_approach_side`                        | B    |     5.0 | final-only                                |
| `_guard_merge_port_outgoing_side_preserved`              | B    |     5.0 | final-only                                |
| `_guard_exit_inherits_entry_bundle_order`                | B    |     1.9 | final-only                                |
| `_guard_bypass_port_no_slot_gaps`                        | B    |     4.9 | final-only                                |
| `_guard_partial_branch_offset_gaps`                      | B    |     2.1 | final-only                                |
| `_guard_row_gaps`                                        | B    |     0.5 | final-only                                |
| `_guard_section_top_padding`                             | B    |     0.6 | final-only                                |
| `_guard_terminus_icons_within_bbox`                      | B    |     0.4 | final-only                                |
| `_guard_inter_section_routes_in_row_band`                | B    |     4.6 | final-only                                |
| `_guard_topmost_row_top_entry_hugs_section`              | B    |     6.7 | final-only                                |
| `_guard_off_track_output_clears_non_producer`            | B    |     2.4 | final-only                                |
| `_guard_tb_exit_corner_column_order`                     | B    |     2.0 | final-only                                |
| `_guard_no_split_same_line_fanout_descents`              | B    |     2.4 | final-only                                |
| `_guard_no_distinct_line_fanout_crossing`                | B    |     3.4 | final-only                                |
| `_guard_no_dogleg_crosses_exempt_trunk`                  | B    |     1.9 | final-only                                |
| `_guard_no_stacked_elbow_graze`                          | B    |     7.6 | final-only                                |
| `_guard_fanout_tail_join`                                | B    |     2.7 | final-only                                |
| `_guard_perp_entry_boundary_consistent`                  | B    |     7.4 | final-only                                |
| `_guard_perp_exit_over_leadin_no_overdip`                | B    |     2.9 | final-only                                |
| `_guard_right_entry_drop_in_when_clear`                  | B    |     2.5 | final-only                                |
| `_guard_inter_section_route_no_backtrack`                | A    |     4.7 | final-only                                |
| `_guard_inter_section_route_no_full_width_backtrack`     | A    |     5.3 | final-only                                |
| `_guard_routes_enter_sections_at_ports`                  | B    |    61.9 | final-only                                |
| `_guard_no_route_through_section`                        | A    |    85.7 | final-only                                |
| `_guard_inter_section_route_clears_own_section_interior` | A    |    11.3 | final-only                                |
| `_guard_feeder_exits_section_through_side`               | B    |     8.2 | final-only                                |
| `_guard_entry_approach_from_port_side`                   | B    |     5.3 | final-only                                |
| `_guard_no_opposing_line_overlap`                        | B    |    86.1 | final-only                                |
| `_guard_serpentine_no_backtrack`                         | A    |     3.9 | final-only                                |
| `_guard_no_artefactual_counter_flow`                     | B    |     4.6 | final-only                                |
| `_guard_inter_row_run_clearance`                         | B    |     3.6 | final-only                                |
| `_guard_trunk_bands_crossing_optimal`                    | B    |    56.9 | final-only                                |
| `_guard_inter_section_descent_edge_clearance`            | B    |     7.1 | final-only                                |
| `_guard_fan_bundles_coincide_or_separate`                | B    |    14.5 | final-only                                |

### Routing checks (`CHECK_REGISTRY`)

| check                                                      | tier | mean us | runs                                          |
| ---------------------------------------------------------- | ---- | ------: | --------------------------------------------- |
| `check_bundle_order_preserved`                             | A    |    34.9 | render chokepoint (always-on)                 |
| `check_concentric_bundle_corners`                          | A    |    41.1 | render chokepoint (always-on)                 |
| `check_collinear_distinct_lines`                           | A    |   200.0 | render chokepoint (always-on)                 |
| `check_no_same_line_parallel_descents`                     | A    |     5.6 | render chokepoint (always-on)                 |
| `check_merge_branches_meet_trunk`                          | A    |     6.9 | render chokepoint (always-on)                 |
| `check_no_hanging_routes`                                  | A    |   430.0 | render chokepoint (always-on)                 |
| `check_bottom_row_climb_stays_at_row_level`                | A    |     2.9 | render chokepoint (always-on)                 |
| `check_gap_channels_materialized`                          | A    |    22.5 | render chokepoint (always-on)                 |
| `check_trunks_declared`                                    | A    |     1.8 | render chokepoint (always-on)                 |
| `check_peeloff_concentric`                                 | A    |     4.2 | render chokepoint (always-on)                 |
| `check_tb_exit_corner_preserves_column_order`              | B    |     1.5 | via `_guard_*` wrapper                        |
| `check_fanout_tail_join`                                   | B    |     2.3 | via `_guard_*` wrapper                        |
| `check_merge_port_approach_side`                           | B    |     4.6 | via `_guard_*` wrapper                        |
| `check_merge_port_outgoing_side_preserved`                 | B    |     4.6 | via `_guard_*` wrapper                        |
| `check_exit_inherits_entry_bundle_order`                   | B    |     1.6 | via `_guard_*` wrapper                        |
| `check_partial_branch_offset_gaps`                         | B    |     1.8 | via `_guard_*` wrapper                        |
| `check_no_split_same_line_fanout_descents`                 | B    |     2.0 | via `_guard_*` wrapper                        |
| `check_no_distinct_line_fanout_crossing`                   | B    |     2.9 | via `_guard_*` wrapper                        |
| `check_no_dogleg_crosses_exempt_trunk`                     | B    |     1.5 | via `_guard_*` wrapper                        |
| `check_stacked_elbow_clearance`                            | B    |     7.1 | via `_guard_*` wrapper                        |
| `check_perp_entry_boundary_consistent`                     | B    |     6.9 | via `_guard_*` wrapper                        |
| `check_perp_exit_over_leadin_clears_only_spanned_sections` | B    |     2.5 | via `_guard_*` wrapper                        |
| `check_right_entry_drop_in_when_clear`                     | B    |     2.1 | via `_guard_*` wrapper                        |
| `check_seam_approach_equals_departure`                     | C    |       - | test suite only (`tests/test_seam_lane_x.py`) |
| `check_seam_segments_meet_at_port`                         | C    |       - | test suite only (`tests/test_seam_lane_x.py`) |

### Inline guards (`INLINE_GUARD_REGISTRY`)

These `_guard_*` functions are invoked directly at specific pipeline stages
rather than through the Pass C / final dispatch, so they carry no `needs` /
`bisection_safe` dispatch data - only their tier and any `issue_pin` /
`narrow_reason`. Costs are not separately measured; each is a single structural
pass.

| guard                                         | tier | role                                                                        |
| --------------------------------------------- | ---- | --------------------------------------------------------------------------- |
| `_guard_stations_within_bbox`                 | A    | Always-on postcondition: every station centre lies within its section bbox. |
| `_guard_no_negative_grid_columns`             | A    | No section sits at a negative grid column.                                  |
| `_guard_explicit_grid_directions`             | A    | Explicit-grid sections keep the LR default unless they declare a direction. |
| `_guard_no_mixed_entry_directions`            | A    | A section's incoming lines approach from a single side.                     |
| `_guard_independent_components_disjoint`      | A    | Independently-stacked components do not overlap.                            |
| `_guard_no_same_row_backward_feed`            | A    | A same-row inter-section edge does not run against source flow.             |
| `_guard_anchors_frozen_during_placement`      | B    | Content placement leaves resolved anchors fixed.                            |
| `_guard_bypass_v_flat_visible`                | B    | Every bypass V keeps a visible horizontal run through its X.                |
| `_guard_centered_line_spread_balanced`        | B    | A `centered` section's weave balances about its trunk.                      |
| `_guard_file_icon_no_name_label`              | B    | A file-icon station gets no separate node-name label.                       |
| `_guard_interchange_bar_clears_non_members`   | B    | An interchange bar does not cross a non-member station.                     |
| `_guard_no_diagonal_strikes_horizontal_label` | B    | No foreign fan diagonal rakes a stacked station's name.                     |
| `_guard_no_label_overlap`                     | B    | No station label overlaps another label or a marker.                        |
| `_guard_no_line_crosses_file_icon`            | B    | No rendered line passes through a file/terminus icon.                       |
| `_guard_no_line_strikes_label`                | B    | No rendered line strikes through a station label.                           |
| `_guard_no_wrapped_label_trunk_strike`        | B    | No wrapped label overruns a foreign horizontal trunk.                       |
| `_guard_off_track_consumer_on_trunk`          | B    | An off-track input's straight-through consumer stays on trunk (`#650`).     |
| `_guard_off_track_input_column_stack`         | B    | Single-trunk off-track inputs hug their consumer column (`#651`).           |
| `_guard_off_track_not_hub`                    | B    | No off-track station has edges on both sides (`#1295`).                     |
| `_guard_rail_above_label_band`                | B    | A rail section reserves room above its top rail for labels.                 |
| `_guard_rail_one_station_per_column`          | B    | Rails place one distinct station per column.                                |
| `_guard_rail_stations_seat_on_rails`          | B    | Rail stations seat on their lines' fixed rails.                             |
| `_guard_single_trunk_off_track_step`          | B    | Single-trunk sections lift off-track stations by the base pitch (`#580`).   |
| `_guard_tall_anchor_stack_well_formed`        | B    | A tall-anchor vertical stack keeps its downstream chain intact.             |
| `_guard_tb_top_entry_drop_hugs_top`           | B    | A clean TB TOP-entry drop seats its first station at the top.               |

## Consolidation

The consolidation pass (#922) removed the validate-only `_guard_*` wrappers
that only raised around a check already in the always-on render chokepoint -
`_guard_bundle_order_preserved`, `_guard_concentric_bundle_corners`,
`_guard_no_collinear_distinct_lines`,
`_guard_no_intra_section_collinear_distinct_lines`, and
`_guard_no_same_line_parallel_descents`. Each check is the single authority and
runs on every render via `assert_render_curve_invariants`, so the wrapper was
pure duplication; `test_no_registry_guard_duplicates_an_always_on_check` keeps
the duplication from returning.

The remaining issue-pinned guards each express a distinct geometric property
(their docstrings explicitly distinguish them from one another), so rather than
force-merging bodies the pass records each guard's originating issue in
`issue_pin` and documents its scope in `narrow_reason`. The two genuinely
general inline guards (`_guard_no_negative_grid_columns`,
`_guard_explicit_grid_directions`) carry no pin: they state a general structural
property, not a special case.

The collinear-distinct overlay checks are unified as one always-on
`check_collinear_distinct_lines`, whose `scopes` argument selects the
inter-section, intra-section, and diagonal scans; the render chokepoint runs
every scope while callers that need a subset (the strike-clearance probe, the
per-scope unit tests) pass the scopes they want.
