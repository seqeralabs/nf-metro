# Topology Stress Tests

Test fixtures and infrastructure for stress-testing the auto-layout engine against diverse pipeline topologies.

## Status

- **179 tests pass** (66 original + 113 topology)
- **rnaseq regression**: confirmed unbroken
- **2 routing bugs fixed** in `src/nf_metro/layout/routing.py`
- **Visual review**: pending final rating on all renders

## Routing Fixes Applied

### 1. Fan-in bundle ordering (`_compute_bundle_info`)

**Bug**: When multiple source sections converge on one target (fan-in), `_compute_bundle_info()` assumed all edges in a corridor shared the same source exit port. It called `_line_source_y_at_port()` on only the first port, returning Y=0 for lines from other ports, scrambling the sort order. Lines visibly crossed when turning corners.

**Fix**: Detect when corridor edges come from different source ports (`len(source_ids) > 1`). In that case, sort by actual source station Y coordinate (`e[2]` in the tuple) instead of looking up from a single port.

**Visible in**: `wide_fan_in` - bundle order now preserved around the L-shaped corner.

### 2. L-shape vertical channel placement (`_inter_column_channel_x`)

**Bug**: L-shaped inter-section routes always placed the vertical channel near the source (`sx + max_r + offset_step`). When sibling sections in the same column were wider than the source section, the channel passed through them visually.

**Fix**: New helper `_inter_column_channel_x()` computes the gap between columns by finding the rightmost edge of all sections in the source column and the leftmost edge of all sections in the target column, then centers the channel in that gap. Falls back to near-source placement when section info is unavailable.

**Visible in**: `section_diamond` - right_path now routes through the gap between Branch Left and Finish instead of through Branch Left.

## Fixtures (15 .mmd files)

| File | Sections | Lines | What it stresses |
|------|----------|-------|-----------------|
| `wide_fan_out.mmd` | 5 | 4 | 1 source -> 4 targets, junction creation, vertical stacking |
| `wide_fan_in.mmd` | 5 | 4 | 4 sources -> 1 sink, bundle ordering around L-shaped corners |
| `deep_linear.mmd` | 7 | 2 | 7 sequential sections, fold threshold stress |
| `parallel_independent.mmd` | 4 | 2 | 2 disconnected 2-section chains, row stacking |
| `section_diamond.mmd` | 4 | 2 | Section-level fork-join (A -> {B,C} -> D) |
| `complex_multipath.mmd` | 6 | 4 | 4 lines taking different routes through different sections |
| `single_section.mmd` | 1 | 1 | Edge case: no ports, no junctions, no grid |
| `asymmetric_tree.mmd` | 7 | 3 | Root -> 3 branches of depth 1/2/3 |
| `mixed_port_sides.mmd` | 3 | 2 | RIGHT + BOTTOM exits from same section |
| `multi_line_bundle.mmd` | 3 | 6 | 6 lines through same 3-section chain, tall pills |
| `rnaseq_lite.mmd` | 5 | 3 | Simplified rnaseq: bottom exit, TB/RL sections, diamond |
| `variant_calling.mmd` | 6 | 4 | DNA/RNA split, fork-join, 4 callers fan into merge |
| `fold_fan_across.mmd` | 7 | 3 | Fan-out/fan-in across fold boundary, rowspan optimization |
| `fold_double.mmd` | 10 | 2 | Double fold serpentine (LR -> RL -> LR), col_step zigzag |
| `fold_stacked_branch.mmd` | 8 | 3 | Stacked sections near fold, post-fold branching, TB fan-out |
| `upward_bypass.mmd` | 4 | 7 | Tall section bypass where trunk is above source (upward gap1) |

All use auto-layout (no `%%metro grid:` directives). These are intended to eventually move to `examples/` once visually polished.

## Test Infrastructure

- **`tests/layout_validator.py`** - Programmatic layout checks:
  - Section overlap (AABB with tolerance)
  - Station containment within section bbox
  - Port boundary positioning (on correct section edge)
  - Coordinate sanity (no NaN/Inf/extreme values)
  - Minimum section spacing (>= 5px gap)
  - Edge waypoints (>= 2 valid points per route, no NaN)
  - Edge-section crossing (no routed segment passes through a non-home section bbox)

- **`tests/test_topology_validation.py`** - Parametrized pytest module:
  - `TestTopologyValidation` - runs all validator checks against every fixture
  - `TestRnaseqRegression` - validates the real rnaseq example
  - `TestTopologySpecific` - targeted assertions per topology (section counts, grid structure, junction creation, etc.)

- **`scripts/render_topologies.py`** - Batch renders all fixtures + rnaseq to SVG/PNG in `/tmp/nf_metro_topology_renders/`

## Known Visual Issues to Address

- **variant_calling**: The RNA Variants (red) line has a long-range route from Alignment to RNA Variant Calling that curves dramatically. Works but could be tighter.
- **deep_linear**: Section 7 (Report) is laid out differently (narrower, stations stacked) because the fold threshold wasn't triggered but it's the last section. Works but worth reviewing if this is the desired behavior.

## Render Command

```bash
source ~/.local/bin/mm-activate nf-metro
python scripts/render_topologies.py
open /tmp/nf_metro_topology_renders/*.png
```
