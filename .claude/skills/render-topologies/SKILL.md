---
name: render-topologies
description: Local visual regression check for layout or rendering changes. Renders all gallery examples, pixel-diffs against main, and opens changed renders as BEFORE/AFTER pairs. In most cases the CI render preview on a PR is sufficient - use this skill only for pre-push confidence on risky changes or when the user explicitly asks for a local diff.
disable-model-invocation: true
allowed-tools: Bash(rm -rf *), Bash(python *), Bash(open *), Bash(cd *), Bash(git *), Bash(source *), Bash(pip *), Bash(cp *)
---

# Render Topologies

Local pixel-diff of all gallery renders between the current branch and `origin/main`. Uses `scripts/build_gallery.py` (the same script CI runs), so local results match the PR render preview.

**Conventions** (substitute if your setup differs):
- Local nf-metro checkout: `~/projects/nf-metro`
- Baseline env: `nf-metro-main`; branch env: `nf-metro` (or `nf-metro-fix-<N>` in worktree mode)
- CI render preview is published at the upstream's GitHub Pages site
  (`pinin4fjords.github.io/nf-metro/_pr/<N>/`); if you ship from a fork
  with Pages enabled, the URL will track your fork's owner

**In most cases you don't need this.** Push to a PR and review the CI-generated render preview at `https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/` instead. Use this skill only when:
- You want pre-push confidence before creating a PR
- The user explicitly asks for a local visual comparison
- You're iterating on a change and want fast feedback without pushing

## Step 1: Detect context

Determine the working mode:

- **Worktree mode**: A worktree exists at `/tmp/nf-metro-fix-<N>` with a matching `nf-metro-fix-<N>` env. Use the worktree path and env for branch renders.
- **Standalone mode**: Working from the main repo at `~/projects/nf-metro`. Use the `nf-metro` env for branch renders. Stash or commit any uncommitted changes first.

## Step 2: Render baseline from main

Update the main repo checkout and render using the shared `nf-metro-main` baseline environment. Install with `[docs]` extras (same as CI).

```bash
cd ~/projects/nf-metro && git fetch origin main && git checkout main && git pull origin main
source ~/.local/bin/mm-activate nf-metro-main && pip install -e "$HOME/projects/nf-metro[docs]" -q
cd ~/projects/nf-metro && python scripts/build_gallery.py
# SVGs are in docs/assets/renders/ → copy to a baseline dir
rm -rf /tmp/nf_metro_renders_main && mkdir -p /tmp/nf_metro_renders_main
cp docs/assets/renders/*.svg /tmp/nf_metro_renders_main/
```

## Step 3: Render from the current branch

Switch back to the branch first (standalone mode) or use the worktree path:

```bash
# Standalone: switch back to the branch
cd ~/projects/nf-metro && git checkout <branch-name>
source ~/.local/bin/mm-activate nf-metro && pip install -e ".[docs]" -q
python scripts/build_gallery.py
rm -rf /tmp/nf_metro_renders_branch && mkdir -p /tmp/nf_metro_renders_branch
cp docs/assets/renders/*.svg /tmp/nf_metro_renders_branch/

# Worktree: use worktree path and env
source ~/.local/bin/mm-activate nf-metro-fix-<N> && pip install -e "/tmp/nf-metro-fix-<N>[docs]" -q
cd /tmp/nf-metro-fix-<N> && python scripts/build_gallery.py
rm -rf /tmp/nf_metro_renders_branch && mkdir -p /tmp/nf_metro_renders_branch
cp /tmp/nf-metro-fix-<N>/docs/assets/renders/*.svg /tmp/nf_metro_renders_branch/
```

## Step 4: Diff and open the report

Use `scripts/build_render_diff.py` — the same script the CI render-preview
workflow uses — to compare the two SVG directories. It writes a
self-contained HTML page (`index.html`) showing side-by-side before/after
for changed examples only, grouped by section.

```bash
cd ~/projects/nf-metro
rm -rf /tmp/render_diff && mkdir -p /tmp/render_diff
python scripts/build_render_diff.py \
  /tmp/nf_metro_renders_main \
  /tmp/nf_metro_renders_branch \
  /tmp/render_diff
open /tmp/render_diff/index.html
```

Exit codes: `0` = changes detected and report written, `2` = no changes
(skip `open`), `1` = error. The report works directly on SVGs, so no
PNG conversion is needed.

## Step 5: Report

- Whether any examples changed (the script's exit code tells you)
- For changed examples, point the user at `/tmp/render_diff/index.html`
- If zero changed, say so and skip `open`

## Notes

- Render script: `scripts/build_gallery.py` (same as CI PR render preview workflow).
- Diff script: `scripts/build_render_diff.py` (also matches CI), so local results match the PR render preview.
- Nextflow fixtures (`tests/fixtures/nextflow/*.mmd`) are included in the gallery.
- Baseline always uses `nf-metro-main` env + main repo updated to `origin/main`.
- Install with `[docs]` extras (not `[dev]`) to match CI dependencies.
- After a PR is created, CI renders the authoritative diff at `https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/`.
- To render a single file: `python -m nf_metro render <file.mmd> -o /tmp/output.svg`
