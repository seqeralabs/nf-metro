Continue work on the `feat/shared-y-grid-alignment` branch of nf-metro. This branch adds shared Y grid alignment for stations across sections in the same grid row.

## What's been done (committed)

### Earlier commits (pre-existing)

1. `_align_row_y_grids` in `engine.py` (Phase 2.5, between Phase 2 and Phase 3) snaps station Y positions to a shared grid at `y_spacing` pitch across same-direction sections in each grid row
2. `_align_uncentered_siblings` in `routing/core.py` fixed to handle bubble-centering outliers - when moved stations disagree on target X, finds the majority position and drags outliers to match
3. Debug overlay renders shared Y grid lines (green dashed horizontals)
4. CI workflow passes `--debug` to `build_gallery.py` for PR renders
5. `build_gallery.py` accepts `--debug` flag, all render paths (including Nextflow auto-converted renders) respect `DEBUG_RENDERS`
6. Debug overlay handles pipelines where all sections are row-spanning (genomeassembly) by falling back to raw section bboxes
7. Floor-based grid slot assignment (`floor(old_y / y_spacing)`) preserves 2+ slot gaps
8. Isolated station grid snap for small fan-outs (<=3 per layer)
9. Diamond detection: enforces minimum 2-slot gap when fork/join hub sits between tracks
10. y_pad compensation shift for cross-section first_station_y alignment
11. Bbox recomputation in Phase 2.5 using `max_y_pad` for symmetric padding
12. Phase 11b `_recompute_grid_group_bboxes` resets bboxes to symmetric padding after port finalisation

### Commit a4954d2

**Uniform spacing preservation** (Issue B fix): When all input gaps between remap Y values are equal (e.g. [0, 68.8, 137.6] with gap 68.8), the slot assignment maps to equally-spaced grid slots (`round(gap/y_spacing)` slots apart) instead of floor-based per-value assignment. This prevents asymmetric compression. Fixed variantbenchmarking output_processing: gaps went from [40, 80] to [80, 80].

**Skip Phase 10b for grid-group sections** (Issue A fix): `_snap_sole_layer_stations_to_ports` now skips sections in `_row_y_grid_info`, so grid-aligned stations are not pulled off-grid by port alignment. Fixed cpsr in variantprioritization: stays at y=320 (on-grid, aligned with report_cpsr and get_pcgr) instead of being dragged to y=340.

**Phase 11b bare port containment**: `_recompute_grid_group_bboxes` expands for ports using bare bounds (no extra `SECTION_Y_PADDING`) to avoid asymmetric bbox inflation. Fixed run_cpsr bbox.

**Debug grid line range**: Computed from actual station Y range instead of stored `slot_count`.

### Issue C (rnaseq fan-out compression) - resolved as non-issue

The branch actually improves rnaseq_sections preprocessing: collapses a 4.1px micro-gap (164.1 -> 160.0) into clean grid alignment. Gaps went from [40, 4.1, 40] to [40, 40]. No label clashes introduced.

### Commit 3f16818

**Issue D fix**: Changed `round()` to `floor()` in uniform-gap slot assignment. Prevents inflation (68.8px gap stays at 1 slot, not 2).

**Issue E partial fix**: Added `effective_y_spacing` per grid group based on `max_lines` at any station. When per-line offsets + label height exceed base y_spacing, grid pitch is inflated (e.g. 50px for 6-line stations). **Introduced regressions: see Issues G and I.**

**Issue F partial fix**: New Phase 10c (`_snap_grid_group_entry_ports`) moves entry ports of grid-group sections to connected station Y for straight horizontal entry. **Upstream exit still at midpoint: see Issue J.**

### Uncommitted changes (in working tree)

The following fixes have been applied but NOT committed. They are in the working tree on top of commit 3f16818. Some have residual bugs that need fixing (see Outstanding Issues below).

#### Issue G partial fix (engine.py)
The `max_lines` computation for `effective_y_spacing` now classifies multi-station-layer Y values per-section and only counts stations at those Y values. Isolated hub stations (like `bench_hub` with 6 lines, sole layer occupant) no longer inflate spacing. Reduced variantbenchmarking row 1 from 50px to 47px. **This part is working correctly.**

#### Issue I partial fix (engine.py)
Added same-layer-pair detection in the non-uniform floor-based slot path. When two successive `remap_ys` share a layer and `floor()` maps them to the same slot, forces `prev_slot + 1`. This fixes the fastp/trimgalore collapse in rnaseq_sections_manual. **However, the same-layer-pair logic is too aggressive - see Issue M (sortmerna/ribodetector collapse).**

#### Issue J partial fix (engine.py)
Added Phase 10d (`_snap_grid_group_exit_ports`) and helper `_resolve_downstream_entry_y`. Moves exit ports of grid-group sections to their connected internal station Y. Added guard: if exit port already matches downstream entry Y, skip the snap. **The guard now handles direct exit->entry connections (not just junction-mediated ones), which fixes Issues K and L. However, Issue J itself needs re-verification after fixing Issue M.**

#### Issue H partial fix (labels.py)
Added final obstacle clearance in `place_labels`: after the push fallback, if the label still collides with an icon obstacle, pushes it just past the obstacle edge. **This moves the MultiQC Report label clear of the HTML icon, but pushes it too far - into the grid row below. See Issue N.**

## Verification checks that MUST continue to pass

These were validated in the previous session and must remain passing after any further changes:

1. All non-port station Y positions are on the grid (within 1px tolerance) for all grid-group sections. bench_hub is exempt (large fan-out hub, constraint 1).
2. Section 6 (benchmarking) in variantbenchmarking: all layer-1 stations have the same X after bubble centering.
3. No section bbox in a grid group has excess asymmetric padding (top and bottom within 5px of each other).
4. Diamond bypass edges are present where expected (e.g. `reformat_vcf` -> `prepare_pcgr` in variantprioritization).
5. Parallel tracks (CNA vs somatic/germline) are on separate grid lines.
6. First station Y is consistent across all sections in each row group.
7. In variantprioritization: cpsr and report_cpsr are at the same Y (straight horizontal germline line). get_pcgr is also at the same Y for straight inter-section connection.
8. In variantbenchmarking output_processing: track gaps are uniform.
9. All 596 tests pass, ruff check + format clean.
10. In variantprioritization: section 5 entry port is at the same Y as cpsr (straight horizontal entry).
11. In variantprioritization: section 3 exit port is at get_pcgr's Y (y=320), not the midpoint (y=340). The inter-section germline line from section 3 to section 5 is straight horizontal.
12. In variantbenchmarking: liftover is 1 grid row (not 2) below subsample.
13. In rnaseq_sections_manual: fastp and trimgalore are at DIFFERENT Y values (not collapsed).
14. No backward merge routing segments in genomeassembly.

## Resolved issues

### Issue D: variantbenchmarking liftover/subsample spacing (RESOLVED in 3f16818)
### Issue E: rnaseq_sections_manual fan-out label crowding (PARTIALLY RESOLVED in 3f16818)
### Issue F: variantprioritization section 5 entry port (PARTIALLY RESOLVED in 3f16818)

## Outstanding issues to fix

### Issue K: variantbenchmarking section 3 (normalization) routing mess (CRITICAL)

**What's happening**: Lines entering Variant Normalization (section 3) from preprocessing double back on themselves. The `preprocess__exit_right_1` route goes RIGHT past the normalization entry, then DOWN, then LEFT back to the entry port - a visible backward detour.

**Root cause**: Phase 10d (`_snap_grid_group_exit_ports`) moved `preprocess__exit_right_1` from y=120 to y=160 (matching `liftover` at y=160). But the downstream `normalization__entry_left_7` is at y=120, so the connection that was previously a straight horizontal line becomes a backwards L-shape.

**Current state of fix**: A guard was added: "if the exit port already matches the downstream entry Y, skip the snap." The helper `_resolve_downstream_entry_y` was updated to handle direct exit->entry connections (not just junction-mediated ones). **This fix is in the working tree but needs verification** - the last test showed the exit was still at y=160, likely because the fix was applied after the test. Re-run and verify.

**Desired state**: `preprocess__exit_right_1` stays at y=120 (matching the downstream entry). Lines from liftover (y=160) descend within the section to reach the exit port. The inter-section connection is a straight horizontal line.

**Verification**: `preprocess__exit_right_1.y == normalization__entry_left_7.y == 120.0`. The route from `preprocess__exit_right_1 -> normalization__entry_left_7` should be a simple 2-point horizontal segment `[(687, 120), (737, 120)]`.

### Issue L: variant_calling_tuned section 2 (alignment) exit port and bypass routing

**What's happening**: Same pattern as Issue K. Phase 10d drags `alignment__exit_right_1` from y=120 down to y=160 (matching `samtools_sort`/`samtools_index`). This removes the bypass routing around `samtools_index` that exists on main, and creates an awkward upward kink to reach the y=120 entry port of `variant_calling`.

**Root cause**: Same as Issue K - Phase 10d snaps to source station Y without checking if the port already connects straight to the downstream entry.

**Current state of fix**: Same guard as Issue K should fix this. Needs verification.

**Desired state**: `alignment__exit_right_1` stays at y=120. The bypass around `samtools_index` routes correctly through the top exit port. The inter-section connection to `variant_calling__entry_left_4` is a straight horizontal line at y=120.

**Verification**: `alignment__exit_right_1.y == 120.0`. Compare the render visually with main - the bypass around `samtools_index` should be preserved.

### Issue M: rnaseq_sections_manual bbsplit/sortmerna/ribodetector 3-way collapse to 2-way

**What's happening**: On main, bbsplit and sortmerna share y=120, ribodetector is at y=170 (a 2-group visual split). On the branch, sortmerna moved to y=170, collapsing with ribodetector. All three are at layer 7 with tracks 0.0, 1.2, 2.4 respectively.

**Root cause**: The Issue I same-layer-pair collision logic forces slot advancement when two `remap_ys` share a layer. bbsplit (y=0 pre-remap), sortmerna (y≈50), and ribodetector (y≈100) are all at layer 7. The `same_layer_pairs` set includes (0, 50) and (50, 100), so sortmerna is forced to `prev_slot + 1` even though on main it shared a slot with bbsplit.

The problem is that the same-layer-pair approach is too coarse: it forces distinct slots for ANY two values that co-occur in any layer, even when the original layout intentionally grouped them at the same Y (via per-line offsets applied later). The Issue I fix was needed to prevent fastp/trimgalore (at y=0 and y=40 with effective_y_spacing=50) from collapsing to the same slot, but it over-corrects for 3+ station layers.

**Investigation needed**: 
1. Check the pre-remap Y values for bbsplit, sortmerna, ribodetector in the preprocessing section. Understand the original spacing.
2. The key distinction: fastp and trimgalore (Issue I) had a 40px gap that floor() collapsed with effective_y_spacing=50. bbsplit and sortmerna have a gap that should allow sharing a slot because they get separated by per-line offsets later.
3. Consider whether the fix should be based on the gap between values relative to y_spacing rather than same-layer co-occurrence. E.g., only force advancement when `gap > y_spacing * 0.8` (values that were on separate tracks before grid alignment).
4. Or: only force advancement when the two values would truly overlap visually (considering per-line offsets). Values within `y_spacing/2` of each other can share a slot if they have enough per-line offset separation.

**Desired state**: bbsplit=120, sortmerna=120, ribodetector=170 (matching main). The 3-way fan-out should show bbsplit and sortmerna at the same Y with ribodetector below.

### Issue N: variantbenchmarking "MultiQC Report" label pushed too far

**What's happening**: The Issue H fix (final obstacle clearance in `place_labels`) pushes the "MultiQC Report" label below the HTML file icon obstacle. But it pushes it too far - the label appears in the grid row below its station, disconnected from the station it belongs to.

**Root cause**: The obstacle clearance code moves the label to `obstacle_bottom + LABEL_MARGIN`, which can place it far from its station. The label at `multiqc` (y=480.6) gets pushed below the `html_report_out` icon (y=[507.6, 547.6]) to approximately y=550. This is 70px below the station - too far.

**Investigation needed**:
1. Check what the label placement looks like on main for `multiqc`. It likely places the label above the station instead.
2. The real fix might be to improve the initial placement strategy (try above first when below would hit an icon) rather than relying on a post-hoc push.
3. Check whether `_compute_safe_offsets` properly accounts for icon obstacles when computing safe_above/safe_below. If it did, the label placer would naturally prefer "above" for `multiqc`.
4. Consider whether `_compute_safe_offsets` should incorporate icon obstacle bboxes as virtual neighbors, reducing the safe offset in the direction of an icon.

**Desired state**: The "MultiQC Report" label should be clearly associated with its station pill. Preferably placed above the station (at y≈469 instead of below at y≈492+), avoiding both the icon below and the "Merged CSVs" label above.

### Issue G: variantbenchmarking section 6 (Benchmarking) spacing

**Current state**: effective_y_spacing reduced from 50px to 47px (driven by `results_hub` with 5 lines in output_processing). The prompt originally wanted 40px, but 47px is justified because `results_hub` IS at a multi-station-layer Y (not isolated). **This may be acceptable** - verify visually that the 47px spacing doesn't cause problems.

## Key constraints learned the hard way

1. **bench_hub must not be remapped** to a grid slot - it's a blank visible station at the center of a 9-station fan-out. Remapping collapses exit port onto bndeval's Y, eliminating bubble centering for all section 6 stations.

2. **Diamond join points must stay between tracks** - snapping to a mapped slot collapses the diamond visual. Snapping to the nearest grid slot via `round()` correctly places them at the midpoint slot.

3. **y_pad shift is necessary** for cross-section first_station_y alignment, but the bbox must be recomputed with `max_y_pad` to maintain symmetric padding.

4. **bbox recomputation is necessary** because diamond 2-slot gaps expand the station range beyond Phase 2's original bbox.

5. **All stations must be on grid lines** (subject to per-line offsets applied later by routing). The only exception is bench_hub in large fan-outs (constraint 1).

6. **Lines leaving a fork should diverge by at least 1 full grid unit or not at all.** Sub-grid-unit divergences create visual noise.

7. **Parallel tracks must remain visually distinct.** The CNA track in variantprioritization section 2 must stay on a separate grid line from the somatic/germline track.

8. **Phase 11b bbox recomputation is essential** - without it, temporary port positions permanently inflate bboxes.

9. **Phase 10b can override grid alignment** - now prevented by skipping grid-group sections in Phase 10b.

10. **Uniform input spacing should produce uniform output spacing** - asymmetric floor-based mapping creates label clashes. Detected and preserved via the uniform-gap path in `_align_row_y_grids`.

11. **effective_y_spacing must not collapse distinct tracks** - when effective_y_spacing > y_spacing, floor-based slot assignment can merge previously distinct Y values into the same slot. The non-uniform path must prevent this for values that are on the same layer.

12. **Hub stations should not inflate effective_y_spacing** - isolated hub stations (sole occupant of their layer, e.g. bench_hub with 6 lines) don't represent inter-track crowding. Only count stations in multi-station layers when computing max_lines for effective_y_spacing.

13. **Exit port snap must not break existing straight connections** - Phase 10d must check if the port already aligns with the downstream entry port before moving it. Moving an exit away from a matching entry creates backward routing detours.

14. **Same-layer slot enforcement must not collapse 3+ station groups** - forcing `prev_slot + 1` for every same-layer pair over-corrects when 3+ stations share a layer but should map to fewer distinct grid slots (with per-line offsets providing the visual separation).

15. **Label obstacle clearance must not push labels too far from their station** - a label pushed past an icon obstacle should still be visually associated with its station. Consider flipping to the other side (above/below) before pushing past an obstacle.

## Iteration process

For each change:
1. Make the code change
2. Run `pytest` - all 596 tests must pass
3. Run `ruff check src/ tests/` and `ruff format src/ tests/` - must be clean
4. Render each of these examples with `--debug` and visually inspect:
   - `variantbenchmarking.mmd`
   - `variantprioritization.mmd`
   - `variant_calling.mmd`
   - `hlatyping.mmd`
   - `genomeassembly.mmd`
   - `rnaseq_sections.mmd`
   - `rnaseq_sections_manual.mmd`
   - `variant_calling_tuned.mmd` (important for Issue L)
5. For each render, programmatically verify:
   - All non-port station Y positions are on the grid (i.e. `(station.y - first_station_y) % y_spacing < 1` within each row group) - bench_hub exempt
   - Section 6 (benchmarking) in variantbenchmarking: all layer-1 stations have the same X after bubble centering
   - No section bbox has excess asymmetric padding (top and bottom padding should be within ~5px of each other)
   - Diamond bypass edges are present where expected
   - Parallel tracks (e.g. CNA vs somatic/germline) are on separate grid lines
   - First station Y is consistent across all sections in each row group
   - In variantprioritization: cpsr and report_cpsr are at the same Y (straight horizontal line)
   - In variantbenchmarking output_processing: track gaps uniform
   - In variantbenchmarking: liftover is 1 grid row (not 2) below subsample; sv_processing and sv_norm gaps are reasonable
   - In variantbenchmarking: section 6 fan-out tracks use reasonable spacing (47px or 40px), NOT the old inflated 50px
   - In rnaseq_sections_manual: fastp and trimgalore are at DIFFERENT Y values (not collapsed)
   - In rnaseq_sections_manual: bbsplit and sortmerna share the same Y, ribodetector is on a separate Y below (3-way fan-out matches main)
   - In rnaseq_sections_manual: fan-out labels don't overlap with unrelated station pills
   - In variantprioritization: section 5 entry port is at the same Y as cpsr (straight horizontal entry)
   - In variantprioritization: section 3 exit port is at get_pcgr's Y (y=320), not the midpoint (y=340)
   - In variantprioritization: the inter-section line from section 3 to section 5 is straight horizontal (exit port, junction, entry port all at same Y)
   - In variantbenchmarking: preprocess exit port at y=120 (same as normalization entry), NOT y=160. Route from preprocess exit to normalization entry is a 2-point horizontal segment.
   - In variant_calling_tuned: alignment exit port at y=120 (same as variant_calling entry). Bypass around samtools_index routes through the top of the section.
   - In variantbenchmarking output_processing: "MultiQC Report" label is visually close to its station (not pushed into the row below). No overlap with file icons.
   - In variantbenchmarking output_processing: "HTML Report" label does not overlap with file icons on adjacent tracks
   - No backward merge routing segments in genomeassembly
6. If any check fails, diagnose and fix before proceeding

## Termination criteria

The work is done when ALL of the following are true:
- All verification checks from step 5 pass for all 8 example files
- Issues K, L, M, and N are resolved
- All existing success criteria (section "Verification checks that MUST continue to pass") still hold
- All tests pass and linting is clean
- Changes are committed (separate logical commits) and pushed
- A local HTML diff page is generated at `/tmp/nf_metro_grid_review/index.html` comparing main vs branch renders (same format as the CI PR preview), and opened for the user to review
- A summary of what changed is provided for user review

To generate the local diff page:
```bash
# Render branch
python scripts/build_gallery.py --debug
mkdir -p /tmp/nf_metro_grid_review/pr_renders
cp docs/assets/renders/*.svg docs/assets/renders/manifest.json /tmp/nf_metro_grid_review/pr_renders/

# Render main (careful git dance - reset index after checkout)
rm -f docs/assets/renders/*.svg
git stash
git checkout main -- src/ scripts/
pip install -e . 2>/dev/null
python scripts/build_gallery.py --debug 2>/dev/null || python scripts/build_gallery.py
mkdir -p /tmp/nf_metro_grid_review/base_renders
cp docs/assets/renders/*.svg /tmp/nf_metro_grid_review/base_renders/
cp docs/assets/renders/manifest.json /tmp/nf_metro_grid_review/base_renders/ 2>/dev/null || true
git reset HEAD -- . 2>/dev/null
git checkout -- .
git stash pop

# Generate diff
python scripts/build_render_diff.py \
  /tmp/nf_metro_grid_review/base_renders \
  /tmp/nf_metro_grid_review/pr_renders \
  /tmp/nf_metro_grid_review
open /tmp/nf_metro_grid_review/index.html
```

**IMPORTANT**: The `git stash pop` step can cause merge conflicts because `git checkout main -- src/` stages main-branch code in the index. After copying base renders, always do `git reset HEAD -- .` then `git checkout -- .` before `git stash pop` to cleanly restore the index.

Do NOT commit until all termination criteria are met. Present the renders and verification results for approval before committing.

## Filed issues

- pinin4fjords/nf-metro#223: sub-grid-unit line divergences at fork hubs (Phase 10b overrides, e.g. `filter_contigs` in variantbenchmarking section 4, `run_cpsr` stations in variantprioritization). The cpsr case is now fixed (Phase 10b skipped for grid groups).
