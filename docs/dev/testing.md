# Testing

The test suite has three layers, each adding a different kind of
regression guard.  Run everything with `pytest`; run one file or test
with the usual selectors:

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
