# Environment and commit hooks

## Reuse one persistent env, don't create one per issue

nf-metro is pure Python; the deps (drawsvg, networkx, pillow, resvg-py,
pytest, ruff, mypy, `types-networkx`) change rarely. Creating a fresh
`micromamba` env per issue re-solves and re-downloads all of that every session
for no benefit. Keep **one** long-lived deps env and point it at the worktree's
code per-command:

```bash
# One-time, reused across all issues (skip if it already exists):
ulimit -n 1000000 && export CONDA_OVERRIDE_OSX=15.0 && /opt/homebrew/bin/micromamba create -n nf-metro-dev python=3.11 -y
source ~/.local/bin/mm-activate nf-metro-dev
cd ~/projects/nf-metro && pip install ".[dev,docs]"
# Install from pyproject, never a hand-listed set: `lark` is a hard runtime
# dependency and `coverage` is needed by the gate ratchet, and a hand-written
# list drifts silently. Non-editable on purpose - PYTHONPATH shadows it below.
# Refresh this env only when pyproject deps actually change.
```

Then run the worktree's code by prepending its `src/` to `PYTHONPATH` on each
command - **do not** `pip install -e` the worktree into this env:

```bash
source ~/.local/bin/mm-activate nf-metro-dev
cd /tmp/nf-metro-fix-<N>
export PYTHONPATH=/tmp/nf-metro-fix-<N>/src
python -m nf_metro render <file.mmd> -o /tmp/out.png
python -m pytest -k <selector>
```

CLAUDE.md now recommends `PYTHONPATH` over an editable install, which agrees
with this file. fix-issue work uses `nf-metro-dev`. Note that the
`render-topologies` skill brings its own per-issue editable env, so a renderer
briefed to use it is a deliberate exception to the rule below.

**Why per-command `PYTHONPATH`, not editable install:** an editable install binds
one env's `site-packages` to exactly one worktree path, so it collides the moment
you run two worktrees in parallel. `PYTHONPATH` is set per command and shadows
whatever is installed, so any number of parallel worktree sessions share the
single `nf-metro-dev` env with zero cross-talk. (If you genuinely want an
isolated editable install for one worktree, dedicate a *separate* env to it -
never editable-install a shared env against a worktree.)

Python 3.11 is the pinned interpreter for the routing/TB ratchets; those tests
skip silently off it (see [gate-ratchet.md](gate-ratchet.md)).

Take a corpus A/B render diff in one script invocation or one continuous
worker turn: the shared `nf-metro-dev` env can be updated by a concurrent
session between two separately-dispatched measurement passes, producing a
near-total false diff that costs a full re-measurement to disprove.

## Never `git stash` in a fix-issue worktree

`refs/stash` is shared across every worktree of this repo, so a `pop` here can
hand back a concurrent session's entry instead of yours, conflicting against
changes you never made. Use a throwaway commit instead.

## Commit hooks

Hooks need the tools on `PATH` in the same Bash call: the repo uses `prek`
(config `prek.toml`, not `pre-commit`), whose `mypy` hook is `language: system`
and so needs `mypy` on `PATH`. Shell state does not persist between Bash calls,
so run the commit as one call with the env activated:

```bash
source ~/.local/bin/mm-activate nf-metro-dev && cd <worktree> && \
  git commit ...
```

Never skip hooks with `--no-verify`. Run hooks on the changed files, which is
what the commit itself does anyway:

```bash
source ~/.local/bin/mm-activate nf-metro-dev && cd <worktree> && \
  git diff --cached --name-only -z | xargs -0 -r prek run --files
```

Activate rather than `micromamba run`: the `micromamba-env` skill explains why
`run` must be avoided on this machine. `prek`'s `mypy` hook is
`language: system`, so it needs the env that carries stub-complete `mypy`.

`prek run --all-files` sweeps the entire repo, including a cold `mypy` over all
of `src/`. Reserve it for a change to the hook config itself or to a
repo-wide generated artifact; it is not the default.

## Verifier environment

One script, which creates the frozen checkout, asserts against it, and removes
it in the same process. `set -e` fires in a script, so its guards bite without
being written defensively.

```bash
# Paths are relative to the worktree root; cd there first or use an absolute path.
cd /tmp/nf-metro-fix-<N>
source ~/.local/bin/mm-activate nf-metro-dev
.claude/skills/fix-issue/scripts/verify_candidate.sh --candidate <SHA> --selector "<fixture-or-invariant>"
```

It prints `VERIFY OK <SHA>` on success. **An absent `VERIFY OK` is a failure**,
whatever the exit status looked like. Report the last line, the failing command
and a short excerpt; never the full output. Carries `--self-test`.

## Local renders

Both of these belong in a LIGHT read-only worker that returns the verdict and
the artifact path, not the imagery, unless a genuine visual question needs the
picture in front of a HIGH reviewer. Neither replaces the CI gallery review.

Fast sanity check of one specific `.mmd` before pushing:

```bash
source ~/.local/bin/mm-activate nf-metro-dev
export PYTHONPATH=/tmp/nf-metro-fix-<N>/src
cd /tmp/nf-metro-fix-<N> && python -m nf_metro render <file.mmd> -o /tmp/<name>.png
open /tmp/<name>.png
```

For a before/after sweep before pushing, brief the worker to use the tracked
`.claude/skills/render-topologies` skill.
