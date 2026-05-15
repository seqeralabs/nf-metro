---
name: fix-issue
description: End-to-end workflow for fixing GitHub issues on the nf-metro repo. Use when the user references a GitHub issue (by number, URL, or description) and wants it fixed. Handles worktree setup, environment creation, implementation, testing, visual review, and PR creation. Trigger on phrases like "fix issue #N", "address #N", "work on issue N", or any request to fix a bug or implement a feature that references an issue.
---

# Fix Issue

Structured workflow for fixing nf-metro GitHub issues in an isolated worktree.

**Conventions** (substitute if your setup differs):
- Local nf-metro checkout: `~/projects/nf-metro`
- Issues + PRs target the canonical upstream `pinin4fjords/nf-metro`. If
  you're working from a fork, resolve the owner with
  `gh repo view --json owner -q .owner.login`.
- micromamba: `/opt/homebrew/bin/micromamba` (macOS Apple Silicon codesign
  workaround). On other platforms, just `micromamba` if it's on PATH.

## Phase 1: Understand the Issue

```bash
gh issue view <N> --repo pinin4fjords/nf-metro
```

Summarize the problem and proposed approach. Wait for user confirmation before proceeding.

## Phase 2: Worktree + Environment Setup

```bash
# Worktree
cd ~/projects/nf-metro
git fetch origin main
git worktree add /tmp/nf-metro-fix-<N> -b fix/<N>-<slug> origin/main

# Fix environment
ulimit -n 1000000 && export CONDA_OVERRIDE_OSX=15.0 && /opt/homebrew/bin/micromamba create -n nf-metro-fix-<N> python=3.11 cairo -y
source ~/.local/bin/mm-activate nf-metro-fix-<N>
pip install -e "/tmp/nf-metro-fix-<N>[docs]"
```

All subsequent work happens inside `/tmp/nf-metro-fix-<N>`.

## Phase 3: Implement the Fix

The shell cwd resets after each Bash call. Always chain `cd` into the worktree:

```bash
source ~/.local/bin/mm-activate nf-metro-fix-<N> && cd /tmp/nf-metro-fix-<N> && ruff format src/ tests/ && ruff check src/ tests/ && pytest
```

Fix any failures before proceeding.

## Phase 4: Visual Review

### Primary method: CI render preview (recommended)

Push the branch and create a PR. The CI workflow (`.github/workflows/pr-renders.yml`) automatically renders all gallery examples on both the PR branch and base, generates a before/after visual diff page, and posts a sticky comment on the PR with the preview link:

```
https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/
```

This is the authoritative visual review. After creating the PR in Phase 5, point the user to the render preview link for review.

### Optional: quick local render of a single file

For a fast sanity check of one specific `.mmd` file before pushing, render it locally:

```bash
source ~/.local/bin/mm-activate nf-metro-fix-<N>
cd /tmp/nf-metro-fix-<N> && python -m nf_metro render <file.mmd> -o /tmp/<name>.svg
python -c "import cairosvg; cairosvg.svg2png(url='/tmp/<name>.svg', write_to='/tmp/<name>.png', scale=2)"
open /tmp/<name>.png
```

This is useful for quick iteration but does not replace the full CI gallery review.

### Optional: local before/after comparison

If you need a before/after comparison before pushing (e.g. risky change, user wants early feedback), use the `/render-topologies` skill.

## Phase 5: Commit and PR

Once tests pass:

```bash
cd /tmp/nf-metro-fix-<N>
gh pr create --repo pinin4fjords/nf-metro --base main --title "<title>" --body "$(cat <<'EOF'
## Summary
<bullets>

Fixes #<N>

## Test plan
- [ ] pytest passes
- [ ] ruff check clean
- [ ] Visual review of [render preview](https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After CI posts the render preview link, ask the user to review it.

## Phase 6: Cleanup

Offer to clean up (only if user agrees):

```bash
cd ~/projects/nf-metro
git worktree remove /tmp/nf-metro-fix-<N>
/opt/homebrew/bin/micromamba env remove -n nf-metro-fix-<N> -y
```
