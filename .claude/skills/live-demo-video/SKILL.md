---
name: live-demo-video
description: Re-record the embedded live-progress demo clip (website/public/assets/live_demo.mp4, shown near the top of docs/live.mdx). Use when the video looks stale (old overlay style, old theme, a layout/label fix it doesn't reflect yet), or when asked to "update the live demo video", "re-record the demo gif/video", "refresh the live progress clip", or similar. Covers the micromamba env with Nextflow + an editable nf-metro install, the two-process (server + pipeline) Playwright recording setup, the SSE/networkidle and light-dark/colorScheme gotchas, and converting the recorded webm to the committed mp4 without needing system ffmpeg.
allowed-tools: Bash(nf-metro *), Bash(nextflow *), Bash(micromamba *), Bash(node *), Bash(scripts/convert_demo_video.sh*), Bash(git *), Bash(curl *), Bash(pkill *), Bash(ps *)
---

# Re-record the live-progress demo video

`docs/live.mdx` embeds a short clip of `nf-metro serve` lighting up a map in
real time. The actual file is **`website/public/assets/live_demo.mp4`**, not
`docs/assets/` - the `../assets/live_demo.mp4` reference in the MDX is rewritten
to that public path by `remarkRebaseLinks` (see `website/astro.config.mjs`),
because it's a static asset, not a navigable page.

There is no existing recording tooling for this in the repo history - the
clip has always been made by hand. This skill replaces that with a
reproducible Playwright recording, because a manual screen capture can't be
re-run identically when the map or overlay styles change again.

## Before you start

- **Use a worktree** (per the repo's own git-safety rules) - this touches a
  committed binary asset in the main checkout otherwise.
- Check `docs/live.mdx` for the overlay style and theme you actually want
  before recording - don't assume the previous clip's look was intentional.
  (This skill was first used to swap a `led` recording for the default
  `ring` style; the CLI's `--overlay`/`--theme` help and `docs/live.mdx`'s
  "Overlay styles" section describe what each looks like.)

## Step 1: An env with Nextflow and an editable nf-metro

The recording drives the real `examples/live/` demo pipeline (toy processes
that only `sleep`), so you need both `nextflow` and `nf-metro` on `PATH`,
with `nf-metro` importing the checkout you're recording from - not whatever
released version happens to be on `PATH`.

Check for a `nf-metro-demo` micromamba env first (`micromamba env list`) -
one was set up for exactly this and already has both:

```bash
micromamba list -n nf-metro-demo | grep -E "nextflow|nf-metro"
micromamba run -n nf-metro-demo pip show nf-metro | grep Location
```

An editable install's `.pth` file points at one fixed checkout path, so if
that worktree has since been pruned (`pip show` names a path that no longer
exists), override it rather than reinstalling:

```bash
source /Users/jonathan.manning/micromamba/etc/profile.d/micromamba.sh
micromamba activate nf-metro-demo
export PYTHONPATH="$(pwd)/src"   # repo root of the worktree you're recording from
```

No such env, or missing a piece? `pip install -e ".[dev]"` (nf-metro) plus a
Nextflow install (`nf-core`/`nextflow-dev` micromamba envs, or the
`micromamba-env` skill) into any env of your choosing works the same way.

## Step 2: Playwright + Chromium

`website/playground-tests/` already carries `@playwright/test` as a
devDependency (it runs the playground e2e suite), and Chromium is normally
already cached from that suite's own setup. The recorder script below lives
in that directory specifically to reuse that browser and package -
`require()` resolves `node_modules` by walking up from the script's own
path, so keep it there rather than moving it to `scripts/` (Python-only) or
using `NODE_PATH` tricks from elsewhere.

`node_modules/` is git-ignored, so a **fresh worktree** (the usual case here
- see "Before you start") needs its own install, even though the tracked
script and config are already there:

```bash
cd website/playground-tests
npm install --prefer-offline --no-audit --no-fund
```

This resolves from the local npm cache in under a second when nothing's
changed - no network needed. Confirm Chromium itself is cached (a cold
`playwright install` does need network access):

```bash
ls ~/Library/Caches/ms-playwright/ | grep chromium
```

## Step 3: Record

From the repo root, with the env from Step 1 active:

```bash
node website/playground-tests/record-live-demo.js /tmp/live-demo-recording
```

This script (`website/playground-tests/record-live-demo.js`):

1. Starts `nf-metro serve examples/live/pipeline.mmd` itself on a scratch
   port and waits for it to answer.
2. Opens a throwaway (non-recording) page against it to measure the page's
   actual content footprint, then closes that page.
3. Opens a second, recording page in headless Chromium via Playwright sized
   to exactly that footprint, with `recordVideo` set on the browser context -
   **not** a screen capture, so there's no dependency on the actual screen
   resolution or window manager.
4. Waits ~1.5s so the idle map is visible before the run starts (a shorter
   pause reads as if the recording started mid-run).
5. Spawns `nextflow run examples/live/workflow/main.nf -with-weblog ...`
   against the running server.
6. Polls `/state` until `run.state` is `"complete"` (not `"completed"` -
   that's the literal value the state schema uses) or `"error"`, holds two
   more seconds, then closes the browser context to flush the video and
   tears both processes down.

Three gotchas already fixed in the script, worth knowing if you modify it:

- **`waitUntil: "networkidle"` on `page.goto` never resolves.** The live page
  holds an open SSE connection to `/stream`, so the network is never idle.
  Wait for the `.wrap > svg` selector instead.
- **`nf-metro serve` bakes one concrete light/dark mode into the map SVG**;
  it isn't `light-dark()`-aware like the page chrome, which does follow
  `prefers-color-scheme`. Playwright's default `colorScheme` is `light`, so
  an unset context renders a dark toolbar over a light map card - a mismatch
  that isn't how the CLI looks by default in an actual browser tab (which
  usually matches the OS scheme). Force `colorScheme: "dark"` in the browser
  context, or `"light"`, but pick one and make sure the SVG and chrome agree.
- **A fixed, guessed viewport leaves a huge margin of page background around
  a small map.** `header` and `.stage` are both plain block-level `<body>`
  children with no explicit width, so they always stretch to fill whatever
  viewport you pick - measuring their own bounding rects just reports back
  the viewport, not the content. Only their non-stretching descendants
  (`#run`, `.controls`, and `.wrap` - the map card, which carries an inline
  `width`/`height` set by the server from the map's own rendered size) size
  to their actual content, so `measureContentSize()` reads *those* instead
  and sizes the viewport (and hence the recorded video) to match exactly.
  This is also why the size isn't a constant in this script: a different
  `.mmd` renders at a different intrinsic size, and the recording should
  always match whatever that page actually renders, not a number baked in
  when someone last ran this.

Output is a `.webm` under the directory you passed (Playwright's
`recordVideo` only produces webm - there's no mp4 option).

## Step 4: Convert to the committed mp4

```bash
scripts/convert_demo_video.sh /tmp/live-demo-recording/*.webm /tmp/live_demo.mp4
```

This re-encodes to h264/yuv420p (universal `<video>` support, unlike webm on
older Safari), at 2x speed and 15fps - the real run takes ~50s, and the clip
autoplay-loops on the docs page, so a shorter loop reads better; 15fps is
enough for the `ring` style's marching-dash animation without bloating the
file. If `ffmpeg` isn't on `PATH` (it wasn't during the first recording -
`brew install ffmpeg` was avoided in favor of not touching the system
Homebrew for one video), the script falls back to the prebuilt binary the
`imageio-ffmpeg` PyPI package bundles, downloaded into a throwaway venv.

Sanity-check a few frames before committing - extract with the same ffmpeg
binary the conversion script resolved:

```bash
ffmpeg -y -ss 10 -i /tmp/live_demo.mp4 -vframes 1 -update 1 /tmp/frame.png
```

`docs/live.mdx`'s `<video>` embed has no fixed width/height of its own
(`style="width: 100%; max-width: 760px"`), so there's no dimension to match
there - just confirm the frame is tightly cropped to the map + header (no
slab of background margin around it) and that the overlay style/theme match
what you intended in Step "Before you start".

## Step 5: Replace the asset and clean up

```bash
cp /tmp/live_demo.mp4 website/public/assets/live_demo.mp4
git add website/public/assets/live_demo.mp4
git commit -m "docs(live): re-record the demo video with the <style> overlay"
```

`work/`, `.nextflow/`, and `.nextflow.log` (left in the repo root by the
pipeline run) are gitignored, so they won't get staged by accident, but
clean them up anyway before you start poking around for anything else to
commit:

```bash
rm -rf work .nextflow .nextflow.log
git status --short   # should show only the mp4
```

If a previous attempt's server or `nextflow run` is still hanging around
(a killed script leaves both as orphans):

```bash
pkill -f "nf-metro serve"
pkill -f "nextflow run examples/live"
```

Then push and open the PR as usual.
