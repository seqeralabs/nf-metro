---
title: "Testing"
description: "Testing strategy for nf-metro: unit tests, topology stress fixtures, layout validators, and the visual review loop."
sidebar:
  order: 10
---

The test suite has four validation layers, each checking a different artifact at a different point in the pipeline.
Run everything with `pytest`, or run one file or test with the usual selectors:

```bash frame="terminal"
pytest                                   # all tests
pytest tests/test_topology_validation.py # one file
pytest tests/test_parser.py::test_parse_title
```

## Fixtures

Test fixtures live in `tests/fixtures/` as `.mmd` files, with `tests/fixtures/regressions/` holding bug-specific minimal repros and `tests/fixtures/nextflow/` holding Nextflow-DAG inputs.
Larger example pipelines live in `examples/`, and the topology stress fixtures in `examples/topologies/`.
The inventory and known issues for the latter are in [`examples/topologies/README.md`](https://github.com/seqeralabs/nf-metro/blob/main/examples/topologies/README.md).

:::tip[Auto-discovery]
Many tests auto-discover fixtures by globbing these directories.
Adding a `.mmd` file under the right directory enrolls it in the relevant parametrized suites automatically.
:::

## Add a topology test

`tests/test_topology_validation.py` parametrizes over every `examples/topologies/*.mmd` fixture through `TOPOLOGY_FILES`.
`test_topology_validation` parses and lays out each fixture, then runs the programmatic checks from `tests/layout_validator.py` against it, covering geometry such as section overlap, station containment, port boundary, edge waypoints, and edge and section crossing.

To add a topology case, drop a new `.mmd` into `examples/topologies/`.
The parametrization picks it up with no further wiring.
Add a fixture-specific assertion only if the case needs one beyond the shared checks.

## Add a layout invariant

`tests/layout_validator.py` holds `check_*` functions that take a laid-out `MetroGraph` and return a list of `Violation`s, each with a `Severity` of `ERROR` or `WARNING`.
The topology suite gates on `ERROR`s only.
`WARNING`s are reported but do not fail CI unless a test promotes them.
To add a check, write a new `check_<thing>` returning `Violation`s, then call it from a test, either the topology suite or a dedicated one.

`tests/test_layout_invariants.py` holds the cross-section bundle-alignment invariants, such as `test_row_trunk_marker_cy_consistent`, symmetric-fan column-mates, and off-track inputs above their consumer.
These parametrize over discovered fixtures and use the helpers in the file, including `_layout` and `_section_trunk_info`.
Known defects are pinned with strict `xfail` markers.
A fix flips them to `XPASS` and reds CI, which is the signal to remove the marker.

The per-phase preconditions, postconditions, and invariants the layout engine must preserve are documented in [`src/nf_metro/layout/CONTRACT.md`](https://github.com/seqeralabs/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md).
See also [Layout pipeline](/nf-metro/dev/layout_pipeline/).

## The byte-identical gallery diff

Review a layout or rendering change by rendering the whole gallery before and after, then diffing the SVGs.
CI automates this in `.github/workflows/pr-renders.yml`, which:

1. Renders every gallery entry on the PR branch (`python scripts/build_gallery.py --debug`) and saves the SVGs.
2. Checks out the base branch and renders the same gallery.
3. Runs `python scripts/build_render_diff.py BASE_DIR PR_DIR OUTPUT_DIR --pr <NUMBER>` to build a side-by-side before/after page for only the outputs that changed.

`build_render_diff.py` exits `2` when there is **no** difference.
A PR that intends to be visually neutral should therefore produce a byte-identical gallery and no diff page.
The preview is published at `https://seqeralabs.github.io/nf-metro/_pr/<PR_NUMBER>/`.

To reproduce locally, render the gallery on each branch into separate directories and run the diff script the same way:

```bash frame="terminal"
python scripts/build_gallery.py            # writes docs/assets/renders/*.svg
python scripts/build_render_diff.py /tmp/base /tmp/pr /tmp/diff_site
```

`scripts/render_topologies.py` batch-renders the topology fixtures to `/tmp/nf_metro_topology_renders/` for quick visual inspection.

The gallery itself is defined by `GALLERY_ENTRIES` in `scripts/build_gallery.py`.
A new example appears in the rendered gallery, and in the render diff, only once it is added to that list.

## Advisory layout-quality metrics

Alongside the byte-identical diff, the render diff prints an advisory metrics table from `tests/layout_metrics.py`.
The metrics cover crossings, near-horizontal and lone-diagonal segments, bends and corners per route, turn angle, marker clearance, label strikes, excessive gaps, and wasted canvas.
The table does not gate CI.
The byte-identical gallery described earlier is the only thing a build fails on, and every changed render needs a human look whatever the numbers say.

The weights `scripts/optimize_layout.py` applies over those metrics were measured rather than guessed.
`datasets/layout_preferences/` holds the evidence and the reasoning as a frozen record.

## The four validation layers

Each layer catches bugs the others cannot see.

| Layer                  | What it checks                                                                                    | When                                |
| ---------------------- | ------------------------------------------------------------------------------------------------- | ----------------------------------- |
| **Layout oracle**      | Graph geometry after layout (needs graph structure to interpret coordinates)                      | Every topology test                 |
| **Routing invariants** | Edge waypoints as each route is computed (catches bad paths immediately)                          | Always-on during routing            |
| **Phase guards**       | Layout engine pre/post-conditions at each phase boundary (pinpoints which phase introduced a bug) | Always-on per phase                 |
| **Render oracle**      | Finished SVG as drawn (catches problems that only emerge from the pixel output)                   | Opt-in CLI flag, corpus pytest gate |

### Layer 1 - Layout oracle (`tests/layout_validator.py`)

**What it does**: once the layout engine has assigned coordinates to every station, port, and edge, this layer inspects the result and flags geometric violations.
It runs against the in-memory graph rather than the drawn SVG.
That gives it the full context: which nodes are ports and which are stations, which lines share a bundle, and where the section boundaries are.
That context lets it check things a raw SVG parser cannot, such as whether an edge waypoint stays inside the section it should pass through, or whether a port lands on the correct face of its section.

**What it catches uniquely**: section overlap, a station outside its section box, a station used as an elbow (a geometry invariant that requires knowing which node is a station and which is a port), a port off its boundary, edge waypoints straying out of bounds, and route-crosses-section-box violations.

**How it's wired**: `check_*` functions in `tests/layout_validator.py` take a laid-out graph and return `Violation` objects with `ERROR` or `WARNING` severity.
`tests/test_topology_validation.py` runs all of them against every topology fixture.
`ERROR`s fail CI.
`WARNING`s are reported but do not.

### Layer 2 - Routing invariants (`src/nf_metro/layout/routing/invariants.py`)

**What it does**: checks each edge's route as soon as it is computed, before the SVG is written.
This is the earliest point at which a routing bug can be caught, at the level of the raw waypoint list for a single edge.

**What it catches uniquely**: path-level problems that need no graph context to diagnose, such as a near-horizontal diagonal that should be 45° but drifts, a missing curve, or a waypoint that places a path inside a section it should pass around.
These can only surface here, because the layout oracle runs after all edges are done and the render oracle reads the drawn artifact, where individual waypoints are no longer visible.

**How it's wired**: the `CHECK_REGISTRY` runs at the end of every call to `route_edges`.
Tier-A checks are always-on and abort rendering if they fail.
Tier-B checks are either issue-pinned (used to track known defects against the corpus) or conditional (fire only under a specific routing arm).

### Layer 3 - Phase guards (`src/nf_metro/layout/phases/guards.py`)

**What it does**: the layout engine runs as a sequence of ~40 numbered phases, such as grid placement, port inference, and coordinate assignment.
Phase guards are assertions inserted at the boundaries of those phases to check that each one left the graph in a valid state.
When a guard fires, the phase name is in the error.
A regression is therefore localized to the phase that broke the invariant, rather than surfacing as an unexplained geometry error at render time.

**What it catches uniquely**: mid-pipeline state corruption that neither the layout oracle nor the routing invariants can see.
The layout oracle runs after all phases, and the routing invariants run after routing rather than layout.
One guard checks that phases which should not touch port coordinates leave them unaltered.

**How it's wired**: `GUARD_REGISTRY` and `INLINE_GUARD_REGISTRY` record every guard with its narrow reason and its classification as always-on, defensive or issue-pinned.
Always-on guards execute every time their phase runs.
Issue-pinned guards fire once per corpus run through `tests/test_guard_registry.py` and are marked `XFAIL`.
When the underlying issue is fixed, CI turns red until the pin is removed.

### Layer 4 - Render oracle (`src/nf_metro/render/validate.py`)

**What it does**: parses the finished SVG as an outside consumer would, with no access to the in-memory graph and only the drawn lines and text to work from.
That mirrors how a visual regression shows up.
The SVG is wrong, and the artifact alone has to explain why.

**What it catches uniquely**: geometry bugs that only emerge in the final pixel output.
The layout engine may compute positions that do not overlap in graph coordinates.
Once font metrics, stroke widths, and SVG transforms are applied, a station label can end up sliced by a route polyline.
Two lines assigned distinct offsets can also be drawn flush, because a rounding step collapsed them.
Neither the layout oracle nor the routing invariants can see this, because both run before the SVG is produced.

**How it's wired**: `validate_render(svg, *, plan=None)` checks for label strikes, where a route polyline crosses a station label, and marker crossings, where a route passes through a node marker it does not serve.
When the render plan is supplied, it also checks for offset collapse, where lines are drawn flush despite being assigned distinct offsets.
Enable it with `nf-metro render --validate` or `nf-metro validate-svg --geometry`.
A corpus-wide pytest gate runs it against every fixture.
