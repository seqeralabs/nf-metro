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

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output PATH` | `<input>.<format>` | Output file path |
| `--format [svg\|html]` | `svg` | Output format: `svg` or interactive `html` |
| `--theme [nfcore\|light]` | `nfcore` | Visual theme |
| `--width INTEGER` | auto | SVG width in pixels |
| `--height INTEGER` | auto | SVG height in pixels |
| `--x-spacing FLOAT` | `60` | Horizontal spacing between layers |
| `--y-spacing FLOAT` | `40` | Vertical spacing between tracks |
| `--max-layers-per-row INTEGER` | auto | Max layers before folding to next row |
| `--animate / --no-animate` | off | Add animated balls traveling along lines |
| `--debug / --no-debug` | off | Show debug overlay |
| `--logo PATH` | none | Logo image path (overrides `%%metro logo:` directive) |
| `--center-ports / --no-center-ports` | off | Centre inter-section ports on the shorter of the two connected sections |
| `--from-nextflow` | off | Convert Nextflow `-with-dag` input before rendering |
| `--title TEXT` | none | Pipeline title (used with `--from-nextflow`) |

#### Interactive HTML output

`--format html` produces a self-contained `.html` file with the SVG inlined plus a small JS/CSS layer (no external dependencies, no network):

```bash
nf-metro render pipeline.mmd --format html -o pipeline.html
```

The page supports drag-to-pan, scroll-to-zoom, station hover tooltips, and a clickable line legend. Clicking a line isolates it: stations and sections not carrying that line are hidden and the view zooms to the bounding box of what remains. Click again, hit `Esc`, or use the Reset button to restore.

The **Embed&hellip;** button opens a panel with three copyable snippets:

- **Inline HTML** - a self-contained `<div>` with scoped CSS and an IIFE-bound script. Paste into any HTML host (MkDocs, Confluence, Notion, a blog template) and the interactivity travels with it. The wrapper class is hashed per render, so multiple maps coexist on the same page.
- **iframe** - a one-line `<iframe src="...">` for when the `.html` file is already hosted.
- **Static SVG** - the raw `<svg>` markup for hosts that strip scripts.

GitHub READMEs strip `<script>` tags, so the interactive page won't run inline there. The standard pattern is to host the HTML on GitHub Pages (or any static host) and link to it from the README, optionally with the static SVG as a preview image.

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

## Gallery

See the [Gallery](gallery/index.md) for rendered examples covering simple pipelines, complex multi-line topologies, fan-out/fan-in patterns, fold layouts, and realistic bioinformatics workflows.

## License

[MIT](https://github.com/pinin4fjords/nf-metro/blob/main/LICENSE)
