# nf-metro

Generate metro-map-style SVG diagrams from Mermaid graph definitions with `%%metro` directives. Designed for visualizing bioinformatics pipeline workflows (e.g., nf-core pipelines) as transit-style maps where each analysis route is a colored "metro line."

![nf-core/rnaseq metro map](assets/renders/rnaseq_auto.svg)

## Installation

### pip (PyPI)

```bash
pip install nf-metro
```

### Conda (Bioconda)

```bash
conda install bioconda::nf-metro
```

### Container (Seqera Containers)

A pre-built container is available via [Seqera Containers](https://seqera.io/containers/):

```bash
docker pull community.wave.seqera.io/library/pip_nf-metro:611b1ba39c6007f1
```

Requires Python 3.10+.

## Quick start

Render a metro map from a `.mmd` file:

```bash
nf-metro render examples/simple_pipeline.mmd -o pipeline.svg
```

Validate your input without rendering:

```bash
nf-metro validate examples/simple_pipeline.mmd
```

Inspect structure (sections, lines, stations):

```bash
nf-metro info examples/simple_pipeline.mmd
```

## CLI reference

### `nf-metro render`

Render a Mermaid metro map definition to SVG or interactive HTML.

```
nf-metro render [OPTIONS] INPUT_FILE
```

Every layout/render option below also has a `%%metro` directive twin; an explicitly-passed flag overrides the directive (see the [precedence table](guide.md#cli-flags-and-directive-precedence) in the guide).

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output PATH` | `<input>.<format>` | Output file path |
| `--format [svg\|html]` | `svg` | Output format: `svg` or interactive `html` |
| `--theme [nfcore\|light]` | from `style:`, else `nfcore` | Visual theme |
| `--debug / --no-debug` | off | Show debug overlay |
| `--from-nextflow` | off | Convert Nextflow `-with-dag` input before rendering |
| `--logo PATH` | none | Logo image path |
| `--title TEXT` | none | Pipeline title |
| `--legend TEXT` | auto | Legend+logo position (keyword, `keyword \| canvas`, `keyword \| dx,dy`, or `x,y`) |
| `--line-spread [bundle\|centered\|rails]` | `bundle` | How shared lines relate vertically |
| `--x-spacing FLOAT` | auto | Horizontal spacing between layers |
| `--y-spacing FLOAT` | auto | Vertical spacing between tracks |
| `--section-x-gap FLOAT` | 50 | Horizontal gap between sections |
| `--section-y-gap FLOAT` | 50 | Vertical gap between sections |
| `--fold-threshold INTEGER` | auto (15) | Max station-columns per row before folding |
| `--diamond-style [straight\|symmetric]` | `straight` | Fork-join layout |
| `--line-order [definition\|span]` | `definition` | Line ordering for track assignment |
| `--center-ports / --no-center-ports` | off | Centre inter-section ports on the shorter section |
| `--compact-offsets / --no-compact-offsets` | off | Size stations only for the lines passing through |
| `--label-angle FLOAT` | theme default | Station-label angle in degrees |
| `--font-scale FLOAT` | 1.0 | Scale text and label-driven layout spacing |
| `--logo-scale FLOAT` | 1.0 | Scale the logo within the legend |
| `--legend-min-height FLOAT` | 0 | Minimum legend content height in pixels |
| `--legend-logo-gap FLOAT` | auto | Gap between logo and legend entries |
| `--width INTEGER` | auto | Output width in pixels |
| `--height INTEGER` | auto | Output height in pixels |
| `--animate / --no-animate` | off | Add animated balls traveling along lines |

#### Embedding options

Flags for producing an SVG to embed in another page or application. The [Embedding guide](embedding.md) explains when to use each.

| Option | Default | Description |
|--------|---------|-------------|
| `--responsive / --no-responsive` | off | Emit `viewBox` only (no fixed `width`/`height`) for CSS-scalable embedding |
| `--embed-font / --no-embed-font` | off | Inline Inter as a base64 `@font-face` so the SVG renders identically anywhere |
| `--text-to-paths / --no-text-to-paths` | off | Convert text to vector paths (no font dependency; loses selectable text) |
| `--bare / --no-bare` | off | Omit the title and outer padding so the canvas hugs the content (keeps the watermark) |
| `--svg-class-prefix TEXT` | none | Prefix every SVG presentation class so multiple maps on one page don't collide |
| `--no-dark-mode-css` | off | Suppress the `prefers-color-scheme: dark` block when the host manages its own theme |
| `--no-chrome-css` | off | Bake concrete chrome colors instead of `--nfm-*` `var()` (needed for raster export, e.g. cairosvg) |

#### Interactive HTML output

`--format html` produces a self-contained `.html` file with the SVG inlined plus a small JS/CSS layer (no external dependencies, no network):

```bash
nf-metro render pipeline.mmd --format html -o pipeline.html
```

The page supports drag-to-pan, scroll-to-zoom, station hover tooltips, and a clickable line legend. Clicking a line isolates it: stations and sections not carrying that line are hidden and the view zooms to the bounding box of what remains. Click again, hit `Esc`, or use the Reset button to restore.

The **Embed&hellip;** button opens a panel with three copyable snippets:

- **Inline HTML** - a self-contained `<div>` you paste into any HTML host (MkDocs, Confluence, Notion, blog templates). Keeps full interactivity, no iframe.
- **iframe** - a one-liner pointing at the hosted `.html` file.
- **Static SVG** - the raw `<svg>` markup for hosts that strip scripts.

GitHub READMEs strip `<script>` tags, so embed there as a static SVG (or link out to a hosted version). Most static-site generators and internal wikis run the inline-HTML snippet as-is.

### `nf-metro convert`

Convert a Nextflow `-with-dag` mermaid file to nf-metro format.

```
nf-metro convert [OPTIONS] INPUT_FILE
```

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output PATH` | stdout | Output `.mmd` file path |
| `--title TEXT` | auto | Pipeline title |

See [Importing from Nextflow](nextflow.md) for details and examples.

### `nf-metro validate`

Check a `.mmd` file for errors without producing output.

```
nf-metro validate INPUT_FILE
```

### `nf-metro info`

Print a summary of the parsed map: sections, lines, stations, and edges.

```
nf-metro info INPUT_FILE
```

## Writing metro maps

Read the [Guide](guide.md) to learn how to write `.mmd` files, from minimal examples to multi-section pipelines with custom grid layouts.

## Embedding maps in your own page

Read the [Embedding guide](embedding.md) to put a rendered map into a docs site, dashboard, or app: responsive sizing, font portability, host theming, and driving a progress overlay from a running pipeline.

## Gallery

See the [Gallery](gallery/index.md) for rendered examples covering simple pipelines, complex multi-line topologies, fan-out/fan-in patterns, fold layouts, and realistic bioinformatics workflows.

## License

[MIT](https://github.com/pinin4fjords/nf-metro/blob/main/LICENSE)
