# Unifying section bbox-top management into one content-hugging rule (#464)

Design doc. No production code change, no behaviour change, no PR. All findings
are measured against origin/main (`9807c85`); `phases/bbox.py` and
`phases/row_align.py` are byte-identical between the local checkout and
origin/main, and #463 (the only behavioural delta on the branch) was gallery
byte-identical, so the measured layouts equal origin/main's.

## 1. Characterise the current mechanism (measured)

### 1.1 Every operation that writes a section's bbox top

Traced by wrapping each candidate helper in `engine` and snapshotting `bbox_y`
per section across the whole pipeline. Two distinct families emerge.

**Top SIZING family** (changes the top edge relative to content - in scope for #464):

| Stage | Helper | Direction | Effect on top edge |
|------|--------|-----------|--------------------|
| 1.2 | `_align_row_y_grids` | set | `bbox_y = min(content) - max_y_pad` (local coords, construction) |
| 4.6 | `_recompute_grid_group_bboxes` | set | resets grid-group bbox to symmetric `max_y_pad` around content, then expands for stray ports |
| 5.3 | `_top_align_row_bboxes_only` | grow-only | flush row tops up to the row group's min `bbox_y`; content left in place |
| 6.9 | `_top_align_row_bboxes_only` | grow-only | same helper, re-run after off-track recenter (gated on `center_ports`) |
| 6.15a | `_grow_bboxes_to_content_top` | grow-only | raise top to `content_top - padding` where content rose above the current top |
| 6.6 / 6.8 | `_reanchor_off_track_to_consumer` -> `_off_track_fit_top` + `_set_section_bbox_top` | grow AND shrink | off-track sections only; bidirectional fit (added by #463) |

**Whole-section SHIFT family** (translates content + box together, top padding preserved - OUT of scope):

| Stage | Helper | Effect |
|------|--------|--------|
| 3.5 / 4.7 | `_top_align_row_sections` | shifts stations AND bbox up to row min top |
| 4.8 | `_align_row_trunk_ys` | shifts content + grows `bbox_h` downward (top fixed) |
| 6.13 phase 2 | `_tighten_lower_rows_after_shrink` | translates whole lower rows up |

The task's framing of two "top-align families" is correct: `_top_align_row_sections`
(3.5 / 4.7) shifts stations with the box and is construction-time scaffolding, not
top sizing. It is OUT of scope. The bbox-top sizing family in scope is
{5.3, 6.9} (flush) and {6.15a} (grow-to-content), with {1.2, 4.6} as construction
seeds and {6.6, 6.8} as the off-track precedent #463 already converted.

### 1.2 Two corrections to the issue's stated mechanism

The issue names a "trio": flush (5.3 / 6.9), grow-to-content (6.15a), and
"the top-shrink half of `_shrink_and_tighten_rows` (6.13)". Measurement refutes
two parts of this:

- **6.13 has no top-shrink.** `_shrink_and_tighten_rows` = `_shrink_bboxes_to_content_bottom`
  (bottom edge only, `bbox_y` untouched) + `_tighten_lower_rows_after_shrink`
  (whole-row translation). Where 6.13's top edge moves at all (differentialabundance
  67px, genomic_pipeline 749px, variantbenchmarking 50px), top == content == bottom
  move identically: a translation, not a top resize. CONTRACT.md's own 6.13 entry
  lists "Bbox tops" under invariants preserved. The trio is really a **duo**:
  {flush 5.3/6.9} + {grow-only 6.15a}.

- **The flush is not transient scaffolding that gets re-established as content-hug.**
  It survives untouched to the final pass. On `terminal_symmetric_fan` the two
  sections' tops are flush at `bbox_y=70` through every stage from 3.5 to 6.9; the
  final non-flush 40px state is *created* by 6.15a, which raises `reporting`'s top
  from 70 to 30 to restore a padding band over its (high) content, while `source`
  (content lower) is left. The flush is overridden by grow only, not "re-established".

### 1.3 Why finished tops are content-hugging on fans but keep empty bands elsewhere

The mechanism is entirely the grow-only nature of 6.15a:

- content sits **above** the flushed top (crowded, gap < padding) -> 6.15a grows
  the top up to `content - padding` -> content-hug, flush broken
  (terminal_symmetric_fan: both end at exactly 50px padding, 40px row-top spread).
- content sits **below** the flushed top (gap > padding) -> 6.15a cannot lower the
  top -> the flush band persists as empty space.

So the engine is already *inconsistent*: it content-hugs in the grow direction and
keeps flush bands in the shrink direction. CONTRACT.md tags 6.15a "invariant - each
bbox top sits a full section_y_padding above its highest marker". That is true only
as a **floor** (>= padding); `test_section_bbox_has_top_padding` is a `>=` check
(`gap + tol < padding` is the only offender), so a 116px band passes it. #464 tightens
the floor to an **equality** (gap == padding, no excess band), which requires the
shrink direction 6.15a lacks.

### 1.4 The genuine empty-band set (measured)

Comparing each section's final `bbox_y` to a content-only hug target
(`min(content) - padding`, clamped to contain ports / bypass, no row-above clamp):

**Group A - pure flush bands (nothing above content):**

| Fixture | Section(s) | Empty band |
|---------|-----------|-----------|
| differentialabundance | reporting | 58px |
| differentialabundance_default | differential, reporting | 116px each |
| off_track_convergence | input, output | 233px each |

These boxes are stretched up to flush with a tall row-mate (a fan or off-track
section) and have genuinely empty space above their topmost station.

**Group B - band occupied by a LEFT/RIGHT entry port above content (40-54px):**
rnaseq_auto/sections/manual `postprocessing`, tb_file_termini `reporting`,
fold_double `calling`/`integration`, fold_fan_across `normalize`,
fold_stacked_branch `integration`, u_turn_fold `sec6`. Here a side entry port sits
in the band (e.g. port at y=120, content at y=160). Shrinking interacts with port
headroom and the station-as-elbow constraint, so this group is higher risk.

**A naive symmetric application would over-tighten.** Applying the existing
`_section_content_top_target` (bbox.py) bidirectionally flags ~60 sections for a
~26px top drop. That 26px is exactly `SECTION_HEADER_PROTRUSION`: the function's
row-above clamp (`above_bot + section_y_gap + SECTION_HEADER_PROTRUSION`) is a
GROW bound ("don't grow the top up into the badge"), not a content-hug position.
For a section that already sits above that clamp (e.g. `wide_fan_out.target_b`:
`bbox_y=220` already hugs content at 270 with exactly 50px), using the clamp as the
equality target would push the top DOWN to 246, cutting content padding to 24px.
So `_section_content_top_target` cannot be the bidirectional target as written; its
row-above clamp must constrain the grow direction only.

### 1.5 Feasibility check (Group A, through the real pipeline)

A runtime monkeypatch making 6.15a also shrink pure-flush bands (sections with no
port / bypass above content), run with `compute_layout(validate=True)` so all
phase-boundary guards fire:

- differentialabundance_default: differential / reporting drop to `bbox_y=183.6`,
  exactly 50px above content (total height 730px honoured), guards pass.
- off_track_convergence: input / output drop to `bbox_y=300.4`, 50px padding,
  guards pass.
- differentialabundance: reporting hugs at 50px, guards pass.

The minimal change is viable end-to-end including the guard suite. The off-track
`process` section is untouched, so off_track_convergence's row 0 becomes one tall
box (process) plus two short content-hugging boxes - a staggered-top row.

## 2. The target rule

### 2.1 Statement

> A section's bbox top sits exactly `section_y_padding` above its highest
> in-section marker (real station centre, off-track input, or bypass-curve
> clearance), and never lower (no higher `bbox_y`) than the inclusion bound set
> by its TOP / side ports. The row above is honoured as a grow-direction bound
> only: the top is never raised *above* `row_above_bottom + section_y_gap +
> SECTION_HEADER_PROTRUSION`, but that bound never pushes the top *below* its
> content-hug position.

This is the equality version of 6.15a's current floor, applied in both directions.

### 2.2 The primitive already exists

#463 built exactly the right shape for off-track sections and #464 generalises it:

- `_set_section_bbox_top(graph, section, new_top)` (`phases/_common.py`) - moves the
  top in either direction, pulls TOP ports to the new edge, leaves BOTTOM ports.
  This is the reversible primitive the issue names.
- `_off_track_fit_top(graph, section, highest_off_track_y, padding)`
  (`phases/off_track.py`) - content-hug target = `highest - padding`, clamped by
  other content (`st.y - padding`) and non-TOP ports (`port.y`, hard-contain, no
  pad). It has NO row-above clamp. This is precisely the calibration the unified
  rule needs; #464 widens its input from "off-track band" to "all content".

The unified target is therefore a generalisation of `_off_track_fit_top`, applied
to every section via `_set_section_bbox_top`, with the row-above protrusion clamp
re-added as a grow-only ceiling (taken from `_section_content_top_target`).

### 2.3 Content-set consistency (a known footgun)

The target helper and any new test must use the SAME content set. The off-track
helpers exclude `is_port` + `__bypass_` prefix and KEEP hidden phantoms (12 of them);
`test_section_bbox_has_top_padding` excludes ports and uses `is_hidden`. #463's notes
flag that swapping `__bypass_`/`is_port` for `is_hidden` diverges (is_hidden is a
superset). The new rule must mirror `_section_content_top_target` / `_off_track_fit_top`
(port + bypass prefix), not the test's `is_hidden`, to avoid drift.

### 2.4 Is band-alignment a genuine final-boundary requirement?

Evidence says **no**. Row-top flush is tagged transient at 3.5, 5.3 and 6.9 in
CONTRACT.md, and it is not a final property anywhere: 6.15a already breaks it on
every fan (terminal_symmetric_fan, trunk_through_fan). No fixture was found where
pure content-hug misaligns a band that the current code keeps aligned AND a route
or bundle runs along that aligned top edge (tops are decorative box edges; shared
horizontal bundles run at trunk Y, governed by `_align_row_trunk_ys` at 4.8, which
is unaffected). The persistent flush bands (Group A) are exactly the empty space
the #461 experiment set out to remove.

The "genuine conflict" the issue anticipates does exist, but it is not band vs
content - it is **content-position vs box-top**: you cannot raise one section's
content into its band without breaking the shared trunk Y (compaction is bounded by
the tightest row-mate, see section 3), so the only way to remove a band is to lower
the box top. The unified rule resolves it by lowering the box (decoupling box-top
from row alignment) rather than raising content. No explicit band-vs-content
priority constant is needed; the priority is simply "hug content, bounded by ports
above and by the row-above badge below in the grow direction only".

## 3. Compaction interaction (Stage 5.4)

`_compact_row_content_to_bbox_top` does two things:

- **Step 1 (content pull-up):** shift each row column-group's content up by the
  group-minimum of `content_min - bbox_y - padding`, keeping `bbox_y` fixed. This
  reads the flushed top and pulls content toward it, uniformly to preserve trunk Y.
- **Step 2 (bottom shrink):** shrink each `bbox_h` so the bottom is `padding` below
  content. Independent of the top.

**Measured:** step 1's content move is effectively dead - it is non-zero only on
variantbenchmarking and variantbenchmarking_auto (16.8px); zero on every other
fixture. The reason is the group-minimum: when any row-mate is already tight
(content at top), the allowable shift is ~0 and the whole group stays, which is why
the differentialabundance_default band is not closed by compaction.

**Under content-hug:** if the flush at 5.3 is replaced by per-section content-hug,
`bbox_y == content_min - padding`, so step 1's shift is identically 0 everywhere.
Step 1 becomes redundant. Step 2 (bottom shrink) is top-independent and unaffected.
CONTRACT.md already tags 5.4 transient (superseded by 6.1 / 6.13).

**Dependency to verify, not assume:** 5.4 currently runs at Stage 5.4, well before
the final top pass (6.15a). Stages 6.1 / 6.2 / 6.11 ("fan content into the empty top
band", tests `test_section_top_band_filled`) read the *current* band to decide
whether to fan siblings up. If the band is removed earlier (top hugged at 5.x), those
fans see no band and skip - which is the desired outcome (no band to fill) but is a
behaviour change that must be vetted on the fixtures those stages target. The safe
sequencing is to keep the band-producing flush until after 6.1 / 6.2 / 6.11 and do
the bidirectional hug at the 6.15a slot (the existing final top pass), so the
band-fill fans still run against the transient flush band exactly as today, and only
the *final* top is hugged. This makes the first increments behaviour-preserving for
the fan-fill stages.

## 4. Staging plan (smallest-first, each separately vettable)

The decomposition keeps the flush (5.3 / 6.9) and fan-fill (6.1 / 6.2 / 6.11) in
place and changes only the FINAL top pass (6.15a), so each increment's render delta
is isolated to "bands that survive to the end".

**PR1 - introduce the bidirectional primitive, behaviour-preserving.**
Generalise `_off_track_fit_top` into a `_section_fit_top` content-hug target
(all content, ports/bypass clamps, plus the row-above protrusion ceiling as a
grow-only bound) and re-express `_grow_bboxes_to_content_top` to call it via
`_set_section_bbox_top` but STILL grow-only (skip when the target would lower the
top). Expected render impact: **none** (byte-identical gallery). Verification: full
gallery hash diff == main; existing top/bottom-padding + lifecycle tests green.

**PR2 - enable shrink for Group A (pure flush bands).**
Allow the final pass to also lower the top for sections with no port / bypass above
content. Expected render impact: differentialabundance, differentialabundance_default,
off_track_convergence lose their empty bands (boxes drop to content-hug; row tops
become staggered). 5 sections, 3 fixtures. Highest-risk fixture here is
off_track_convergence (233px change, adjacent to #463's off-track machinery).
Verification: gallery diff shows only those 3 fixtures; off-track invariant tests
(`test_off_track_inputs_above_consumer`) and lifecycle tests green; new equality test
(see PR4) added scoped to Group A.

**PR3 - enable shrink for Group B (port-bearing bands), conditionally and per-case.**
Group B is NOT a blanket "shrink all six". Some of those port-bearing bands are
intentional vertical runway for a side entry port's approach; shrinking them would
starve the port and risk a station-as-elbow violation. PR3 shrinks only the
genuinely-empty port-bearing bands and keeps the port-approach-clearance ones, vetted
case by case. It is fine for PR3 to shrink a subset or to be dropped entirely.
Candidate set to triage: fold_double, fold_fan_across, fold_stacked_branch,
u_turn_fold, rnaseq_* postprocessing, tb_file_termini reporting (40-54px each).
Verification: `check_station_as_elbow`, port-on-boundary, no-kink tests; gallery diff
limited to the retained subset; eyeball each (fold + port interaction).

**PR4 - retire the dead phases and lock the invariant. The flush stays.**
Once the final pass hugs content in both directions: remove 5.4 step 1 (content
pull-up, dead), and collapse the grow-only 6.15a into the single bidirectional pass.
**Do NOT drop the 5.3 / 6.9 flush** - it is load-bearing: it creates the transient
band that the fan-fill stages (6.1 / 6.2 / 6.11) fan content into, so removing it
would regress those. Keep it; only the dead 5.4 step-1 is retired and the grow-only
6.15a is folded into the bidirectional pass. Add `test_section_bbox_top_hugs_content`
(equality: `abs(gap - padding) <= tol` for sections with no port above content),
keeping the existing `>=` `test_section_bbox_has_top_padding`. Update CONTRACT.md:
retag 6.15a's lifecycle from floor to equality, mark the retired step, and note
band-alignment is not a maintained property. Verification: gallery diff == union of
PR2 + PR3 deltas only; new equality test green; lifecycle completeness test
(`test_contract_lifecycle`) updated.

**Highest-risk fixtures across the whole change:** off_track_convergence (PR2) and
the fold_* / u_turn_fold port-fed bands (PR3). Verification approach throughout:
full-gallery SVG hash/diff via `scripts/build_gallery.py` + render-diff against main,
the off-track and lifecycle invariant suites, and the new equality test. The change
is render-visible by design, so the PR-render CI preview
(`https://pinin4fjords.github.io/nf-metro/_pr/<N>/`) is the authoritative gate on
each of PR2 / PR3.

## 5. Decisions for the user, and risk

**Decisions (recommendation in bold):**

1. **Band-alignment: drop it (no explicit priority).** No load-bearing case found;
   flush is already transient and broken on fans. The rule is "hug content". If the
   user wants tidy aligned row tops as an aesthetic, that is a separate opt-in, not
   a final-boundary requirement, and should not block #464.

2. **Group B scope: ship Group A first (PR2), then decide on Group B (PR3) after
   seeing the PR2 preview.** Group A is a clean win; Group B touches port headroom
   and station-as-elbow and may have cases where the current band is intentional
   port-approach clearance.

3. **3.5 / 4.7 station-shifting top-align: OUT of scope.** They translate content
   with the box (no empty band) and feed downstream alignment; leave them.

4. **Row-above badge clearance vs content padding (the latent 26px / `target_b`
   case): keep current behaviour - the protrusion clamp bounds the GROW direction
   only and never pushes a top below its content-hug position.** This deliberately
   does NOT "fix" sections that currently sit closer than `gap + protrusion` to the
   row above, to avoid cutting content padding. If the user wants that enforced, it
   is a separate, larger render change (independent of #464).

5. **Staging boundaries:** as in section 4 (PR1 no-op refactor, PR2 Group A, PR3
   Group B, PR4 retire + lock). Each PR's gallery delta is pre-stated and small.

**Risk assessment:** the change is cleanly stageable and the per-increment render
churn is bounded and pre-enumerated (Group A: 3 fixtures; Group B: 6 fixtures;
PR1 / PR4: byte-identical apart from the already-vetted deltas). The minimal change
passes the full guard suite (section 1.5). The main residual risks are (a) the
fan-fill stages (6.1 / 6.2 / 6.11) reading the band - mitigated by hugging only at
the final pass so they still see the transient flush band, and (b) off-track
adjacency at off_track_convergence - mitigated by Group A reusing #463's exact
`_set_section_bbox_top` / fit-top pattern. #464 is worth doing now, staged; it is
not forced into one large unavoidable diff.

### Decisions taken (review of this doc)

Recommendations 1-4 above adopted as-is: band-alignment dropped (no explicit
priority, the rule is "hug content"); 3.5 / 4.7 station-shifting out of scope;
the row-above protrusion clamp stays a grow-direction bound only and the latent
under-clearance / 26px `target_b` cases are deliberately not "fixed" here (separate
change); Group A before Group B, with Group A as PR2.

Two adjustments to the staging plan (folded into section 4 above):

- **Group B (PR3) is conditional and per-case, not "shrink all six."** Some
  port-bearing bands are intentional runway for a side entry port's approach;
  shrinking them would starve the port and risk station-as-elbow. PR3 shrinks only
  the genuinely-empty port-bearing bands, keeps the port-approach-clearance ones, and
  may ship a subset or be dropped.
- **PR4 keeps the 5.3 / 6.9 flush.** It is load-bearing (it creates the band the
  fan-fill stages 6.1 / 6.2 / 6.11 fan content into). PR4 retires only the dead 5.4
  step-1 and folds the grow-only 6.15a into the bidirectional pass; the flush stays.

### Implementation status

- **PR1 (this change): done.** `_section_content_top_target` generalised to
  `_section_fit_top` (the content-hug top target, generalising the off-track fit to
  all content), and `_grow_bboxes_to_content_top` routed through the bidirectional
  `_set_section_bbox_top` while still gated grow-only. Gallery byte-identical (63/63
  SVGs). Added unit tests exercising the bypass, port and row-above clamps of
  `_section_fit_top` (those branches never bind at the current grow-only call site,
  the same coverage gap #463 closed for `_off_track_fit_top`).
- **PR2 onward: pending.** They change renders and need preview review; PR3/Group B
  needs the per-case judgement above.

## Verification (for the implementation phase, not this design task)

- `python -m nf_metro render <fixture> -o /tmp/x.svg` + cairosvg PNG for local
  iteration (nf-metro micromamba env).
- `scripts/build_gallery.py` + render-diff vs main for each PR; PR-render CI preview
  is authoritative.
- `pytest tests/test_layout_invariants.py tests/test_contract_lifecycle.py
  tests/test_topology_validation.py`; add `test_section_bbox_top_hugs_content`.
- `compute_layout(validate=True)` on all fixtures so phase-boundary guards run.
