# Off-track reanchor: timing dependency and irreversible bbox (diagnostic)

Numbers backing the fragility fixed in #463. Measured on `origin/main`
(`d333181`) with the default content-derived `y_spacing` (58.4 px,
`SECTION_Y_PADDING = 50`).

`_reanchor_off_track_to_consumer` (`layout/phases/off_track.py`) re-pins each
off-track input at `consumer.y - n*y_spacing` and grows the section bbox
upward to keep a padding band above the highest lifted icon. It is correct
today only because of *where* it runs in `_compute_section_layout`: at Stages
6.6 and 6.8, both after the Stage 6.4 grid snap. Two organic-growth artifacts
make that timing load-bearing.

## Bug (a): reanchoring against a non-final (pre-snap) consumer mislocates the icon

`differentialabundance.mmd`, section `functional`, off-track `gmt_in` feeding
consumer `gsea`:

- Final (post-snap) state: `gsea.y = 175.2`, `gmt_in.y = 116.8` - both on the
  clean `y_spacing` grid, gap 58.4.
- If the reanchor runs against a consumer that has not yet been snapped (Y
  carries a fractional residue, here `gsea.y = 187.9`), it re-pins
  `gmt_in.y = 129.5` - an off-grid Y. The icon is mislocated because the pass
  inherited the consumer's pre-snap position.

Nothing in the function detects that its precondition (consumers final and
grid-snapped) is unmet; it silently produces the off-grid result.

## Bug (b): grow-only bbox bakes in excess top slack

`_grow_section_bbox_upward` only ever lowers `bbox_y` (grows the box up); it
never raises it. So a stale or premature run that grew the box too tall is
never reclaimed. Simulating a stale too-tall box (`bbox_y` pushed up by
`2*y_spacing`) and re-running the reanchor, across all three off-track
fixtures:

| fixture | ideal `bbox_y` (H - padding) | after stale grow | after re-run reanchor | excess top slack |
|---|---|---|---|---|
| differentialabundance | 66.8 | -50.0 | -50.0 | 116.8 px |
| off_track_convergence | 66.8 | -50.0 | -50.0 | 116.8 px |
| da_pipeline | 66.8 | -50.0 | -50.0 | 116.8 px |

The re-run leaves the box exactly as tall as the stale grow left it: the fit is
monotonic, so order of operations changes the result. A consumer that later
moves down has the same effect - the off-track icon follows down, the bbox top
does not, leaving a too-tall section (da_pipeline: 58.4 px residual band when
`gsea` is moved down one pitch).

At the current call sites the consumers are already snapped and no earlier run
exists, so neither artifact bites today. The fix removes the hidden coupling:
an explicit "consumers grid-snapped" precondition (raising `PhaseInvariantError`
if violated) plus a recompute-to-fit bbox top (grow **or** shrink) so the pass
is order-independent.
