---
title: "Embedding guide"
---

:::note[Stable as of nf-metro 1.0]
The embedding surface (the `--nfm-*` properties, the `data-*` contract, and
the manifest schema) is a public, versioned surface. The manifest schema
version (`MANIFEST_SCHEMA_VERSION`) and driver contract version
(`DRIVER_CONTRACT_VERSION`) are both `1.0` and change under `major.minor`
semantics; see [Versioning and stability](#versioning-and-stability) below.
:::

This guide is for someone putting a rendered nf-metro map into **their own**
page or application: a docs site, an internal dashboard, a pipeline run viewer.
You do not need to read `src/` to follow it. It covers how to produce an
embed-friendly file, how to size and theme it from the host page, and how to
drive it from live state (lighting up nodes as a job runs).

:::tip[Just want a picture?]
Skip straight to [Static embed](#a-static-embed). If you want a panel that reacts to a running pipeline, read on to [Interactive and progress embeds](#interactive-and-progress-embeds).
:::

## Choosing an output

nf-metro renders two shapes, and the right one depends on what the host needs.

| You want                                                    | Use                                           | Why                                                              |
| ----------------------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------- |
| A static picture (thumbnail, README, slide)                 | `render` → **SVG**                            | One self-contained file; scales crisply; no scripts.             |
| A live, interactive panel (pan/zoom, line filtering, hover) | `render --format html`                        | A self-contained page with the driver and styling already wired. |
| A progress overlay driven by your own app                   | **SVG** + the [manifest](/nf-metro/manifest/) | You read the embedded manifest and draw your own status layer.   |

The SVG carries a machine-readable [manifest](/nf-metro/manifest/) and a stable
[`data-*` contract](/nf-metro/embed/) either way, so a static embed can later become an
interactive one without re-rendering.

## Render options for embedding

These flags shape the SVG for life inside someone else's page. They apply to
`--format svg`; the interactive HTML page already handles sizing, scoping, and
chrome itself (see [Interactive and progress embeds](#interactive-and-progress-embeds)).

### Responsive sizing - `--responsive`

By default the `<svg>` carries fixed `width`/`height` attributes. With
`--responsive` it emits **only** a `viewBox` (plus `preserveAspectRatio`), so
the host sizes it with CSS:

```bash
nf-metro render pipeline.mmd -o pipeline.svg --responsive
```

```css
.metro-map svg {
  width: 100%;
  height: auto;
}
```

Use this for any fluid layout. The `viewBox` stays `0 0 <width> <height>`, so
overlays built from the manifest still line up (see
[Progress overlays](#progress-overlays)).

### Font portability - `--embed-font` / `--text-to-paths`

By default the SVG references a system font family, which renders differently
(or falls back) on a host without that font. Two flags make it self-contained:

| Flag              | What it does                                              | Keeps selectable text?        | Trade-off                                             |
| ----------------- | --------------------------------------------------------- | ----------------------------- | ----------------------------------------------------- |
| `--embed-font`    | Inlines a subset of Inter as a base64 `@font-face` block. | Yes (and `data-*` on labels). | Larger file.                                          |
| `--text-to-paths` | Converts every glyph to a vector `<path>`.                | No.                           | Smallest dependency surface; needs `fonttools[woff]`. |

```bash
nf-metro render pipeline.mmd -o pipeline.svg --embed-font      # portable, still selectable
nf-metro render pipeline.mmd -o pipeline.svg --text-to-paths   # zero font dependency
```

Prefer `--embed-font` when you want labels to stay selectable/searchable;
`--text-to-paths` when the consumer is a strict renderer or you need pixel
fidelity with no font handling at all.

### Bare fragment - `--bare`

`--bare` drops the title and the outer right padding so the canvas hugs the
content, for a host that supplies its own frame and heading:

```bash
nf-metro render pipeline.mmd -o pipeline.svg --bare
```

The `viewBox` origin stays at `0 0` and coordinates stay absolute, so the
[manifest](/nf-metro/manifest/) and any overlay still align. The attribution watermark
is **kept** in bare mode (see [Attribution](#attribution)).

### Theming from the host - `--nfm-*` properties

Chrome colors (background, title, labels, section boxes, legend) are emitted as
CSS custom properties with the theme color as the fallback, e.g.
`fill: var(--nfm-bg, #2b2b2b)`. A host recolors the map **without re-rendering**
by setting these on a wrapping element:

```css
.metro-map {
  --nfm-bg: #ffffff;
  --nfm-title-color: #222;
  --nfm-label-color: #333;
  --nfm-section-fill: #f4f4f4;
  --nfm-section-stroke: #ddd;
  --nfm-section-label-color: #555;
  --nfm-legend-bg: #fafafa;
  --nfm-legend-text-color: #333;
}
```

| Property                                      | Recolors                       |
| --------------------------------------------- | ------------------------------ |
| `--nfm-bg`                                    | Background rectangle           |
| `--nfm-title-color`                           | Title text                     |
| `--nfm-label-color`                           | Station labels                 |
| `--nfm-section-fill` / `--nfm-section-stroke` | Section box fill / border      |
| `--nfm-section-label-color`                   | Section names and group labels |
| `--nfm-legend-bg` / `--nfm-legend-text-color` | Legend background / text       |

Line and route colors are **not** recolorable - they carry meaning, so they
stay baked as presentation attributes.

### Multiple maps on one page - `--svg-class-prefix`

Two inline SVGs on the same page share class names (`nf-metro-station`, …),
so host CSS or the dark-mode block from one can bleed into the other. Give each
a distinct prefix:

```bash
nf-metro render a.mmd -o a.svg --svg-class-prefix mapA
nf-metro render b.mmd -o b.svg --svg-class-prefix mapB
```

`mapA-nf-metro-station`, `mapB-nf-metro-station`, and so on stay independent.
`data-*` attributes and the manifest element id are never prefixed, so the
[contract](/nf-metro/embed/) is unchanged.

### Dark-mode opt-out - `--no-dark-mode-css`

When a theme has a transparent background, the SVG injects a
`@media (prefers-color-scheme: dark)` block so labels stay readable on a dark
host page. If your host manages its own theme and that media query fights it,
suppress it:

```bash
nf-metro render pipeline.mmd -o pipeline.svg --no-dark-mode-css
```

### Raster export (PNG) - `--no-chrome-css`

The `--nfm-*` properties above use CSS `var()`, which a browser resolves but
many rasterizers (including **cairosvg**) cannot parse - they abort on the
`var()` token. For a PNG, either render with `--no-chrome-css` (which bakes the
concrete theme colors and omits the `var()` block; the map looks identical, you
just lose live host recoloring)…

```bash
nf-metro render pipeline.mmd -o pipeline.svg --no-chrome-css
python -c "import cairosvg; cairosvg.svg2png(url='pipeline.svg', write_to='pipeline.png', scale=2)"
```

…or feed the default SVG to a CSS-custom-property-aware rasterizer
(`resvg`, `rsvg-convert`, or headless Chromium), which honors the fallbacks.

## Sizing and placement

Everything in an nf-metro SVG lives in one coordinate space: `viewBox="0 0 w h"`
with no outer transform. That is what makes the host's job simple:

- **Size** the SVG with CSS (`width: 100%; height: auto`) - use `--responsive`
  so there are no fixed dimensions to override.
- **Stack** a base render and an overlay by giving both the **same `viewBox`**
  and absolutely positioning them in the same box. Because coordinates are
  absolute and share the origin, a marker the overlay draws at a node's
  manifest `(x, y)` lands exactly on that node.

```html
<div class="metro-map" style="position: relative;">
  <!-- base render, sized by CSS -->
  <object data="pipeline.svg" type="image/svg+xml" style="width:100%;"></object>
  <!-- overlay, same viewBox, on top -->
  <svg
    viewBox="0 0 1509 759"
    style="position:absolute; inset:0; width:100%; pointer-events:none;"
  >
    <!-- status markers at manifest coordinates -->
  </svg>
</div>
```

The manifest's `width`/`height` fields give the exact `viewBox` to reuse.

## The embed contract

The stable surface a host depends on is documented in one authoritative place
each - this guide links to them rather than restating them:

- **[Embed contract](/nf-metro/embed/)** - the `data-node-*` / `data-station-*` /
  `data-section-*` attribute vocabulary and the driver API
  (`attachMetroMap`, `highlightLine`, `selectNode`, `getManifest`, …).
- **[Data manifest](/nf-metro/manifest/)** - the manifest JSON schema, its version, the
  matching semantics (`patterns` → runtime names), and the `overlay_svg` helper.

The join key across all of it is the node `id`: it equals `data-node-id` on the
drawn element and `node.id` in the manifest JSON.

## A static embed

The minimum to put a map on a page. Render a portable, fluid SVG and inline it:

```bash
nf-metro render pipeline.mmd -o pipeline.svg --responsive --embed-font
```

```html
<div class="metro-map" style="max-width: 1000px;">
  <!-- paste the contents of pipeline.svg here, or: -->
  <object data="pipeline.svg" type="image/svg+xml" style="width:100%;"></object>
</div>
```

GitHub READMEs strip `<script>`, so a static SVG is the right choice there. Most
static-site generators and wikis accept the inline SVG as-is.

## Interactive and progress embeds

### The self-contained interactive page

`render --format html` produces a complete page - SVG, driver, and styling
inlined, no network. Its **Embed…** modal offers an inline `<div>` snippet
(keeps interactivity, no iframe), an iframe one-liner, and a static-SVG
fallback. The page is already responsive and scopes each map independently, so
the SVG-only sizing/namespacing flags above do not apply to it (the CLI warns
if you pass them with `--format html`). Font portability **does** reach the
inlined SVG, so an embeddable page can carry its own fonts:

```bash
nf-metro render pipeline.mmd --format html -o pipeline.html --embed-font
```

To wire the driver onto a page yourself (rather than copy the modal snippet),
see the [driver API](/nf-metro/embed/#driver-api) and `nf-metro embed-script`.

### Progress overlays

To light up nodes as a pipeline runs, keep the base map static and redraw a
thin **overlay** layer on each state change. The base SVG is the durable map;
the overlay is a cheap, disposable status layer. The coordinate-space rules:

- The base SVG and overlay share `viewBox="0 0 w h"` (origin `0 0`).
- The manifest's `width`/`height` match the base render's dimensions.
- Each node's `x`/`y`/`r` are absolute units in that space, so an overlay
  marker at `(x, y)` lands on the node.

The recipe is always the same three steps: `read_manifest` the committed SVG,
`match_node_ids` each runtime event to a node, and redraw an `overlay_svg()`
status layer over the base. The manifest tutorial,
**[Light up a diagram as a job runs](/nf-metro/manifest/#tutorial-light-up-a-diagram-as-a-job-runs)**,
walks it end to end in ~50 lines of Python (with the matching semantics and the
node state model documented alongside it on the [Data manifest](/nf-metro/manifest/)
page).

For a ready-made server that does exactly this for a live Nextflow run - no code
to write - see [Live progress](/nf-metro/live/).

## Versioning and stability

The manifest schema and the driver contract are versioned independently, both
`1.0` today. The stable surface keyed to those versions - the `data-*` attribute
names, the manifest fields, the `0 0 w h` coordinate rule, and the driver method
names - and the `major.minor` rules for changing it are specified under
[Versioning](/nf-metro/embed/#versioning) on the Embed contract page. This surface is
stable as of nf-metro 1.0: within a major version it only grows in
backward-compatible ways, so **consumers must ignore unknown fields**. Pin to a
specific nf-metro release only if you depend on the exact bytes of the output.

## Attribution

:::note[Please keep the watermark]
Rendered maps carry a small `created with nf-metro` watermark in the corner — including in `--bare` mode. It is a quiet credit that helps people find the project, and keeping it is the easiest way to support nf-metro. There is no convenience flag to remove it; removal is reserved for specific functionality rather than offered as a toggle. This is a friendly ask, not a license restriction.
:::
