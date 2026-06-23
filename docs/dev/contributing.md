# Contributing

This page covers the development workflow for nf-metro: how to set up, run checks, add tests, and submit changes.

## Setup

```bash
git clone https://github.com/pinin4fjords/nf-metro
cd nf-metro
pip install -e ".[dev]"
```

Required Python: 3.10+. Dependencies: `click`, `drawsvg`, `networkx`, `pillow`. Dev extras add `pytest`, `ruff`, `mypy`.

## Running checks

```bash
# All tests
pytest

# Single test file or case
pytest tests/test_topology_validation.py
pytest tests/test_parser.py::test_parse_title

# Lint (match CI exactly)
ruff check src/ tests/
ruff format --check src/ tests/

# Type checking
mypy src/
```

The topology validation suite parametrizes over every `.mmd` in `examples/topologies/` and runs the full layout oracle against each one. This is the most sensitive regression check.

## Before opening a PR

1. Run `pytest` and `ruff format --check` - CI will run both.
2. Render the gallery locally and eyeball any fixtures your change touches (see [Visual review](#visual-review) below).
3. If you added new behaviour, add a topology fixture or regression fixture that covers it.
4. If you fixed a known bug that had an `xfail` marker, remove the marker from the test.

## Working with the layout phases

The layout engine runs as a sequence of ~40 numbered phases. Each phase reads the state left by the previous one and writes into it. The contracts between phases - what each one requires on entry and guarantees on exit - are documented in [`src/nf_metro/layout/CONTRACT.md`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md). Read the relevant section before modifying a phase; changes to coordinate assignment often have non-local effects.

Phase guards in `src/nf_metro/layout/phases/guards.py` assert pre/postconditions at each phase boundary. When a guard fires, the phase name is in the error message, which localises the regression immediately. If you add a new invariant that a phase must preserve, add a guard for it here.

## Adding a topology test

`examples/topologies/` holds `.mmd` fixtures, each isolating a specific graph topology (fan-out, fan-in, diamond, fold, etc.). `tests/test_topology_validation.py` parametrizes over every file in that directory and runs the full layout oracle against each one.

To add a case:

1. Write a minimal `.mmd` that exercises the topology.
2. Drop it in `examples/topologies/`. No further wiring is needed; the parametrization picks it up automatically.
3. Run `pytest tests/test_topology_validation.py` to confirm it passes (or fails, if you are pinning a known issue).

If the fixture exercises a bug you have not fixed yet, use an `xfail` marker rather than skipping it. See the [xfail pattern](#the-xfail-ratchet) below.

## Adding a layout invariant

`tests/layout_validator.py` holds `check_*` functions that take a laid-out `MetroGraph` and return a list of `Violation` objects with `ERROR` or `WARNING` severity. `ERROR`s fail CI; `WARNING`s are reported but do not.

To add a check:

1. Write a new `check_<thing>` function returning a list of `Violation` objects.
2. Call it from the relevant test - usually the topology suite in `test_topology_validation.py`, or a dedicated test if the check is narrow.

Per-phase preconditions, postconditions, and invariants are tracked separately in `CONTRACT.md` and enforced by phase guards, not layout oracle checks.

## The xfail ratchet

When you find a bug that will take time to fix, do not ignore it. Write a test that checks the correct behaviour and mark it `xfail`:

```python
@pytest.mark.xfail(strict=True, reason="issue #NNN: description of what should happen")
def test_something():
    ...
```

While the bug is present, `xfail` keeps CI green and the test documents what is wrong. When someone fixes the bug, the test flips to `XPASS` and CI turns red, prompting the developer to remove the `xfail` marker and lock in the correct behaviour. The floor cannot slip backwards by accident.

The corollary: once a check is in, it stays in. Do not remove checks to make CI pass. If a check fires incorrectly, fix the check or the code; don't delete it.

## Adding a routing handler

The routing module uses a first-match dispatcher over handler families in `src/nf_metro/layout/routing/`. Each handler covers a specific combination of section orientation, entry/exit direction, and flow type. The full dispatch table is documented in [`docs/dev/inter_section_dispatch.md`](inter_section_dispatch.md).

To add a handler for a new case:

1. Write a topology fixture that exercises it.
2. Confirm the current dispatcher falls through to an error or produces wrong output.
3. Add a handler in the appropriate module, or extend an existing one.
4. Add an entry to the gate coverage matrix in `docs/dev/routing_gate_coverage.md` so the new arm is tracked.

## Visual review

Automated geometry checks verify that coordinates are correct. They cannot verify that the result looks right. For layout or rendering changes, visual review is essential.

**Via CI (preferred):** push to a PR. The workflow at `.github/workflows/pr-renders.yml` renders the gallery on both the PR branch and the base, builds a side-by-side before/after for every SVG that changed, and publishes the diff at:

```
https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/
```

Scroll through the before/afters and confirm nothing regressed.

**Locally:** render individual examples directly:

```bash
# Activate the nf-metro dev environment if using micromamba
source ~/.local/bin/mm-activate nf-metro

# Render an SVG (--no-chrome-css bakes concrete colours for cairosvg)
python -m nf_metro render examples/rnaseq_sections.mmd -o /tmp/out.svg --no-chrome-css

# Convert to PNG for easier review
python -c "import cairosvg; cairosvg.svg2png(url='/tmp/out.svg', write_to='/tmp/out.png', scale=2)"
open /tmp/out.png
```

To batch-render the full topology library:

```bash
python scripts/render_topologies.py
# output goes to /tmp/nf_metro_topology_renders/
```

## Station-as-elbow constraint

**Never position a perpendicular port at the same coordinate as an internal station.** This is a hard invariant enforced by `check_station_as_elbow` in `tests/layout_validator.py` (10px tolerance).

- TOP/BOTTOM ports on LR/RL sections must not share X with any internal station.
- LEFT/RIGHT ports on TB sections must not share Y with any internal station.

Do not "fix" a routing kink by moving a port to match a station's coordinate. That makes a station the inflection point of a curve, which looks wrong. Accept a small offset between the port and the station and handle it in the routing path instead.

## Commit and CI conventions

- Use `[skip ci]` at the end of the commit subject for work-in-progress pushes. Omit it for the final commit before requesting review, and for any commit that fixes a CI failure.
- Do not write the literal `[skip ci]` marker in the commit body; GitHub Actions scans the full message.

## Filing issues

When you trip over a bug mid-task, file a detailed issue rather than trying to fix it immediately. Include: a minimal `.mmd` that reproduces the problem, what the output looks like versus what you expected, and which topology category it belongs to (if known). That record is what makes the bug fixable by someone with no context on the original task.
