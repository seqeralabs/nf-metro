# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

nf-metro generates metro-map-style SVG diagrams from Mermaid graph definitions augmented with `%%metro` directives. It is designed for visualizing bioinformatics pipeline workflows (e.g., nf-core pipelines) as transit-style maps where each analysis route is a colored "metro line."

## Build & Development

```bash
# Install in development mode (uses hatchling build system)
pip install -e ".[dev]"

# Run CLI
nf-metro render examples/rnaseq_sections.mmd -o output.svg
nf-metro validate examples/rnaseq_sections.mmd
nf-metro info examples/rnaseq_sections.mmd

# Run via module
python -m nf_metro

# Run all tests
pytest

# Run a single test
pytest tests/test_parser.py::test_parse_title

# Lint
ruff check src/ tests/
```

Dependencies: click, drawsvg, networkx, pillow. Dev: pytest, ruff.

## Architecture

The pipeline is: **Parse** -> **Layout** -> **Render**

### Parser (`src/nf_metro/parser/`)
- `mermaid.py` - Line-by-line regex parser. Parses Mermaid `graph LR` syntax plus custom `%%metro` directives.
- `model.py` - Core data model: `MetroGraph`, `Station`, `Edge`, `MetroLine`, `Section`, `Port`. The `MetroGraph` dataclass is the central data structure passed through all stages.
- Sections are defined as Mermaid `subgraph` blocks with `%%metro entry:/exit:` port directives.
- Post-parse `_resolve_sections()` rewrites inter-section edges into 3-part chains: source -> exit_port -> entry_port -> target, inserting junction stations for fan-outs. Internally split into `_build_entry_side_mapping()`, `_classify_edges()`, and `_create_ports_and_junctions()`.

### Layout (`src/nf_metro/layout/`)
- `auto_layout.py` - Runs before `_resolve_sections()`. Infers missing grid positions, section directions, and port sides from the section DAG. Preserves any values explicitly set by `%%metro` directives. Handles fold thresholds (wrapping long chains into serpentine rows).
- `engine.py` - Orchestrator. Section-first layout runs 9 phases: (2) internal section layout, (3) section placement, (4) global coordinate mapping, (5) port positioning, (6) junction positioning, (7) entry port alignment, (8) exit port alignment on fold sections, (9) junction re-positioning after exit alignment. Port alignment is split into focused helpers (`_align_lr_entry_port`, `_align_tb_entry_port`, etc.). Per-phase preconditions, postconditions, and invariants are documented in `src/nf_metro/layout/CONTRACT.md`.
- `layers.py` - Longest-path layering via networkx topological sort (X-axis assignment).
- `ordering.py` - Track-per-line vertical ordering (Y-axis). Each metro line gets a dedicated base track. Handles diamond (fork-join) detection for compact layout of alternative paths (e.g., FastP/TrimGalore).
- `section_placement.py` - Meta-graph layout: places sections on a grid via topological layering of section dependencies. Supports `%%metro grid:` overrides. Also handles port positioning on section boundaries.
- `constants.py` - All layout magic numbers (spacing, padding, routing tolerances, etc.). Imported by engine, routing, ordering, labels, and section_placement modules.
- `labels.py` - Station label placement.

#### Routing subpackage (`src/nf_metro/layout/routing/`)
Edge routing with horizontal runs and 45-degree diagonal transitions. Inter-section edges use L-shaped (horizontal + vertical) routing. Junction stations get horizontal offset for visual line separation in bundles.

- `core.py` - Main `route_edges()` dispatcher. Pre-computes all shared state into a `_RoutingCtx` dataclass, then dispatches each edge through handler functions in priority order: `_route_inter_section` -> `_route_tb_internal` -> `_route_tb_lr_exit` -> `_route_tb_lr_entry` -> `_route_perp_entry` -> `_route_intra_section`. First handler that returns a result wins.
- `common.py` - Shared types (`RoutedPath`) and helper functions (`compute_bundle_info`, `inter_column_channel_x`, etc.).
- `corners.py` - Corner radius computation and curve smoothing.
- `inter_section.py` - Simplified inter-section routing (used by tests).
- `offsets.py` - Per-station Y offset computation for parallel lines.
- `reversal.py` - Fold/reversal edge routing (serpentine row transitions).

### Render (`src/nf_metro/render/`)
- `svg.py` - SVG generation using the `drawsvg` library. Renders section boxes, edges (with quadratic Bezier curves at corners), pill-shaped station markers, labels, and legend.
- `animate.py` - Animated SVG balls traveling along routed metro line paths (enabled via `--animate` CLI flag).
- `style.py` - `Theme` dataclass defining all visual properties.
- `legend.py` - Legend rendering.
- `icons.py` - Icon support.
- `constants.py` - All render magic numbers (canvas padding, legend sizing, animation params, debug overlay, etc.). Theme-dependent values remain in `style.py`.

### Themes (`src/nf_metro/themes/`)
- `nfcore.py` - Dark theme (default), matching nf-core visual style.
- `light.py` - Light theme variant.
- New themes: create a `Theme` instance and register in `themes/__init__.py` `THEMES` dict.

## Input Format

`.mmd` files use a subset of Mermaid `graph LR` syntax with `%%metro` directive extensions:

```
%%metro title: Pipeline Name
%%metro style: dark
%%metro line: line_id | Display Name | #hexcolor | style
%%metro line_order: span
%%metro compact_offsets: true
%%metro legend_min_height: 72
%%metro grid: section_id | col,row

graph LR
    subgraph section_id [Section Name]
        %%metro entry: left | line1, line2
        %%metro exit: right | line1, line2
        node_id[Label]
        node_id -->|line_id| other_node
    end
    %% Inter-section edges outside subgraphs
    node_a -->|line_id| node_b
```

Edges support comma-separated line IDs: `a -->|line1,line2,line3| b` creates one edge per line.

Lines support an optional style (4th field): `solid` (default), `dashed`, or `dotted`. Dashed/dotted lines render with SVG `stroke-dasharray` on both edge paths and legend swatches.

## Key Design Decisions

- Stations are mutable dataclasses; layout phases write coordinates directly onto `Station.x`/`.y` fields.
- Port stations (`is_port=True`) participate in layout but are invisible during rendering.
- Layout uses networkx only for DAG operations (topological sort); all coordinate computation is custom.
- Auto-layout (`auto_layout.py`) infers everything from the section DAG, so most `.mmd` files need no `%%metro grid:` directives. Explicit directives override inferred values.

## Station-as-Elbow Constraint (CRITICAL)

**NEVER position a perpendicular port at the same coordinate as an internal station.** This is validated by `check_station_as_elbow` in `tests/layout_validator.py` (10px tolerance).

- TOP/BOTTOM ports on LR/RL sections must NOT share X with any internal station.
- LEFT/RIGHT ports on TB sections must NOT share Y with any internal station.

When fixing routing or alignment issues, do NOT "solve" a kink by moving a port to match a station's coordinate. That creates a station-as-elbow violation where the line visually passes through the station marker. Instead, accept small offsets between ports and stations and handle them via routing (near-vertical drops, gentle curves, etc.).

## Visual Review

**Primary method**: Push to a PR. The CI workflow (`.github/workflows/pr-renders.yml`) renders all gallery examples on both the PR branch and base, generates a before/after visual diff, and posts a preview link:

```
https://pinin4fjords.github.io/nf-metro/_pr/<PR_NUMBER>/
```

This is the authoritative visual review and should be used for all layout or rendering changes.

**Quick local render** (for fast iteration before pushing):

```bash
source ~/.local/bin/mm-activate nf-metro

# Render SVG
python -m nf_metro render examples/rnaseq_sections.mmd -o /tmp/rnaseq_sections.svg --x-spacing 60 --y-spacing 40

# Convert SVG to PNG via cairosvg (scale=2 for retina)
python -c "import cairosvg; cairosvg.svg2png(url='/tmp/rnaseq_sections.svg', write_to='/tmp/rnaseq_sections.png', scale=2)"

# Open it
open /tmp/rnaseq_sections.png
```

The `nf-metro` micromamba environment has the project installed in editable mode along with cairosvg for PNG conversion.

## Test Fixtures & Topology Stress Tests

- Test fixtures: `tests/fixtures/`
- Example pipelines: `examples/` (including `rnaseq_sections.mmd` with manual grid and `rnaseq_auto.mmd` with fully inferred layout)
- Topology stress tests: `tests/fixtures/topologies/*.mmd` - 15 fixtures covering fan-out, fan-in, diamonds, folds, mixed port sides, etc. See `tests/fixtures/topologies/README.md` for full inventory and known issues.
- `tests/layout_validator.py` - Programmatic layout checks (section overlap, station containment, port positioning, edge waypoints).
- `tests/test_topology_validation.py` - Parametrized tests running all validator checks against every topology fixture.
- `scripts/render_topologies.py` - Batch render all fixtures to `/tmp/nf_metro_topology_renders/`.
