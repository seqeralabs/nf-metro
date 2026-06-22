# Testing

The test suite has four complementary validation layers, each checking a
different artifact at a different point in the pipeline.  Run everything
with `pytest`; run one file or test with the usual selectors:

```bash
pytest                                   # all tests
pytest tests/test_topology_validation.py # one file
pytest tests/test_parser.py::test_parse_title
```

## Fixtures

Test fixtures live in `tests/fixtures/` (`.mmd` files, plus
`tests/fixtures/regressions/` for bug-specific minimal repros and
`tests/fixtures/nextflow/` for Nextflow-DAG inputs).  Larger example
pipelines live in `examples/`, and the topology stress fixtures in
`examples/topologies/` (inventory and known issues in
[`examples/topologies/README.md`](https://github.com/pinin4fjords/nf-metro/blob/main/examples/topologies/README.md)).

Many tests auto-discover fixtures by globbing these directories, so
**adding a `.mmd` file under the right directory enrolls it in the
relevant parametrized suites automatically**.

## Adding a topology test

`tests/test_topology_validation.py` parametrizes over every
`examples/topologies/*.mmd` fixture (via `TOPOLOGY_FILES`).  Each fixture
is parsed and laid out, then the `TestTopologyValidation` methods run the
programmatic checks from `tests/layout_validator.py` against it
(section overlap, station containment, port boundary, edge waypoints,
edge/section crossing, and so on).

To add a topology case, drop a new `.mmd` into `examples/topologies/`;
it is picked up by the parametrization with no further wiring.  Add a
fixture-specific assertion only if it needs one beyond the shared
checks.

## Adding a layout invariant

`tests/layout_validator.py` holds `check_*` functions that take a
laid-out `MetroGraph` and return a list of `Violation`s, each with a
`Severity` (`ERROR` or `WARNING`).  The topology suite gates on `ERROR`s
only; `WARNING`s are reported but do not fail CI unless a test promotes
them.  To add a check: write a new `check_<thing>` returning
`Violation`s, then call it from a test (the topology suite, or a
dedicated test).

`tests/test_layout_invariants.py` holds the cross-section bundle-
alignment invariants (e.g. `test_row_trunk_marker_cy_consistent`,
symmetric-fan column-mates, off-track inputs above their consumer).
These parametrize over discovered fixtures and use the helpers in the
file (`_layout`, `_section_trunk_info`, etc.).  Known defects are pinned
with strict `xfail` markers so that a fix flips them to `XPASS` and reds
CI, prompting the marker's removal.

The per-phase preconditions, postconditions, and invariants the layout
engine must preserve are documented in
[`src/nf_metro/layout/CONTRACT.md`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md);
see also [Layout pipeline](layout_pipeline.md).

## The byte-identical gallery diff

Layout and rendering changes are reviewed by rendering the whole gallery
before and after and diffing the SVGs.  This is automated in CI by
`.github/workflows/pr-renders.yml`, which:

1. Renders every gallery entry on the PR branch
   (`python scripts/build_gallery.py --debug`) and saves the SVGs.
2. Checks out the base branch and renders the same gallery.
3. Runs `python scripts/build_render_diff.py BASE_DIR PR_DIR OUTPUT_DIR
   --pr <NUMBER>` to build a side-by-side before/after page for only the
   outputs that changed.

`build_render_diff.py` exits `2` when there is **no** difference: a
PR that intends to be visually neutral should produce a byte-identical
gallery (no diff page).  The preview is published at
`https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/`.

To reproduce locally, render the gallery on each branch into separate
directories and run the diff script the same way:

```bash
python scripts/build_gallery.py            # writes docs/assets/renders/*.svg
python scripts/build_render_diff.py /tmp/base /tmp/pr /tmp/diff_site
```

`scripts/render_topologies.py` batch-renders the topology fixtures to
`/tmp/nf_metro_topology_renders/` for quick visual inspection.

The gallery itself is defined by `GALLERY_ENTRIES` in
`scripts/build_gallery.py`.  A new example only appears in the rendered
gallery (and the render diff) once it is added to that list.

## The four validation layers

The pipeline has four validation layers, each checking a different artifact
at a different point in processing.  They are complementary, not redundant:
each catches bugs the others cannot see.

| Layer | What it checks | When |
|---|---|---|
| **Layout oracle** | Graph geometry after layout (needs graph structure to interpret coordinates) | Every topology test |
| **Routing invariants** | Edge waypoints as each route is computed (catches bad paths immediately) | Always-on during routing |
| **Phase guards** | Layout engine pre/post-conditions at each phase boundary (pinpoints which phase introduced a bug) | Always-on per phase |
| **Render oracle** | Finished SVG as drawn (catches problems that only emerge from the actual pixel output) | Opt-in CLI flag; corpus pytest gate |

### Layer 1 - Layout oracle (`tests/layout_validator.py`)

**What it does**: after the layout engine has assigned coordinates to every
station, port, and edge, this layer inspects the result and flags geometric
violations.  Because it runs against the in-memory graph (not the drawn SVG),
it knows the full context: which nodes are ports vs. stations, which lines
share a bundle, and what the section boundaries are.  That context lets it
check things a raw SVG parser cannot, such as whether an edge waypoint stays
inside the section it should pass through, or whether a port lands on the
correct face of its section.

**What it catches uniquely**: section-overlap, station outside its section
box, station used as an elbow (a geometry invariant that requires knowing
which node is a station vs. a port), port off its boundary, edge waypoints
straying out of bounds, and route-crosses-section-box violations.

**How it's wired**: `check_*` functions in `tests/layout_validator.py` take
a laid-out graph and return `Violation` objects with `ERROR` or `WARNING`
severity.  `tests/test_topology_validation.py` runs all of them against every
topology fixture; `ERROR`s fail CI, `WARNING`s are reported but do not.

### Layer 2 - Routing invariants (`src/nf_metro/layout/routing/invariants.py`)

**What it does**: checks each edge's route as soon as it is computed, before
the SVG is written.  This is the earliest point at which a routing bug can be
caught - at the level of the raw waypoint list for a single edge.

**What it catches uniquely**: path-level problems that require no graph
context to diagnose, such as a near-horizontal diagonal (a line that should
be 45° but drifts), a missing curve, or a waypoint that places a path inside
a section it should pass around.  These can only surface here because the
layout oracle runs after all edges are done, and the render oracle reads the
drawn artifact where individual waypoints are no longer visible.

**How it's wired**: the `CHECK_REGISTRY` runs at the end of every call to
`route_edges`.  Tier-A checks are always-on and abort rendering if they fail.
Tier-B checks are either issue-pinned (used to track known defects against
the corpus) or conditional (fire only under a specific routing arm).

### Layer 3 - Phase guards (`src/nf_metro/layout/phases/guards.py`)

**What it does**: the layout engine runs as a sequence of ~40 numbered phases
(grid placement, port inference, coordinate assignment, and so on).  Phase
guards are assertions inserted at the boundaries of those phases to check that
each one left the graph in a valid state.  When a guard fires, the phase name
is in the error, so a regression is immediately localised to the phase that
broke the invariant rather than appearing as a mysterious geometry error at
render time.

**What it catches uniquely**: mid-pipeline state corruption that the layout
oracle (which runs after all phases) and the routing invariants (which run
after routing, not layout) cannot see.  For example, a guard checks that port
coordinates are not altered by phases that should not touch them.

**How it's wired**: `GUARD_REGISTRY` and `INLINE_GUARD_REGISTRY` record every
guard with its classification (always-on, defensive, or issue-pinned) and
narrow reason.  Always-on guards execute every time their phase runs.
Issue-pinned guards fire once per corpus run via `tests/test_guard_coverage.py`
and are marked `XFAIL`; when the underlying issue is fixed, CI turns red until
the pin is removed.

### Layer 4 - Render oracle (`src/nf_metro/render/validate.py`)

**What it does**: parses the finished SVG as an outside consumer would - no
access to the in-memory graph, only the drawn lines and text.  This mirrors
how a visual regression would actually manifest: the SVG is wrong, and we need
to know why from the artifact alone.

**What it catches uniquely**: geometry bugs that only emerge from the final
pixel output.  The layout engine might compute positions that are technically
non-overlapping in graph coordinates, but after font metrics, stroke widths,
and SVG transforms are applied, a station label ends up sliced by a route
polyline, or two lines that were assigned distinct offsets end up drawn flush
because a rounding step collapsed them.  Neither the layout oracle nor the
routing invariants can see this, because they run before the SVG is produced.

**How it's wired**: `validate_render(svg, *, graph=None)` checks label-strike
(a route polyline crosses a station label), marker crossings (a route passes
through a node marker it does not serve), and - when the graph is supplied -
offset-collapse (lines drawn flush despite being assigned distinct offsets).
Enabled with `nf-metro render --validate` or `nf-metro validate-svg
--geometry`; a corpus-wide pytest gate runs it against every fixture.
