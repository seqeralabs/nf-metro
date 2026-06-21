# Guard tiers

nf-metro defends its layout output with two families of runtime invariant:

- **`_guard_*` functions** in `src/nf_metro/layout/phases/guards.py` — raise
  `PhaseInvariantError` on a violation.
- **`check_*` functions** in `src/nf_metro/layout/routing/invariants.py` —
  return a list of violation objects; a caller decides whether to raise.

Almost all of the `_guard_*` suite is gated behind `compute_layout(validate=True)`,
which the `nf-metro render` path never sets. Only `_guard_stations_within_bbox`
(always-on in `engine.py`) and the routing `check_*` invariants that
`assert_render_curve_invariants` runs on every render reach an end user. A
novel pipeline can therefore ship a silently-broken SVG that only our own
`validate=True` tests would have caught.

This page is the cost-tier classification that the always-on promotion
(#923) and the consolidation pass (#922) build on. It is **descriptive of the
current tree** and **prescriptive of the target tier** — the two coincide for
Tier A (already always-on) and diverge where a Tier-A *candidate* is still
gated.

## Tiers

| Tier | Meaning | Promotion intent |
|---|---|---|
| **A** | Cheap, observational, placement-only structural checks (finite coords, bbox containment with marker overhang, station overlap, ports on boundaries), **plus** the routing `check_*` invariants already always-on via the render chokepoint, **plus** `_guard_stations_within_bbox`. | Run on the default render path (#923), warning by default with a `--strict` escalation. |
| **B** | The remaining `validate=True` set: route-shape, bundle-order, label, rail, and merge-port geometry. Correct but either costlier or dependent on a mid-pipeline reroute (they consume `route_edges` output), so not cheap-always-on. | Stay behind `validate=True` / `--strict`. |
| **C** | Test-only oracles: too slow, too fixture-specific, or non-observational. | Live in the test suite, not the runtime suite. |

Tier A is the cheap-structural set the cost audit confirms runs in single-digit
microseconds and needs no routing pass. Tier B holds everything that either
needs the routed geometry or is materially more expensive; the most expensive
members (`check_no_hanging_routes` ~470 us, the collinear-distinct checks
~100 us) are routed-geometry sweeps. **Tier C is empty on the current tree**:
every guard and check is reachable from `compute_layout` or the render
chokepoint, none is a pure test oracle. It is documented here as the target
bucket for #922 — see [Consolidation candidates](#consolidation-candidates).

## Registries

The classification is data, not prose:

- **`GUARD_REGISTRY`** (`phases/guards.py`) — the single ordered source of
  truth for the `validate=True` guard call sequence. Its order *is* the call
  order; `run_validate_guards` iterates it. Each entry is a `GuardSpec` with
  `tier`, the `needs` set (which of `offsets` / `routes` / `section_y_*` the
  guard takes), `bisection_safe` (runs at every Pass C checkpoint, gated by
  `first_valid_stage`) vs final-only, and the `_BISECTION_FIRST_VALID` data
  derived back out for the engine re-export.
- **`CHECK_REGISTRY`** (`routing/invariants.py`) — the same `GuardSpec` schema
  applied to the routing checks. Because checks return lists rather than
  raising, this is a *classification* registry, not a dispatcher; the runtime
  chokepoint stays `assert_render_curve_invariants`. The two registries are
  unified through the shared schema and this page, **not** by merging the
  raise-vs-return error protocols.

`tests/test_guard_registry.py` keeps both honest: every guard and check must be
registered, and the Tier-A check set must equal the render chokepoint exactly.

## Cost audit

`scripts/guard_cost_audit.py` lays out every fixture under `examples/` +
`examples/topologies/` once, then times each registered guard and check against
the final geometry. Mean microseconds-per-fixture below are from that run;
regenerate with:

```bash
python scripts/guard_cost_audit.py --json /tmp/guard_cost.json
```

### Validate-suite guards (`GUARD_REGISTRY`)

| guard | tier | mean us | dispatch |
|---|---|--:|---|
| `_guard_coordinates_finite` | A | 1.5 | bisection (first valid: start) |
| `_guard_section_bboxes_positive` | A | 0.4 | bisection (first valid: start) |
| `_guard_stations_in_sections` | A | 3.5 | bisection (first valid: after Stage 5.3) |
| `_guard_ports_on_boundaries` | A | 1.3 | bisection (first valid: start) |
| `_guard_no_station_overlap` | A | 14.0 | bisection (first valid: after Stage 6.4) |
| `_guard_no_coincident_station_coords` | A | 3.3 | bisection (first valid: after Stage 6.4) |
| `_guard_no_line_crosses_non_consumer` | B | 78.7 | bisection (first valid: after Stage 6.14) |
| `_guard_station_x_column_drift` | A | 6.2 | bisection (first valid: start) |
| `_guard_row_trunk_cy_consistent` | B | 12.4 | final-only |
| `_guard_off_track_clear_of_anchor` | B | 3.6 | final-only |
| `_guard_fanout_junction_shares_exit_port_y` | B | 0.9 | final-only |
| `_guard_fanout_junction_resolves_upstream` | B | 0.7 | final-only |
| `_guard_entry_port_fed_only_by_ports` | B | 0.7 | final-only |
| `_guard_flow_exit_anchored_to_carrier` | B | 3.6 | final-only |
| `_guard_perp_entry_feed_not_collinear` | B | 0.7 | final-only |
| `_guard_merge_port_approach_side` | B | 5.1 | final-only |
| `_guard_merge_port_outgoing_side_preserved` | B | 5.2 | final-only |
| `_guard_exit_inherits_entry_bundle_order` | B | 2.0 | final-only |
| `_guard_bypass_port_no_slot_gaps` | B | 5.0 | final-only |
| `_guard_partial_branch_offset_gaps` | B | 2.2 | final-only |
| `_guard_row_gaps` | B | 0.5 | final-only |
| `_guard_section_top_padding` | B | 0.6 | final-only |
| `_guard_terminus_icons_within_bbox` | B | 0.4 | final-only |
| `_guard_inter_section_routes_in_row_band` | B | 4.8 | final-only |
| `_guard_topmost_row_top_entry_hugs_section` | B | 6.8 | final-only |
| `_guard_off_track_output_clears_non_producer` | B | 2.5 | final-only |
| `_guard_bundle_order_preserved` | B | 35.9 | final-only |
| `_guard_tb_exit_corner_column_order` | B | 2.0 | final-only |
| `_guard_concentric_bundle_corners` | B | 42.5 | final-only |
| `_guard_no_collinear_distinct_lines` | B | 17.2 | final-only |
| `_guard_no_intra_section_collinear_distinct_lines` | B | 108.0 | final-only |
| `_guard_no_same_line_parallel_descents` | B | 6.2 | final-only |
| `_guard_no_split_same_line_fanout_descents` | B | 2.4 | final-only |
| `_guard_no_dogleg_crosses_exempt_trunk` | B | 1.9 | final-only |
| `_guard_no_stacked_elbow_graze` | B | 7.6 | final-only |
| `_guard_fanout_tail_join` | B | 2.7 | final-only |
| `_guard_perp_entry_boundary_consistent` | B | 7.4 | final-only |
| `_guard_perp_exit_over_leadin_no_overdip` | B | 2.9 | final-only |
| `_guard_right_entry_drop_in_when_clear` | B | 2.5 | final-only |
| `_guard_inter_section_route_no_backtrack` | B | 5.3 | final-only |
| `_guard_inter_section_route_no_full_width_backtrack` | B | 6.0 | final-only |
| `_guard_routes_enter_sections_at_ports` | B | 63.3 | final-only |
| `_guard_no_route_through_section` | B | 94.7 | final-only |
| `_guard_feeder_exits_section_through_side` | B | 8.5 | final-only |
| `_guard_entry_approach_from_port_side` | B | 5.4 | final-only |
| `_guard_no_opposing_line_overlap` | B | 86.8 | final-only |
| `_guard_serpentine_no_backtrack` | B | 4.1 | final-only |
| `_guard_no_artefactual_counter_flow` | B | 4.7 | final-only |
| `_guard_inter_row_run_clearance` | B | 3.6 | final-only |
| `_guard_trunk_bands_crossing_optimal` | B | 56.3 | final-only |
| `_guard_inter_section_descent_edge_clearance` | B | 7.5 | final-only |
| `_guard_fan_bundles_coincide_or_separate` | B | 13.2 | final-only |

### Routing checks (`CHECK_REGISTRY`)

| check | tier | mean us | runs |
|---|---|--:|---|
| `check_bundle_order_preserved` | A | 35.5 | render chokepoint (always-on) |
| `check_concentric_bundle_corners` | A | 42.2 | render chokepoint (always-on) |
| `check_no_collinear_distinct_lines` | A | 16.9 | render chokepoint (always-on) |
| `check_intra_section_collinear_distinct_lines` | A | 113.4 | render chokepoint (always-on) |
| `check_no_collinear_distinct_diagonals` | A | 89.5 | render chokepoint (always-on) |
| `check_no_same_line_parallel_descents` | A | 5.7 | render chokepoint (always-on) |
| `check_merge_branches_meet_trunk` | A | 7.1 | render chokepoint (always-on) |
| `check_no_hanging_routes` | A | 468.7 | render chokepoint (always-on) |
| `check_bottom_row_climb_stays_at_row_level` | A | 3.0 | render chokepoint (always-on) |
| `check_gap_channels_materialized` | A | 22.6 | render chokepoint (always-on) |
| `check_trunks_declared` | A | 1.8 | render chokepoint (always-on) |
| `check_peeloff_concentric` | A | 4.3 | render chokepoint (always-on) |
| `check_tb_exit_corner_preserves_column_order` | B | 1.5 | via `_guard_*` wrapper |
| `check_fanout_tail_join` | B | 2.4 | via `_guard_*` wrapper |
| `check_merge_port_approach_side` | B | 4.6 | via `_guard_*` wrapper |
| `check_merge_port_outgoing_side_preserved` | B | 4.7 | via `_guard_*` wrapper |
| `check_exit_inherits_entry_bundle_order` | B | 1.6 | via `_guard_*` wrapper |
| `check_partial_branch_offset_gaps` | B | 1.8 | via `_guard_*` wrapper |
| `check_no_split_same_line_fanout_descents` | B | 2.0 | via `_guard_*` wrapper |
| `check_no_dogleg_crosses_exempt_trunk` | B | 1.5 | via `_guard_*` wrapper |
| `check_stacked_elbow_clearance` | B | 7.2 | via `_guard_*` wrapper |
| `check_perp_entry_boundary_consistent` | B | 7.1 | via `_guard_*` wrapper |
| `check_perp_exit_over_leadin_clears_only_spanned_sections` | B | 2.6 | via `_guard_*` wrapper |
| `check_right_entry_drop_in_when_clear` | B | 2.2 | via `_guard_*` wrapper |

### Inline guards (not in `GUARD_REGISTRY`)

These `_guard_*` functions are invoked directly at specific pipeline stages
rather than through the Pass C / final dispatch, so they are classified here
but not dispatched from `GUARD_REGISTRY`. Costs are not separately measured;
each is a single structural pass.

| guard | tier | role |
|---|---|---|
| `_guard_stations_within_bbox` | A | Always-on postcondition: every station centre lies within its section bbox. |
| `_guard_no_negative_grid_columns` | A | No section sits at a negative grid column. |
| `_guard_explicit_grid_directions` | A | Explicit-grid sections keep the LR default unless they declare a direction. |
| `_guard_no_mixed_entry_directions` | A | A section's incoming lines approach from a single side. |
| `_guard_independent_components_disjoint` | A | Independently-stacked components do not overlap. |
| `_guard_no_same_row_backward_feed` | A | A same-row inter-section edge does not run against source flow. |
| `_guard_anchors_frozen_during_placement` | B | Content placement leaves resolved anchors fixed. |
| `_guard_bypass_v_flat_visible` | B | Every bypass V keeps a visible horizontal run through its X. |
| `_guard_centered_line_spread_balanced` | B | A `centered` section's weave balances about its trunk. |
| `_guard_file_icon_no_name_label` | B | A file-icon station gets no separate node-name label. |
| `_guard_interchange_bar_clears_non_members` | B | An interchange bar does not cross a non-member station. |
| `_guard_no_diagonal_strikes_horizontal_label` | B | No foreign fan diagonal rakes a stacked station's name. |
| `_guard_no_label_overlap` | B | No station label overlaps another label or a marker. |
| `_guard_no_line_crosses_file_icon` | B | No rendered line passes through a file/terminus icon. |
| `_guard_no_line_strikes_label` | B | No rendered line strikes through a station label. |
| `_guard_no_wrapped_label_trunk_strike` | B | No wrapped label overruns a foreign horizontal trunk. |
| `_guard_off_track_consumer_on_trunk` | B | An off-track input's straight-through consumer stays on trunk. |
| `_guard_off_track_input_column_stack` | B | Single-trunk off-track inputs hug their consumer column. |
| `_guard_rail_above_label_band` | B | A rail section reserves room above its top rail for labels. |
| `_guard_rail_one_station_per_column` | B | Rails place one distinct station per column. |
| `_guard_rail_stations_seat_on_rails` | B | Rail stations seat on their lines' fixed rails. |
| `_guard_single_trunk_off_track_step` | B | Single-trunk sections lift off-track stations by the base pitch. |
| `_guard_tall_anchor_stack_well_formed` | B | A tall-anchor vertical stack keeps its downstream chain intact. |
| `_guard_tb_top_entry_drop_hugs_top` | B | A clean TB TOP-entry drop seats its first station at the top. |

## Consolidation candidates

For #922, the clearest Tier-C / de-duplication targets are the `_guard_*`
wrappers that exist only to raise around a `CHECK_REGISTRY` check —
`_guard_bundle_order_preserved`/`check_bundle_order_preserved`,
`_guard_concentric_bundle_corners`/`check_concentric_bundle_corners`, the
collinear-distinct pair, and the merge-port pair. The check is the oracle; the
guard is a thin raising adapter. The collinear-distinct family
(`check_no_collinear_distinct_lines`, `check_intra_section_collinear_distinct_lines`,
`check_no_collinear_distinct_diagonals`) is also a candidate to fold into one
parametrised check.
