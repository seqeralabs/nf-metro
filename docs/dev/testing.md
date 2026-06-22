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

Validation is split across four complementary layers.  They cover different
artifacts and different phases of the pipeline, so they are not redundant:
each one catches a class of bug the others cannot see.

| Layer | Location | Artifact checked | When it runs |
|---|---|---|---|
| **Layout oracle** | `tests/layout_validator.py` | In-process `MetroGraph` after layout | Every test that calls `compute_layout` |
| **Routing invariants** | `src/nf_metro/layout/routing/invariants.py` | Routed edge waypoints (in-process) | Always-on at the end of `route_edges`; Tier B guards are opt-in or issue-pinned |
| **Phase guards** | `src/nf_metro/layout/phases/guards.py` | Layout phase pre/post-conditions (in-process) | Always-on per-phase; issue-pinned guards fire once per corpus run |
| **Render oracle** | `src/nf_metro/render/validate.py` | Drawn SVG artifact (post-render) | Opt-in via `--validate` / `validate-svg --geometry`; corpus pytest gate |

### Layer 1 - Layout oracle (`layout_validator.py`)

`check_*` functions take a laid-out `MetroGraph` and return `Violation`
objects.  `tests/test_topology_validation.py` runs every check against
every topology fixture.  This layer can see graph structure (which stations
are ports, which lines share a bundle) and therefore catches spatial
invariants that depend on that context: section-overlap, station-outside-
bbox, station-as-elbow, port-boundary, edge-waypoint containment, and
the crossing checks.

### Layer 2 - Routing invariants (`routing/invariants.py`)

The `CHECK_REGISTRY` runs after every call to `route_edges`.  Tier-A checks
are always-on and block rendering if they fail.  Tier-B checks are either
issue-pinned (run against the corpus to surface known defects) or guarded
(fire only when their covering condition is met).  This layer runs inside
the process on the raw waypoint lists before any SVG is written.

### Layer 3 - Phase guards (`phases/guards.py`)

`GUARD_REGISTRY` and `INLINE_GUARD_REGISTRY` record every in-phase guard
together with its issue pin, classification (defensive, always-on, or
needs-review), and narrow reason.  Always-on guards run every time the
relevant phase executes.  Issue-pinned guards fire once per corpus run via
`tests/test_guard_coverage.py` and go `XFAIL` until the issue is resolved,
at which point CI turns red until the pin is removed.

### Layer 4 - Render oracle (`render/validate.py`)

`validate_render(svg, *, graph=None)` parses the **drawn SVG** and checks
it.  Because it reads the artifact rather than the in-process graph, it
can catch geometry problems that only emerge after the SVG is written:
label-strike (a route polyline slicing through a station label), marker
crossings (a route segment passing through a non-consumer node marker),
and offset-collapse (distinct lines drawn flush where the engine assigned
them separate offsets).  The `graph` argument is required for
offset-collapse checks, since same-slot bundles and collapsed offsets are
indistinguishable in the bare SVG.
