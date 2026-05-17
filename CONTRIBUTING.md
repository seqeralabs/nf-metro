# Contributing to nf-metro

nf-metro accepts contributions via pull requests against `main`. The project relies on a tight feedback loop between code changes and rendered output, so a few conventions exist to keep that loop honest.

## Visual review is the contract

For any change that touches `src/nf_metro/layout/`, `src/nf_metro/render/`, or routing, the authoritative review is the visual diff produced by `.github/workflows/pr-renders.yml`. The workflow renders the gallery on both the PR branch and `main`, and posts a link to a before/after preview site at `https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/`. Open the link and look at every changed render before requesting human review. If you cannot defend a delta as an improvement or a neutral consequence, narrow the change.

For purely additive changes (new tests, new fixtures, new documentation), the render diff should be empty. Verify locally with:

```bash
python scripts/build_gallery.py --debug
python scripts/build_render_diff.py <baseline-dir> docs/assets/renders <diff-out>
```

The diff script returns exit code 2 on "no changes" and 0 on "changes detected" - exit 2 signals "no diff page worth publishing" so CI can skip the upload step.

## Layout invariants and the ratchet

`tests/test_layout_invariants.py` is the project's growing collection of programmatic layout invariants. Treat it as a ratchet:

- Every visible layout regression that lands a fix should also land a generic invariant (parametrized over the fixture corpus) that would have caught it.
- When you add an invariant that fires on a known-broken fixture, add the fixture to the relevant `_XFAIL_<NAME>` dict with a clear reason and a GitHub issue number. Do not skip the parametrization to make the test pass.
- When you fix the underlying bug, drop the xfail; the test will turn green, and the regression won't silently reopen because the parametrization is strict.

`src/nf_metro/layout/CONTRACT.md` documents per-phase preconditions and postconditions for `_compute_section_layout`. If you add or modify a phase, update the contract in the same PR.

## Runtime validators

`src/nf_metro/layout/engine.py` exposes `_guard_*` functions gated by `compute_layout(graph, validate=True)`. CLI users get the default (`validate=False`); guards run primarily in tests. When you add an invariant, consider whether a matching runtime guard would catch the same class of bug earlier than CI - several guards in `engine.py` already mirror CI tests one-to-one.

## Test fixtures

- `examples/` holds gallery fixtures (real nf-core pipelines and synthetic guide examples). Renders from this directory are included in the gallery and tracked by the PR-render workflow.
- `examples/topologies/` holds synthetic structural-stress fixtures. Each fixture aims to trigger one specific topology class (multi-input convergence, U-turn fold, off-track convergence, etc.). Keep them small and single-purpose.
- `tests/fixtures/` holds fixtures consumed by specific tests rather than by gallery rendering. They are not included in the gallery, but they do participate in invariant parametrization (see below).

`tests/test_layout_invariants.py` discovers all `%%metro`-format fixtures in `examples/` and `tests/fixtures/` automatically via `_discover_fixtures()`. New fixtures appear in every parametrized invariant by default.

## Git hygiene

### Branch and history discipline

- Do not force-push to shared branches (including any PR branch under review). The default policy is **never force-push**.
- Prefer additive commits over rewriting history. Fixing a CI failure means a new commit, not an `--amend`.
- Avoid interactive rebase on branches that have been pushed.

### Stacked PR chains

When working a series of dependent PRs:

- Choose the chain's root branch once, at the start. If `main` advances during the chain, **merge** main into the root (don't rebase the root onto main).
- A PR rejected from the chain should be **reverted** rather than rebased out. Reverting on `main` produces a stable history that downstream chain branches can merge cleanly. Rebasing out of a rejected commit forces every downstream branch to revert it independently, which is what triggered the multi-revert pattern documented in issue #323 section 8.
- When a PR in the chain merges, re-target the next PR's base to `main` **before** the merged branch is deleted. Otherwise GitHub auto-closes the downstream PR.

### Commit messages and `[skip ci]`

- Append `[skip ci]` to short-line commit messages during iterative WIP work to avoid burning CI on every push.
- Drop `[skip ci]` for: the final commit before requesting review, commits fixing a known CI failure, and any commit landing on `main`.
- Use Conventional Commits prefixes (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`, `style:`) for grep-ability in the log.

## Local development

```bash
# Install in editable mode (one-shot per environment)
pip install -e ".[dev]"

# Run the full test suite (fast)
pytest tests/

# Lint and format
ruff check src/ tests/
ruff format --check src/ tests/

# Render a single fixture
nf-metro render examples/rnaseq_sections.mmd -o /tmp/rnaseq.svg

# Render with debug overlay (shows layout grid, station ids, bypass markers)
nf-metro render examples/rnaseq_sections.mmd --debug -o /tmp/rnaseq.svg

# Build the full gallery (writes docs/assets/renders/)
python scripts/build_gallery.py --debug
```

When working on multiple branches concurrently, use `git worktree add` for each branch rather than swapping with `git checkout` - the editable install means `src/` changes are live, and a checkout swap can briefly leave the env pointing at the wrong code.

## Reporting issues

File issues against the GitHub project. For visible layout regressions, include:

- The fixture (`.mmd` file or link to a gallery example).
- A screenshot or `--debug` SVG showing the regression.
- The nearest known-good commit if you have one (`git bisect` is supported here).

For new feature requests, a small `.mmd` example fixture demonstrating what you'd like to see usually shortcuts the design discussion considerably.
