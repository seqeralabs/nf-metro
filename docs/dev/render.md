# Render

Rendering turns a laid-out `MetroGraph` (stations, ports, junctions, and
sections with coordinates, plus routed `RoutedPath` polylines) into output
files.  The primary entry point is `render_svg` in
[`src/nf_metro/render/svg.py`](https://github.com/pinin4fjords/nf-metro/blob/main/src/nf_metro/render/svg.py),
which returns an SVG string.  From there, `render_html` wraps that SVG in
an interactive HTML page, and `build_manifest` embeds a data manifest into
the SVG for downstream tooling.

## SVG generation (`svg.py`)

`render_svg(graph, theme, ...)` is the top-level call.  It:

1. Scales theme fonts by `graph.font_scale` (set by the `%%metro font_scale:`
   directive or the `--font-scale` CLI flag).
2. Calls `_render_svg_scaled`, which does the actual drawing via the
   `drawsvg` library.
3. If `graph.animate` is set (or `--animate` was passed), calls
   `render_animation` from `animate.py` to add travelling balls.
4. If a manifest is requested, calls `build_manifest` from `manifest.py`
   and injects it into the SVG element.

`apply_route_offsets(routes, station_offsets)` is also exported from
`svg.py`.  It fans a bundle of co-travelling routes into parallel tracks
by applying the per-station Y offsets computed by `compute_station_offsets`
in `routing/offsets.py`.  This is a separate function (not called inside
`render_svg`) so that `animate.py` can share the same offset-applied paths
rather than recomputing them.

### What gets drawn

`_render_svg_scaled` draws in layers:

1. **Section boxes** - rounded rectangles with optional section labels and
   tick marks for group labels.
2. **Edges** - polylines from `RoutedPath.waypoints`, with quadratic Bézier
   curves at corners (radius computed by `routing/corners.py`).  Where
   `compute_bridges` identifies a non-merging crossing, `_render_bridged_edge`
   draws the under-route with a gap (see [Bridges](#bridges) below).
3. **Station markers** - pill-shaped rectangles (or circles/squares for
   alternative marker styles).  Rail-mode interchange stations span multiple
   rails and are drawn by `_render_rail_pill`.
4. **Icons** - file, files, and folder icons for off-track input nodes (drawn
   by `icons.py`).
5. **Labels** - placed by `layout/labels.py` and rendered with optional
   line-wrapping; positioned above or beside their station.
6. **Legend** - drawn by `legend.py`, auto-positioned to avoid overlapping
   section boxes and routes.  Position can be overridden via
   `%%metro legend_position:` or set to `"none"` (suppressed) for the HTML
   output mode.

## Bridges (`bridges.py`)

Two distinct metro lines may cross at a point that is not a shared station,
port, junction, or merge.  Drawn naively that reads as an interchange.
`compute_bridges` resolves the ambiguity by inserting a short gap in the
under-route where it passes beneath the over-route.

`compute_bridges(graph, routes)` takes the assembled polylines (with offsets
already applied) and:

1. Identifies all genuine pairwise crossings: ignores crossings between the
   same line, crossings at shared endpoints, and crossings within
   `BRIDGE_NODE_TOLERANCE` of any node.
2. For same-line crossings, distinguishes a fan-in/out (two legs that share a
   common ancestor and rejoin at a common descendant) from an independent
   self-crossing that genuinely needs a bridge.
3. Groups nearby crossings into clusters and assigns "over" and "under" by
   2-colouring the cluster graph.
4. Returns a list of `BridgeBreak` objects, one per under-route segment,
   recording the `t_start`/`t_end` parametric range on that segment to omit.

The drawing half lives in `svg.py`: `_render_bridged_edge` splits the
polyline at the gap span and renders each piece separately.

## Interactive HTML (`html.py`)

`render_html(graph, theme, ...)` wraps `render_svg` in a self-contained
HTML page.  It suppresses the SVG legend (the side panel takes its place)
and returns a full HTML string.

The HTML output has two delivery modes:

- **Standalone page** (`_STANDALONE_TEMPLATE`): a full `<!DOCTYPE html>`
  document with a two-column layout (canvas left, legend panel right).
  Supports pan/zoom, per-line focus (click a line chip to dim everything
  else and zoom to visible), station tooltips, and an embed modal that
  generates copy-paste snippets.
- **Inline embed snippet** (`_INLINE_TEMPLATE`, via `_build_inline_snippet`):
  a `<div>` with scoped CSS and an IIFE — paste into any HTML host (MkDocs,
  Confluence, blog templates) without hosting a separate file.

Both modes share a single `_SHARED_JS` block (`attachMetroMap`) so the
interaction behaviour is identical.  The embed snippet scopes CSS under a
per-render hash (`.nfmm-<sha1[:8]>`) so multiple maps can coexist on one
page.

## Manifest (`manifest.py`)

`build_manifest(graph)` maps the laid-out `MetroGraph` onto the
[embedded-manifest standard](../../docs/manifest.md): stations become nodes,
sections become groups, and visual regions (section bboxes) become regions.
The manifest is serialised to JSON and injected as a `<metadata>` element
inside the SVG, keyed by `MANIFEST_ELEMENT_ID`.

The tool-neutral serialisation/deserialisation logic lives in
`nf_metro.manifest` (a dependency-free package built to be lifted into its
own distribution).  `render/manifest.py` is the thin nf-metro-specific
adapter: it imports from `nf_metro.manifest` and re-exports the public API
so that existing `nf_metro.render.manifest` import paths keep working.

`manifest_metadata_svg(graph)` returns the raw SVG `<metadata>` XML string
for cases where the caller assembles the SVG element manually.

## Render-geometry validation (`validate.py`)

The layout guards and routing invariants validate geometry *before* the
render-time regimes run — the per-line offsets `apply_route_offsets` applies,
the multi-line label Y-shifts, and the wrapped-label lift. The picture the
user sees only exists in the emitted SVG, so a class of defect (a line drawn
through a label only after the offsets shift it) is invisible to them.

`validate_render(svg)` closes that gap from the other side. It reads the
finished artifact back into geometry — node markers from the embedded
manifest, route polylines from the drawn `<path data-line-id>` ink (splitting
at each `M` so a bridge-hop gap is a real break, and collapsing each smoothing
`Q` to its corner), and label ink boxes from the drawn `<text>` ink — then
runs render-geometry checks on what was drawn. Three checks run:

- **label-strike** reuses the authoritative `segment_strikes_label` predicate
  at the label's drawn font scale, so a finding means the rendered image is
  wrong, not that a re-derivation diverged.
- **marker-cross** flags a route segment through a non-consumer station's
  marker (a line the station carries, via the manifest `groups`, is exempt).
  Rail-interchange stations are exempt too — a line threading an interchange
  knob is the intended rail idiom — and their ids are read back from the drawn
  `...-rail-...` markers, since the manifest carries no rail flag.
- **offset-collapse** flags two distinct lines drawn flush where the offset
  regime assigned them a full `OFFSET_STEP` apart. Two lines on the same
  assigned slot draw flush by design (a shared-trunk bundle), so the check
  compares the drawn gap against the gap the regime *assigned*, not a constant.

label-strike and marker-cross are pure artifact oracles (the SVG string is
enough), so they run on a produced file in CI, via `validate-svg --geometry`,
and behind `render --validate`. offset-collapse needs the engine's assigned
offsets to tell an intended same-slot bundle from a real merge, so
`validate_render(svg, *, graph=...)` runs it only when the laid-out graph is
supplied — the `render --validate` path and the CI corpus test, not the
standalone SVG path.

## Animation (`animate.py`)

`render_animation(d, graph, routes, station_offsets, theme)` appends
animated `<circle>` elements with `<animateMotion>` to an existing
`drawsvg.Drawing`.  It is called by `_render_svg_scaled` when `animate` is
`True`.

Each metro line gets one ball.  All balls are synchronised to the same
cycle duration (`max_dur`, chosen so the slowest ball just finishes one
lap per cycle) via `keyTimes`/`keyPoints`, so no ball restarts while
another is still mid-track.

## Theming (`style.py`)

`Theme` is a frozen dataclass of visual properties: colours, font sizes,
line widths, station radii, animation speed, and legend layout.

Built-in themes live in `src/nf_metro/themes/`:
- `nfcore.py` — dark theme (default), matching nf-core visual style.
- `light.py` — light theme variant.

To add a theme: create a `Theme` instance and register it in
`themes/__init__.py`'s `THEMES` dict under a string key; it then becomes
selectable via `%%metro style: <key>` or `--style`.

## Module map

| Module | Responsibility |
| --- | --- |
| `svg.py` | `render_svg` entry point; `apply_route_offsets`; all drawing passes (sections, edges, stations, icons, labels, legend) |
| `bridges.py` | `compute_bridges` — detects genuine non-merging crossings and returns `BridgeBreak` gap spans; drawing is in `svg.py` |
| `html.py` | `render_html` — standalone HTML page and inline embed snippet around the SVG |
| `manifest.py` | nf-metro adapter for the embedded-manifest standard; `build_manifest`, `manifest_metadata_svg` |
| `validate.py` | `validate_render` — render-geometry guards that read the drawn SVG (markers, route ink, label ink) as their own oracle |
| `animate.py` | `render_animation` — animated balls via `<animateMotion>` |
| `style.py` | `Theme` dataclass |
| `legend.py` | `render_legend`, `compute_legend_dimensions` |
| `icons.py` | `render_file_icon`, `render_files_icon`, `render_folder_icon` |
| `constants.py` | render magic numbers (canvas padding, legend sizing, animation params, debug overlay); theme-dependent values remain in `style.py` |
