# Layout-engine failure modes & structural axes

This is the menu the stress-render skill draws from when composing a novel
layout. The engine is solid on shapes it has seen; it breaks on *new
combinations*. Your job is to pick axes that have not been combined before
and braid them into one believable pipeline.

## Table of contents
- [How to use this](#how-to-use-this)
- [Structural axes (the dice)](#structural-axes-the-dice)
- [Known-fragile areas (likely dup, not novel)](#known-fragile-areas-likely-dup-not-novel)
- [What the probe catches vs what only your eye catches](#what-the-probe-catches-vs-what-only-your-eye-catches)
- [Authoring-correctness rules (so a defect is the engine's fault)](#authoring-correctness-rules)

## How to use this

The existing `examples/topologies/` corpus (see its README) already covers
each axis *in isolation* and a handful of pairs. Coverage of an axis alone is
not novelty. Novelty is **A x B x C where that triple has never been drawn** -
e.g. "a fold whose return row also carries an off-track output feeding a
station inside a TB section." Read `coverage_log.md` to see what past runs
already tried, and deliberately go somewhere else.

Bias toward combinations that sit *near* an open issue or known-fragile area
but are not that issue - the seams next to known cracks are where the next
crack usually is.

## Structural axes (the dice)

Pick 2-4. The more axes you braid into one connected graph (not separate
disconnected toys), the more likely an interaction bug surfaces.

| Axis | What it stresses | Engine machinery exercised |
|---|---|---|
| **Fan-out** (1 -> N) | junction insertion, port spacing, bundle slot reservation | `_create_ports_and_junctions`, ordering |
| **Fan-in** (N -> 1) | bundle ordering at L-corners, merge junctions | inter-section handlers |
| **Diamond / fork-join** | distinct track per branch, uneven branch lengths | `_equalize_fork_groups`, diamond detection (#610) |
| **Fold / serpentine** | wrap to RL return row, double fold, reading direction | `auto_layout` fold threshold, `reversal.py` - **a fold only fires on a near-linear section spine that exceeds ~15 station-columns; a branchy DAG stacks vertically instead and never folds. To exercise this axis, build a long mostly-linear chain, or pin `%%metro fold_threshold:` low.** |
| **Fan across a fold** | fan-out/in spanning the row boundary | rowspan optimisation |
| **Mixed port sides** | TOP+BOTTOM exits, LEFT+RIGHT on TB | `ports.py`, station-as-elbow risk |
| **TB + LR mix** | perpendicular ports, diagonal-label gating | `_infer_directions`, TB guards |
| **Off-track inputs** | inputs sit above their consumer, not on a line | `off_track.py` |
| **Off-track outputs** | producer-fed sinks below the line (#573) | `_space_off_track_output_columns` |
| **Wide labels** | auto-wrap, column spread, diagonal strike-through | `labels.py`, label-strike levers (#513) |
| **Dense bundle** | many lines sharing a trunk, tall station pills | `offsets.py`, `compute_bundle_info` |
| **Bypass** | line routing around an intervening section box | bypass handlers (#484), bypass-V (#632) |
| **Skip edges on a spine** | linear chain with forward jumps | fold-suppression gate (#551) |
| **Tall-narrow anchor** | one dominant tall fan beside a narrow chain | `_detect_tall_anchor_chain` (#552) |
| **Same-line divergence/convergence** | one colour splitting then rejoining a port | coincide-fanout passes (#546) |
| **Multi-source convergence** | several sources, one line, one sink | merge-junction routing |
| **Disconnected components** | independent sub-pipelines, row stacking | component packing |
| **Rails / line_spread** | bundle vs centered vs rails convergence | `rail_mode.py`, `line_spread` |
| **Cycle / self-loop** | a back-edge or `a --> a` | now rejected fast with a node-naming error (#645, fixed) - the probe surfaces it as a parse issue; expected behaviour, not a find |

## Known-fragile areas (likely dup, not novel)

If your render trips one of these, it is almost certainly a *known* defect.
Cross-check the open issue before drafting - link/append, don't re-file. These
are also areas where a validator ERROR may be expected, so don't treat them as
fresh discoveries:

- **#255** station-as-elbow recurrence in TB-direction sections (OPEN bug).
- **#248** funcprofiler upstream layout quality (dense fan-out + fan-in).
- **#533** `graph TB` primary direction is not honoured (silently warns).
- **#556** diagonal labels gated to LR-only; TB sections get horizontal labels.
- **#555** diagonal-label column pitch is graph-wide (sparse fans inherit dense pitch).
- **#589** debug grid lines at logical `station.y`, not bundle-centred markers.
- **#349** triage red bboxes don't mark the true label position.

A novel find is one that does *not* map onto any of these and is not already in
`gh issue list`.

## What the probe catches vs what only your eye catches

`probe_layout.py` is good at *structural* defects but blind to *aesthetic*
ones. Know the split so you review the render for what the validator can't see.

**Probe catches (file as obvious bug):**
- layout crash / `PhaseInvariantError` guard failure
- section box overlap, station outside its box, port off the boundary
- coincident stations, station-as-elbow (line passes through a marker it doesn't stop at)
- near-horizontal-but-not edges, excessive column gaps, route-segment crossings
- label/label and label/box overlap (geometric)

**Only your eye catches (present to user, draft only with their nod):**
- a line that is *technically* clear but reads as an ugly detour or wobble
- asymmetric fans that "should" mirror
- a diagonal that grazes a label the geometric check rounded as clear
- bundle ordering that is legal but visually scrambles line identity
- overall balance / whitespace / "does this look like a metro map"
- reading-direction confusion across a fold

## Authoring-correctness rules

A filed bug is only credible if the `.mmd` is well-formed - otherwise you are
reporting your own mistake. Before probing, verify:

1. **Lines only traverse stations they consume.** An edge `a -->|rna| b` means
   the `rna` line stops at both `a` and `b`. Never route a line *through* a
   station it doesn't serve - that is the #1 authoring mistake that masquerades
   as a "line crosses non-consumer" engine bug. If a line must pass a section
   without stopping, that is a genuine *bypass* (model it with the line simply
   not having an edge into that section's internals).
2. **Every `%%metro line:` id used in an edge is declared**, and every edge line
   id is declared (the parser raises otherwise - the probe surfaces it as a
   parse issue).
3. **Off-track inputs/outputs carry their directive** (`%%metro off_track:`),
   or they will be treated as on-line stations.
4. **Ports match the inter-section edges** - entry/exit directives name the
   lines that actually cross that boundary.
5. **Prefer auto-layout.** Omit `%%metro grid:` / `direction:` unless the axis
   you are testing *is* manual placement. The strongest finds come from what
   the engine infers, not what you pin. (If you pin everything, you have tested
   your own arithmetic, not the engine.)

If the probe reports a parse issue, it is a rule-1..4 violation: fix the `.mmd`
and re-probe. Only a clean parse with an engine-level finding is a bug worth
drafting.
