# nf-metro

**[Documentation](https://pinin4fjords.github.io/nf-metro/latest/)**

Generate metro-map-style SVG diagrams from Mermaid graph definitions with `%%metro` directives. Designed for visualizing bioinformatics pipeline workflows (e.g., nf-core pipelines) as transit-style maps where each analysis route is a colored "metro line."

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/pinin4fjords/nf-metro/main/examples/rnaseq_light_animated.svg">
  <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/pinin4fjords/nf-metro/main/examples/rnaseq_light_animated.svg">
  <img alt="nf-core/rnaseq metro map" src="https://raw.githubusercontent.com/pinin4fjords/nf-metro/main/examples/rnaseq_auto_light.png">
</picture>

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

### Development

```bash
pip install -e ".[dev]"
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
| `--theme [nfcore\|light\|seqera]` | from `style:`, else `nfcore` | Visual theme |
| `--legend TEXT` | auto | Legend+logo position (keyword, `keyword \| canvas`, `keyword \| dx,dy`, or `x,y`) |
| `--line-spread [bundle\|centered\|rails]` | `bundle` | How lines sharing a station relate vertically |
| `--width INTEGER` | auto | SVG width in pixels |
| `--height INTEGER` | auto | SVG height in pixels |
| `--x-spacing FLOAT` | auto | Horizontal spacing between layers (widened from 60 only when wide labels would otherwise collide) |
| `--y-spacing FLOAT` | auto | Vertical spacing between tracks (derived from the map's content so captioned icons and dense labels don't collide) |
| `--fold-threshold INTEGER` | `15` | Max station-columns a section row may reach before wrapping to the next row |
| `--animate / --no-animate` | off | Add animated balls traveling along lines |
| `--directional / --no-directional` | off | Draw static chevrons along each route pointing in the flow direction (source to target) |
| `--strict / --no-strict` | off | Treat a layout-invariant violation on the rendered geometry (e.g. a station outside its section box) as an error that aborts the render, instead of a warning |
| `--debug / --no-debug` | off | Show debug overlay (ports, hidden stations, edge waypoints) |
| `--logo PATH` | none | Logo image path (overrides `%%metro logo:` directive) |
| `--line-order [definition\|span]` | `definition` | Line ordering strategy: `definition` preserves `.mmd` order, `span` sorts by section span (longest first) |
| `--diamond-style [straight\|symmetric]` | `straight` | Fork-join layout: `straight` keeps the top branch on the main track; `symmetric` fans the branches evenly |
| `--center-ports / --no-center-ports` | off | Centre inter-section ports on the shorter of the two connected sections |
| `--compact-offsets / --no-compact-offsets` | off | Size each station only for the lines actually passing through it |
| `--section-x-gap FLOAT` | `50` | Horizontal gap between sections |
| `--section-y-gap FLOAT` | `50` | Vertical gap between sections |
| `--label-angle FLOAT` | theme default | Station-label angle in degrees |
| `--font-scale FLOAT` | `1.0` | Scale text and the label metrics that drive layout spacing |
| `--logo-scale FLOAT` | `1.0` | Scale the logo within the legend |
| `--legend-min-height FLOAT` | `0` | Minimum legend content height in pixels |
| `--legend-logo-gap FLOAT` | auto | Gap between the logo and the legend entries |
| `--validate` | off | Run the render-geometry oracle over the drawn SVG and report violations |
| `--from-nextflow` | off | Convert Nextflow `-with-dag` mermaid input before rendering |
| `--title TEXT` | none | Pipeline title (overrides the `%%metro title:` directive) |

#### Embedding options

Flags for producing an SVG to embed in another page or application. See the
[embedding guide](docs/embedding.md) for when to use each.

| Option | Default | Description |
|--------|---------|-------------|
| `--responsive / --no-responsive` | off | Emit `viewBox` only (no fixed `width`/`height`) so the host can scale with CSS |
| `--embed-font / --no-embed-font` | off | Inline Inter as a base64 `@font-face` so the SVG renders identically on any host |
| `--text-to-paths / --no-text-to-paths` | off | Convert text to vector paths (no font dependency; loses selectable text; needs `fonttools[woff]`) |
| `--bare / --no-bare` | off | Omit the title and outer padding so the canvas hugs the content (keeps the watermark) |
| `--svg-class-prefix TEXT` | none | Prefix every SVG presentation class, so multiple maps on one page don't collide |
| `--no-dark-mode-css` | off | Suppress the `prefers-color-scheme: dark` block when the host manages its own theme |
| `--no-chrome-css` | off | Bake concrete chrome colors instead of `--nfm-*` `var()` (needed for raster export, e.g. cairosvg) |

The `--logo` flag lets you use the same `.mmd` file with different logos for dark/light themes:

```bash
nf-metro render pipeline.mmd -o pipeline_dark.svg --theme nfcore --logo logo_dark.png
nf-metro render pipeline.mmd -o pipeline_light.svg --theme light --logo logo_light.png
```

#### Interactive HTML output

`--format html` produces a self-contained `.html` file with the SVG inlined plus a small JS/CSS layer (no external dependencies, no network):

```bash
nf-metro render pipeline.mmd --format html -o pipeline.html
```

The page provides:

- **Drag to pan**, **scroll to zoom** (Cmd/Ctrl+scroll in embedded mode).
- **Hover a station** to see its label, section, and the lines passing through it.
- **Click a line in the legend** to isolate it. Stations and sections not carrying that line disappear and the view zooms to the bounding box of what remains. Click again, hit `Esc`, or use the **Reset** button to restore.
- **Embed&hellip;** opens a copy-snippet panel with three options:
  - **Inline HTML** - a self-contained `<div>` you paste into any HTML host (MkDocs, Confluence, Notion, blog templates). Keeps full interactivity, no iframe.
  - **iframe** - a one-liner pointing at the hosted `.html` file.
  - **Static SVG** - the raw `<svg>` markup for contexts that strip scripts.

GitHub READMEs strip `<script>` tags, so embed there as a static SVG (or link out to a hosted version). Most static-site generators and internal wikis run the inline-HTML snippet as-is.

#### Embedded data manifest

Every rendered SVG carries a machine-readable manifest so the committed file is a self-contained, durable artifact - a downstream tool can drive it (position overlays, restyle nodes, look up which processes a node represents) without re-running the layout engine. The format is tool-neutral, so it uses generic vocabulary - a metro station is a **node**, a line a **group**, a section a **region**. The data is carried two redundant ways, both sanitization-safe (no `<script>`):

- A JSON manifest in a `<metadata id="diagram-manifest">` element: schema `version`, `title`, canvas `width`/`height`, `groups`, `regions`, and `nodes` (each with `id`, `label`, absolute `x`/`y`/`r`, the `groups` and `region` it belongs to, and the `patterns` regexes it matches).
- `data-node-*` attributes on each node's `<g>` element (`data-node-id`, `data-node-cx/cy/r`, `data-node-groups`, `data-node-region`), so individual nodes stay addressable directly in the DOM.

The node `id` is the join key between the two: it equals `data-node-id="<id>"` on the matching element. Coordinates are absolute SVG user units inside the `viewBox="0 0 width height"`, so an overlay sharing that viewBox lines up exactly. The manifest is embedded by default and adds no external dependencies; set `%%metro manifest: false` to emit the drawn map only. The format is a standalone contract any tool can emit - the `nf_metro.manifest` helpers build, embed, read, and match it without a metro map. See [Data manifest](docs/manifest.md) for the full schema, the terminology, the matching semantics, and how to produce a conforming SVG yourself.

### `nf-metro validate`

Check a `.mmd` file for errors without producing output. The bare command runs
graph-semantic checks (every edge references a defined line, every section
points at stations that exist, the graph is acyclic).

```
nf-metro validate [OPTIONS] INPUT_FILE
```

| Option | Default | Description |
|--------|---------|-------------|
| `--with-layout` | off | Also run the layout engine with its full invariant suite, reporting a layout failure as an error instead of a traceback |
| `--strict` | off | Treat warnings (e.g. a non-LR primary direction) as errors |

### `nf-metro info`

Print a summary of the parsed map: sections, lines, stations, and edges.

```
nf-metro info INPUT_FILE
```

### `nf-metro convert`

Convert a Nextflow `-with-dag` mermaid file to nf-metro `.mmd` format. The output can be rendered directly or hand-tuned first.

```
nf-metro convert [OPTIONS] INPUT_FILE
```

| Option | Default | Description |
|--------|---------|-------------|
| `-o`, `--output PATH` | stdout | Output `.mmd` file path |
| `--title TEXT` | auto | Pipeline title for the converted output |

To convert and render in one step, use the `--from-nextflow` flag on `render` instead. See [Importing from Nextflow](docs/nextflow.md) for details.

## Examples

The [`examples/`](examples/) directory contains ready-to-render `.mmd` files:

| Example | Description |
|---------|-------------|
| [`simple_pipeline.mmd`](examples/simple_pipeline.mmd) | Minimal two-line pipeline with no sections |
| [`rnaseq_auto.mmd`](examples/rnaseq_auto.mmd) | nf-core/rnaseq with fully auto-inferred layout |
| [`rnaseq_sections.mmd`](examples/rnaseq_sections.mmd) | nf-core/rnaseq with manual grid overrides |

### Topology gallery

The [`examples/topologies/`](examples/topologies/) directory has 38 examples covering a range of layout patterns. See the [topology README](examples/topologies/README.md) for descriptions and rendered previews.

A few highlights:

| | | |
|:---:|:---:|:---:|
| **Wide Fan-Out** | **Section Diamond** | **Variant Calling** |
| ![Wide Fan-Out](examples/topologies/wide_fan_out.png) | ![Section Diamond](examples/topologies/section_diamond.png) | ![Variant Calling](examples/topologies/variant_calling.png) |
| **Fold Serpentine** | **Multi-Line Bundle** | **RNA-seq Lite** |
| ![Fold Double](examples/topologies/fold_double.png) | ![Multi-Line Bundle](examples/topologies/multi_line_bundle.png) | ![RNA-seq Lite](examples/topologies/rnaseq_lite.png) |

## Input format

Input files use a subset of Mermaid `graph LR` syntax extended with `%%metro` directives. The format has three layers: **global directives** that configure the overall map, **section directives** inside `subgraph` blocks that control section layout, and **edges** that define connections between stations.

### Walkthrough: nf-core/rnaseq

The full example is at [`examples/rnaseq_sections.mmd`](examples/rnaseq_sections.mmd). Here's how each part works.

#### Global directives

```
%%metro title: nf-core/rnaseq
%%metro logo: examples/nf-core-rnaseq_logo_dark.png
%%metro style: dark
```

- `title:` sets the map title (shown top-left unless a logo is provided)
- `logo:` embeds a PNG image in place of the text title
- `style:` selects a theme (`dark` or `light`)

#### Lines (routes)

Each metro line represents a distinct path through the pipeline. Lines are defined with an ID, display name, and color:

```
%%metro line: star_rsem | Aligner: STAR, Quantification: RSEM | #0570b0
%%metro line: star_salmon | Aligner: STAR, Quantification: Salmon (default) | #2db572
%%metro line: hisat2 | Aligner: HISAT2, Quantification: None | #f5c542
%%metro line: pseudo_salmon | Pseudo-aligner: Salmon, Quantification: Salmon | #e63946
%%metro line: pseudo_kallisto | Pseudo-aligner: Kallisto, Quantification: Kallisto | #7b2d3b
```

In the rnaseq pipeline, each line corresponds to a parameter-driven analysis route. All five lines share the preprocessing section, then diverge based on aligner choice.

#### Grid placement

Sections are placed on a grid automatically via topological sort, but explicit positions can be set:

```
%%metro grid: postprocessing | 2,0,2
%%metro grid: qc_report | 1,2,1,2
```

The format is `section_id | col,row[,rowspan[,colspan]]`. In this example:
- `postprocessing` is pinned to column 2, row 0, spanning 2 rows vertically
- `qc_report` is pinned to column 1, row 2, spanning 2 columns horizontally

#### Legend

```
%%metro legend: bl
```

Position the legend: `tl`, `tr`, `bl`, `br` (corners), `bottom`, `right`, or `none`.

#### Sections

Sections are Mermaid `subgraph` blocks. Each section is laid out independently, then placed on the grid:

```
graph LR
    subgraph preprocessing [Pre-processing]
        %%metro exit: right | star_salmon, star_rsem, hisat2
        %%metro exit: bottom | pseudo_salmon, pseudo_kallisto
        cat_fastq[cat fastq]
        fastqc_raw[FastQC]
        ...
    end
```

**Section directives:**

- `%%metro entry: <side> | <line_ids>` - declares which lines enter this section and from which side (`left`, `right`, `top`, `bottom`)
- `%%metro exit: <side> | <line_ids>` - declares which lines exit and to which side
- `%%metro direction: <dir>` - section flow direction: `LR` (default), `RL` (right-to-left), or `TB` (top-to-bottom)

Entry/exit hints control port placement on section boundaries. A section can have exit hints on multiple sides (e.g., preprocessing exits right for aligners and bottom for pseudo-aligners), but all lines from a section leave through a single exit port. If all exit hints point to one side, that side is used; otherwise it defaults to `right`.

#### Section directions

Most sections flow left-to-right (`LR`, the default). Two other directions are useful for layout:

**Top-to-bottom (`TB`)** - used for the Post-processing section, which acts as a vertical connector carrying lines downward:

```
    subgraph postprocessing [Post-processing]
        %%metro direction: TB
        %%metro entry: left | star_salmon, star_rsem, hisat2
        %%metro exit: bottom | star_salmon, star_rsem, hisat2
        samtools[SAMtools]
        picard[Picard]
        ...
    end
```

**Right-to-left (`RL`)** - used for the QC section, which flows backward to create a serpentine layout:

```
    subgraph qc_report [Quality control & reporting]
        %%metro direction: RL
        %%metro entry: top | star_salmon, star_rsem, hisat2
        rseqc[RSeQC]
        preseq[Preseq]
        ...
    end
```

#### Stations and edges

Stations use Mermaid node syntax. Edges carry comma-separated line IDs to indicate which routes use that connection:

```
        cat_fastq[cat fastq]
        fastqc_raw[FastQC]

        cat_fastq -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| fastqc_raw
```

All five lines pass through this edge. Later, lines diverge:

```
        star -->|star_rsem| rsem
        star -->|star_salmon| umi_tools_dedup
        hisat2_align -->|hisat2| umi_tools_dedup
```

Here different lines take different paths through the section, creating the visual fork in the metro map.

#### Inter-section edges

Edges between stations in different sections go outside all `subgraph`/`end` blocks:

```
    %% Inter-section edges
    sortmerna -->|star_salmon,star_rsem| star
    sortmerna -->|hisat2| hisat2_align
    sortmerna -->|pseudo_salmon| salmon_pseudo
    sortmerna -->|pseudo_kallisto| kallisto
    stringtie -->|star_salmon,star_rsem,hisat2| rseqc
```

These are automatically rewritten into port-to-port connections with junction stations at fan-out points. You just specify the source and target stations directly.

### Directive reference

| Directive | Scope | Description |
|-----------|-------|-------------|
| `%%metro title: <text>` | Global | Map title |
| `%%metro logo: <path>` | Global | Logo image (replaces title text) |
| `%%metro logo_scale: <factor>` | Global | Scale the logo within the legend block (`1.0` = auto-size); values above 1 grow the legend box to contain it |
| `%%metro style: <name>` | Global | Theme: `dark`, `light` |
| `%%metro line: <id> \| <name> \| <color> [\| <style>]` | Global | Define a metro line. Optional style: `solid` (default), `dashed`, `dotted` |
| `%%metro grid: <section> \| <col>,<row>[,<rowspan>[,<colspan>]]` | Global | Pin section to grid position |
| `%%metro legend: <position>` | Global | Legend position: `tl`, `tr`, `bl`, `br`, `bottom`, `right`, `none` (append `\| canvas`, `\| <dx>,<dy>`, or use `<x>,<y>` for finer placement - see the [guide](docs/guide.md)) |
| `%%metro line_order: <strategy>` | Global | Line ordering for track assignment: `definition` (default) or `span` (longest-spanning lines get inner tracks) |
| `%%metro file: <station> \| <label> [\| <name>] [\| banner]` | Global | Mark a station as a file terminus with a document icon. Optional `name` renders as a caption below the icon; optional `banner` draws the label on a dark strip across the icon |
| `%%metro files: <station> \| <label> [\| <name>] [\| banner]` | Global | Mark a station with a stacked-documents icon (e.g. paired files). Optional `name` caption; optional `banner` strip |
| `%%metro dir: <station> \| <label> [\| <name>]` | Global | Mark a station with a folder icon (e.g. output directory). Optional `name` caption |
| `%%metro off_track: <station>[, <station>...]` | Global | Lift the listed stations (typically `file:`/`files:`/`dir:` artefacts) above the section's main track instead of consuming a line-track slot, anchored to their consumer (inputs) or producer (output artefacts) |
| `%%metro compact_offsets: true` | Global | Use compact per-station offsets instead of global line-priority slots (better for dense maps with few lines) |
| `%%metro center_ports: true` | Global | Centre inter-section ports on the shorter of the two connected sections (overridden by the `--center-ports` / `--no-center-ports` CLI flag) |
| `%%metro line_spread: <mode>[ \| <section>...]` | Global / Section | How lines sharing a station relate vertically: `bundle` (default) merges them onto one trunk that cascades downward, `centered` balances that bundle about the midline, `rails` draws each line as a parallel rail with shared stations as interchanges. Bare form sets the graph default; the `\| <section>, ...` form overrides named sections. Overridden by the `--line-spread` CLI flag |
| `%%metro legend_min_height: <pixels>` | Global | Minimum legend content height in pixels (useful for single-line maps where the logo would otherwise be tiny) |
| `%%metro entry: <side> \| <lines>` | Section | Entry port hint |
| `%%metro exit: <side> \| <lines>` | Section | Exit port hint |
| `%%metro direction: <dir>` | Section | Flow direction: `LR`, `RL`, `TB` |

## Python API

nf-metro is a command-line tool. Its Python modules are importable, but the internal
API (parser, layout engine, renderer) is not part of the public, semver-stable surface
and may change between releases without notice. Drive nf-metro through the `nf-metro`
CLI (or `python -m nf_metro`) for stable behaviour.

## License

[MIT](LICENSE)
