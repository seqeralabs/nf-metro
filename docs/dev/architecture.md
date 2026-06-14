# Architecture

nf-metro turns a Mermaid `graph LR` definition (augmented with `%%metro`
directives) into a metro-map-style SVG.  The pipeline has three stages:

```
Parse  ->  Layout  ->  Render
```

Each stage hands a single `MetroGraph` (defined in
[`src/nf_metro/parser/model.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/parser/model.py))
to the next.  The graph is mutated in place: parsing fills in stations,
edges, lines, sections, and ports; layout writes coordinates onto those
objects; rendering reads them.

## Stages

### Parse (`src/nf_metro/parser/`)

`parse_metro_mermaid` in `parser/mermaid.py` is a line-by-line regex
parser.  It reads Mermaid subgraphs (sections), nodes (stations), and
edges, plus the `%%metro` directive extensions.  After the line scan it
runs a post-parse pass that auto-infers layout, then rewrites
inter-section edges into port/junction chains via `_resolve_sections`.

See [Parser](parser.md) for the directive model and the
parse-then-resolve flow.

### Layout (`src/nf_metro/layout/`)

`compute_layout` in `layout/engine.py` assigns every station, port,
junction, and section bbox an `(x, y)`.  It chains many small phases,
split into an anchor-setting (structural) layer and a content-placement
layer.  The phase implementations live in `layout/phases/`; the
per-phase preconditions, postconditions, and invariants are documented
in
[`src/nf_metro/layout/CONTRACT.md`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md).

Edge routing (horizontal runs plus 45-degree diagonal transitions, with
L-shaped inter-section routing) lives in `layout/routing/`.

See [Layout pipeline](layout_pipeline.md) for the full phase-by-phase
walkthrough and [Routing](routing.md) for the route families.

### Render (`src/nf_metro/render/`)

`render_svg` in `render/svg.py` draws section boxes, routed edges (with
curved corners), pill-shaped station markers, labels, and the legend
using the `drawsvg` library.  Visual properties come from a `Theme`
(`render/style.py`); themes are registered in `themes/__init__.py`.
`render/animate.py` adds travelling balls along the routed paths (the
`--animate` CLI flag).

## Reference docs

| Topic | Where |
| --- | --- |
| Layout phase-by-phase | [layout_pipeline.md](layout_pipeline.md) |
| Per-phase contract (preconditions/postconditions/invariants) | [`CONTRACT.md`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/layout/CONTRACT.md) |
| Edge routing families | [routing.md](routing.md) |
| Parser and `%%metro` model | [parser.md](parser.md) |
| SVG, HTML, bridges, manifest, animation | [render.md](render.md) |
| Adding fixtures, tests, invariants | [testing.md](testing.md) |
| Topology stress fixtures (inventory + known issues) | [`examples/topologies/README.md`](https://github.com/pinin4fjords/nf-metro/blob/main/examples/topologies/README.md) |

## Data model

The central structure is `MetroGraph` (`parser/model.py`), holding:

- `lines` (`MetroLine`): coloured routes, with an optional `style`
  (`solid` / `dashed` / `dotted`).
- `stations` (`Station`): mutable dataclasses; layout writes `x`, `y`,
  `layer`, `track` directly onto them.  `is_port` stations participate
  in layout but are invisible at render time.
- `edges` (`Edge`): directed, each tagged with a `line_id`.
- `sections` (`Section`): subgraph groupings with a `direction`
  (`LR` / `RL` / `TB`), grid position, and bbox.
- `ports` (`Port`): synthetic entry/exit points on section boundaries,
  created during `_resolve_sections`.
