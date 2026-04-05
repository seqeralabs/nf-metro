---
name: render-topologies
description: Local visual regression check for layout or rendering changes. Renders all gallery examples, pixel-diffs against main, and opens changed renders as BEFORE/AFTER pairs. In most cases the CI render preview on a PR is sufficient - use this skill only for pre-push confidence on risky changes or when the user explicitly asks for a local diff.
disable-model-invocation: true
allowed-tools: Bash(rm -rf *), Bash(python *), Bash(open *), Bash(cd *), Bash(git *), Bash(source *), Bash(pip *), Bash(cp *)
---

# Render Topologies

Local pixel-diff of all gallery renders between the current branch and `origin/main`. Uses `scripts/build_gallery.py` (the same script CI runs), so local results match the PR render preview.

**In most cases you don't need this.** Push to a PR and review the CI-generated render preview at `https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/` instead. Use this skill only when:
- You want pre-push confidence before creating a PR
- The user explicitly asks for a local visual comparison
- You're iterating on a change and want fast feedback without pushing

## Step 1: Detect context

Determine the working mode:

- **Worktree mode**: A worktree exists at `/tmp/nf-metro-fix-<N>` with a matching `nf-metro-fix-<N>` env. Use the worktree path and env for branch renders.
- **Standalone mode**: Working from the main repo at `/Users/jonathan.manning/projects/nf-metro`. Use the `nf-metro` env for branch renders. Stash or commit any uncommitted changes first.

## Step 2: Render baseline from main

Update the main repo checkout and render using the shared `nf-metro-main` baseline environment. Install with `[docs]` extras (same as CI).

```bash
cd /Users/jonathan.manning/projects/nf-metro && git fetch origin main && git checkout main && git pull origin main
source ~/.local/bin/mm-activate nf-metro-main && pip install -e "/Users/jonathan.manning/projects/nf-metro[docs]" -q
cd /Users/jonathan.manning/projects/nf-metro && python scripts/build_gallery.py
# SVGs are in docs/assets/renders/ → copy to a baseline dir
rm -rf /tmp/nf_metro_renders_main && mkdir -p /tmp/nf_metro_renders_main
cp docs/assets/renders/*.svg /tmp/nf_metro_renders_main/
```

Convert SVGs to PNGs for pixel diffing:

```bash
source ~/.local/bin/mm-activate nf-metro-main
python -c "
import cairosvg
from pathlib import Path
for svg in sorted(Path('/tmp/nf_metro_renders_main').glob('*.svg')):
    cairosvg.svg2png(url=str(svg), write_to=str(svg.with_suffix('.png')), scale=2)
"
```

## Step 3: Render from the current branch

Switch back to the branch first (standalone mode) or use the worktree path:

```bash
# Standalone: switch back to the branch
cd /Users/jonathan.manning/projects/nf-metro && git checkout <branch-name>
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

Convert SVGs to PNGs:

```bash
python -c "
import cairosvg
from pathlib import Path
for svg in sorted(Path('/tmp/nf_metro_renders_branch').glob('*.svg')):
    cairosvg.svg2png(url=str(svg), write_to=str(svg.with_suffix('.png')), scale=2)
"
```

## Step 4: Diff and open changed renders

Clean previous comparison files, diff each PNG pair, copy changed renders with sequential numbering (BEFORE/AFTER adjacent when flipping with arrow keys):

```python
from PIL import Image, ImageChops
import os, glob, shutil

# Clean previous comparison files
for f in glob.glob("/tmp/*_BEFORE.png") + glob.glob("/tmp/*_AFTER.png"):
    os.remove(f)

main_dir = "/tmp/nf_metro_renders_main"
branch_dir = "/tmp/nf_metro_renders_branch"

pngs = sorted(f for f in os.listdir(branch_dir) if f.endswith('.png'))
changed = []
for name in pngs:
    path_main = os.path.join(main_dir, name)
    if not os.path.exists(path_main):
        changed.append(name)  # new file
        continue
    im_b = Image.open(os.path.join(branch_dir, name))
    im_m = Image.open(path_main)
    if im_b.size != im_m.size or ImageChops.difference(im_b, im_m).getbbox():
        changed.append(name)

print(f"{len(changed)} changed, {len(pngs) - len(changed)} unchanged")
for i, name in enumerate(changed):
    stem = name.replace('.png', '')
    idx = i * 2 + 1
    main_path = os.path.join(main_dir, name)
    if os.path.exists(main_path):
        shutil.copy(main_path, f"/tmp/{idx:02d}_{stem}_BEFORE.png")
    shutil.copy(os.path.join(branch_dir, name), f"/tmp/{idx+1:02d}_{stem}_AFTER.png")
    print(f"  {name}")
```

**IMPORTANT**: Write the diff script to a file (`/tmp/diff_renders.py`) and run it with `python3`, rather than inlining Python in the shell. Inline Python with `!=` gets mangled by shell escaping.

```bash
# Open all pairs sorted so BEFORE/AFTER interleave correctly (skip if zero changed)
ls /tmp/[0-9][0-9]_*_BEFORE.png /tmp/[0-9][0-9]_*_AFTER.png | sort | xargs open -a Preview
```

## Step 5: Report

- Count of changed vs unchanged renders
- List changed file names
- If zero changed, say so and skip Preview

## Notes

- Render script: `scripts/build_gallery.py` (same as CI PR render preview workflow).
- Nextflow fixtures (`tests/fixtures/nextflow/*.mmd`) are included in the gallery.
- Baseline always uses `nf-metro-main` env + main repo updated to `origin/main`.
- Install with `[docs]` extras (not `[dev]`) to match CI dependencies.
- After a PR is created, CI renders the authoritative diff at `https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/`.
- To render a single file: `python -m nf_metro render <file.mmd> -o /tmp/output.svg`
