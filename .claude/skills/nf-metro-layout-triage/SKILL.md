---
name: nf-metro-layout-triage
description: Build a self-contained HTML triage page for failing/xfailing layout-invariant tests in nf-metro (test_label_x_matches_segment_midpoint_on_horizontal_runs, test_stack_station_xs_share_column, test_row_trunk_marker_cy_consistent, test_no_kink_at_section_boundary, test_symfan_pairs_share_y, test_lines_dont_cross_non_consumer_markers, test_topological_siblings_share_y_or_symmetric, test_section_bbox_has_bottom_padding, test_off_track_inputs_above_consumer). Each row pairs the rendered fixture SVG with a red-bbox overlay on the offending element, a plain-English "Supposed issue" + "What to check" explanation, and bug / not-a-bug / ambiguous triage buttons whose state is saved in localStorage and exported as JSON. Use when the user asks to "triage layout invariants", "review xfails", "review failing layout tests", "build the triage page", or generally wants to triage the layout-invariant test suite by eye.
when-to-use: The user wants to walk through every failing or xfailing case in `tests/test_layout_invariants.py` and classify each one as bug / not-a-bug / ambiguous - typically before deciding which invariants to fix in the engine vs. which to relax in the test. Trigger phrases include "triage layout invariants", "review xfails", "review failing layout tests", "triage tool", "build the triage page", or any mention of triaging the nine invariants listed in the description.
---

# nf-metro layout-invariant triage

This skill packages the triage tool that produces a single self-contained HTML page (with embedded SVGs, red-bbox overlays, explanations, and localStorage triage state) for every failing or xfailing case in `tests/test_layout_invariants.py`. It grew out of the xfail-review session for PR #326.

## What it covers

The tool produces one card per `(fixture, invariant)` pair for these nine invariants:

- `test_label_x_matches_segment_midpoint_on_horizontal_runs`
- `test_stack_station_xs_share_column`
- `test_row_trunk_marker_cy_consistent`
- `test_no_kink_at_section_boundary`
- `test_symfan_pairs_share_y`
- `test_lines_dont_cross_non_consumer_markers`
- `test_topological_siblings_share_y_or_symmetric`
- `test_section_bbox_has_bottom_padding`
- `test_off_track_inputs_above_consumer`

If new invariants are added to the suite, the script will still surface them in the page but without a tailored explanation block; add a new finder + explanation entry in `build_review.py` to give them a structured highlight.

## Recipe

Assume an nf-metro checkout at `$PWD` (or a worktree off it) and the `nf-metro` micromamba env is available.

1. **Activate the env and pin `PYTHONPATH` to the worktree's `src/`** (the script does not require a `pip install`, just an importable engine):

   ```bash
   source ~/.local/bin/mm-activate nf-metro
   export PYTHONPATH="$PWD/src"
   ```

2. **Pick an output directory** outside the repo to keep generated SVGs out of git, for example `/Users/jonathan.manning/projects/nf-metro-triage` or `/tmp/triage-<task>`:

   ```bash
   OUT=/Users/jonathan.manning/projects/nf-metro-triage
   mkdir -p "$OUT"
   ```

3. **Run the build script**. By default it invokes pytest itself to discover the FAILED/XFAIL set, then renders each fixture and writes `index.html` plus `renders/`:

   ```bash
   python .claude/skills/nf-metro-layout-triage/build_review.py \
       --worktree "$PWD" \
       --output-dir "$OUT"
   ```

   If you already have pytest output captured in a log file (e.g. `pytest tests/test_layout_invariants.py -rfX --tb=no -q > /tmp/inv.log`), pass it via `--fail-list /tmp/inv.log` to skip re-running the suite.

4. **Serve the output**. The HTML is self-contained (SVGs are inlined as base64) but a local server makes loading and JSON export reliable:

   ```bash
   cd "$OUT" && python -m http.server 8765
   ```

   Then point the user at <http://localhost:8765>.

5. **Triage in the browser**. For each row pick **Bug**, **Not a bug**, or **Ambiguous** and optionally add a note. The state persists in `localStorage` per browser. When done, click **Export JSON** in the page header - this downloads `xfail-review-tags-<timestamp>.json` containing `{key: {tag, notes}}` keyed by `<fixture>__<invariant>`.

6. **Clean up** when the triage is finished:

   ```bash
   # Stop the http.server (Ctrl-C) and optionally
   rm -rf "$OUT"
   ```

## How the script works (in brief)

- `--worktree` is added to `sys.path`, so the script imports the parser, layout engine, routing, labels, and SVG renderer directly from that checkout. No pip install required.
- Each fixture is rendered once via the `nf-metro` CLI (cached in `<output-dir>/renders/<fixture>.svg`) and laid out once via `compute_layout()`.
- For each invariant the script runs a finder that re-derives the offending geometry (port Y, trunk marker cy, fan column, label X, etc.) and emits a red dashed rectangle into a per-row annotated SVG (`<output-dir>/renders/annotated/<key>.svg`).
- The HTML embeds the annotated SVG as base64 data URI, so the page is fully portable - you can drop `index.html` anywhere and it still works (although the page also references `renders/` for debugging).
- If pytest is rerun later, the rendered SVGs are cached - delete `<output-dir>/renders/` to force a fresh render.

## Output anatomy

```
<output-dir>/
  index.html                       # the page to open
  fail-list.txt                    # raw pytest output (only when --fail-list not passed)
  renders/
    <fixture>.svg                  # base render per fixture (cached)
    annotated/
      <fixture>__<invariant>.svg   # base + red overlay per row
```

The exported triage JSON lands in the user's browser Downloads folder; it is not written by the script.

## When the explanation is generic

If the invariant fires inside the test harness but the embedded finder cannot reproduce the offending element (e.g. the test relies on a slightly different layout-param path), the row shows a yellow "no offending element" note plus a generic invariant-level explanation. Treat those as "no red highlight, classify from the whole render".

## See also

- `tests/test_layout_invariants.py` - the actual assertions the page mirrors.
- `tests/layout_validator.py` - lower-level programmatic checks used by other test files.
- PR #326 (feat/comprehensive-invariants) - the original session this tool was built for.
