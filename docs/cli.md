---
title: "CLI reference"
description: "Full reference for the nf-metro command-line interface: render, convert, validate, and info."
---

nf-metro ships four commands - `render`, `convert`, `validate`, and `info`. This page documents every option. Most `render` flags also have a `%%metro` directive twin; an explicitly-passed flag overrides the directive.

## `nf-metro render`

Render a Mermaid metro map definition to SVG or interactive HTML.

```bash frame="terminal"
nf-metro render [OPTIONS] INPUT_FILE
```

Every layout/render option below also has a `%%metro` directive twin; an explicitly-passed flag overrides the directive (see the [precedence table](/nf-metro/guide/#cli-flags-and-directive-precedence) in the guide).

| Option                                     | Default                      | Description                                                                       |
| ------------------------------------------ | ---------------------------- | --------------------------------------------------------------------------------- |
| `-o`, `--output PATH`                      | `<input>.<format>`           | Output file path                                                                  |
| `--format [svg\|html]`                     | `svg`                        | Output format: `svg` or interactive `html`                                        |
| `--theme [nfcore\|light]`                  | from `style:`, else `nfcore` | Visual theme                                                                      |
| `--debug / --no-debug`                     | off                          | Show debug overlay                                                                |
| `--from-nextflow`                          | off                          | Convert Nextflow `-with-dag` input before rendering                               |
| `--logo PATH`                              | none                         | Logo image path (must exist; errors on a bad path, same as `%%metro logo:`)       |
| `--title TEXT`                             | none                         | Pipeline title                                                                    |
| `--legend TEXT`                            | auto                         | Legend+logo position (keyword, `keyword \| canvas`, `keyword \| dx,dy`, or `x,y`) |
| `--line-spread [bundle\|centered\|rails]`  | `bundle`                     | How shared lines relate vertically                                                |
| `--x-spacing FLOAT`                        | auto                         | Horizontal spacing between layers                                                 |
| `--y-spacing FLOAT`                        | auto                         | Vertical spacing between tracks                                                   |
| `--section-x-gap FLOAT`                    | 50                           | Horizontal gap between sections                                                   |
| `--section-y-gap FLOAT`                    | 50                           | Vertical gap between sections                                                     |
| `--fold-threshold INTEGER`                 | auto (15)                    | Max station-columns per row before folding                                        |
| `--diamond-style [straight\|symmetric]`    | `straight`                   | Fork-join layout                                                                  |
| `--line-order [definition\|span]`          | `definition`                 | Line ordering for track assignment                                                |
| `--center-ports / --no-center-ports`       | off                          | Centre inter-section ports on the shorter section                                 |
| `--compact-offsets / --no-compact-offsets` | off                          | Size stations only for the lines passing through                                  |
| `--label-angle FLOAT`                      | theme default                | Station-label angle in degrees                                                    |
| `--font-scale FLOAT`                       | 1.0                          | Scale text and label-driven layout spacing                                        |
| `--logo-scale FLOAT`                       | 1.0                          | Scale the logo within the legend                                                  |
| `--legend-min-height FLOAT`                | 0                            | Minimum legend content height in pixels                                           |
| `--legend-logo-gap FLOAT`                  | auto                         | Gap between logo and legend entries                                               |
| `--width INTEGER`                          | auto                         | Output width in pixels                                                            |
| `--height INTEGER`                         | auto                         | Output height in pixels                                                           |
| `--animate / --no-animate`                 | off                          | Add animated balls traveling along lines                                          |

### Embedding options

Flags for producing an SVG to embed in another page or application. The [Embedding guide](/nf-metro/embedding/) explains when to use each.

| Option                                 | Default | Description                                                                                        |
| -------------------------------------- | ------- | -------------------------------------------------------------------------------------------------- |
| `--responsive / --no-responsive`       | off     | Emit `viewBox` only (no fixed `width`/`height`) for CSS-scalable embedding                         |
| `--embed-font / --no-embed-font`       | off     | Inline Inter as a base64 `@font-face` so the SVG renders identically anywhere                      |
| `--text-to-paths / --no-text-to-paths` | off     | Convert text to vector paths (no font dependency; loses selectable text)                           |
| `--bare / --no-bare`                   | off     | Omit the title and outer padding so the canvas hugs the content (keeps the watermark)              |
| `--svg-class-prefix TEXT`              | none    | Prefix every SVG presentation class so multiple maps on one page don't collide                     |
| `--no-dark-mode-css`                   | off     | Suppress the `prefers-color-scheme: dark` block when the host manages its own theme                |
| `--no-chrome-css`                      | off     | Bake concrete chrome colors instead of `--nfm-*` `var()` (needed for raster export, e.g. cairosvg) |

### Interactive HTML output

`--format html` produces a self-contained `.html` file with the SVG inlined plus a small JS/CSS layer (no external dependencies, no network):

```bash frame="terminal"
nf-metro render pipeline.mmd --format html -o pipeline.html
```

The page supports drag-to-pan, scroll-to-zoom, station hover tooltips, and a clickable line legend. Clicking a line isolates it: stations and sections not carrying that line are hidden and the view zooms to the bounding box of what remains. Click again, hit `Esc`, or use the Reset button to restore.

The **Embed&hellip;** button opens a panel with copyable inline-HTML, iframe, and static-SVG snippets. The [Embedding guide](/nf-metro/embedding/) explains when to reach for each, plus responsive sizing, font portability, host theming, and progress overlays.

### Validating the rendered geometry

Pass `--validate` to check the _drawn_ SVG after rendering and fail (non-zero exit) if a route is drawn through a station's label or marker, or two distinct lines collapse into one stroke where they should run parallel. This reads the geometry as it ends up on the page (after the per-line offsets and label shifts the layout applies), catching defects the pre-render checks cannot see:

```bash frame="terminal"
nf-metro render pipeline.mmd -o pipeline.svg --validate
```

To run the same geometry checks on an already-rendered SVG, use [`nf-metro validate-svg --geometry`](/nf-metro/manifest/#manifest-schema).

## `nf-metro convert`

Convert a Nextflow `-with-dag` mermaid file to nf-metro format.

```bash frame="terminal"
nf-metro convert [OPTIONS] INPUT_FILE
```

| Option                | Default | Description             |
| --------------------- | ------- | ----------------------- |
| `-o`, `--output PATH` | stdout  | Output `.mmd` file path |
| `--title TEXT`        | auto    | Pipeline title          |

See [Importing from Nextflow](/nf-metro/nextflow/) for details and examples.

## `nf-metro validate`

Check a `.mmd` file for errors without producing output.

```bash frame="terminal"
nf-metro validate INPUT_FILE
```

## `nf-metro info`

Print a summary of the parsed map: sections, lines, stations, and edges.

```bash frame="terminal"
nf-metro info INPUT_FILE
```
