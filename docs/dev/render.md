---
title: "Render"
description: How a laid-out MetroGraph becomes an SVG, including animation, bridges, legends, and themes.
sidebar:
  order: 9
---

Rendering has two steps.
First, `build_render_plan` copies a laid-out `MetroGraph` and finishes the render-specific geometry.
It stores the result in an immutable `RenderPlan`.
Second, an emitter turns that plan into SVG or HTML.

The split prevents rendering from changing the caller's graph, and it gives validators and metrics the exact geometry used to draw the output.
The plan stores the resolved theme and every setting that affects geometry.
Emitters accept only output options such as animation and responsive sizing.

The main entry point is `render_svg` in [`src/nf_metro/render/svg.py`](https://github.com/seqeralabs/nf-metro/blob/main/src/nf_metro/render/svg.py), which performs both steps and returns an SVG string.
Callers that need to reuse a plan can call `build_render_plan`, `emit_render_plan`, or `emit_render_plan_html` directly.

The internal [candidate executor](/nf-metro/dev/candidate_execution/) observes the route plan used by the final `RenderPlan`.
That matters when render-time geometry settling causes a reroute, because acceptance evidence must describe the SVG that was actually emitted rather than an earlier routing pass.

`build_render_plan` makes one deep copy of the graph.
Render-specific routing, label placement, and section adjustments run on this private copy.
A private copy is safer than changing the caller's graph and trying to restore it after an error.
Across three representative maps the copy took 1.8 to 2.2 ms, or 1.6% to 2.8% of the combined plan-build and SVG-emission time.

## Deterministic text metrics

`src/nf_metro/text_metrics.py` owns text advances, ink bounds, line heights, and reserved widths.
Each measurement carries a semantic role, such as station label, section header, legend entry, or icon caption.
That keeps the safety margin for each use explicit while sharing one deterministic measurement path.

The default SVG mode keeps the Helvetica-family output and its conservative per-role reservations.
Because its proportional advance table is bundled in the package, layout never searches the host for an installed font.
`--embed-font` and `--text-to-paths` instead select exact Inter metrics from generated tables shipped in `src/nf_metro/_inter_metrics.py`.
Those tables come from the same bundled Inter Regular and Bold WOFF2 files used by the output, and weights 600, 700, and `bold` all select Inter Bold.
Unsupported characters use the advance, bounds, and outline of the visible `?` replacement.

The runtime path has no FontTools dependency.
Maintainers can regenerate the tables after intentionally replacing the bundled fonts with:

```bash
python scripts/build_text_metrics.py
```

The generator requires FontTools with WOFF2 support.
Commit the generated table alongside the font files so metrics and portable output cannot drift apart.

## SVG generation (`svg.py`)

`render_svg(graph, theme, ...)` is the top-level call.
It:

1. Scales theme fonts by `graph.font_scale`, which the `%%metro font_scale:` directive or the `--font-scale` CLI flag sets.
   It also scales stroke widths and station pills by `graph.stroke_scale`.
   Layout reserves space with the same scale values.
2. Builds a `RenderPlan` from a private graph copy.
3. Calls `emit_render_plan`, which draws the plan with `drawsvg`.
4. If `graph.animate` is set, or `--animate` was passed, calls `render_animation` from `animate.py` to add traveling balls.
5. If requested, embeds the plan's node, group, region, marker, and canvas geometry as a manifest.

`apply_route_offsets(routes, station_offsets)` lives in `layout/routing/common.py`.
It separates a route bundle into parallel tracks, using the per-station offsets from `compute_station_offsets` in `routing/offsets.py`.
Animation uses the same offset paths.

### Draw order

`emit_render_plan` draws in layers:

1. **Section boxes.** Rounded rectangles with optional section labels and tick marks for group labels.
2. **Edges.** Polylines from `RoutedPath.points`, with quadratic Bézier curves at corners, whose radius `routing/corners.py` computes.
   Where `compute_bridges` identifies a non-merging crossing, `_render_bridged_edge` draws the under-route with a gap (see [Bridges](#bridges-bridgespy)).
3. **Station markers.** Pill-shaped rectangles, or circles and squares for alternative marker styles.
   Rail-mode interchange stations span several rails and are drawn by `_render_rail_pill`.
4. **Icons.** File, files, and folder icons for off-track input nodes, drawn by `icons.py`.
5. **Labels.** Placed by `layout/labels.py` and rendered with optional line-wrapping, positioned above or beside their station.
6. **Legend.** Drawn by `legend.py` and auto-positioned to avoid overlapping section boxes and routes.
   Override its position with `%%metro legend:`, or set it to `"none"` to suppress it for the HTML output mode.

### Canvas sizing

The canvas is a first-quadrant frame.
Its `viewBox` always starts at `0 0` so overlays can share it without an outer transform (see [`manifest/__init__.py`](https://github.com/seqeralabs/nf-metro/blob/main/src/nf_metro/manifest/__init__.py)).
Width and height come from `_compute_canvas_bounds` plus a margin: `CANVAS_PADDING` on the right and the watermark band at the bottom.
The far edges therefore grow to hold whatever the render draws.

The near edges cannot grow.
The map moves instead.
`_settle_clear_of_the_canvas_margins` measures the ink that lands outside the section-box envelope on the left or top.
That ink is typically an inter-row return band wrapping around the first box of a row, or a bundle rising over the top of one.
It then moves the whole laid-out graph away from the edge by the shortfall, using `translate_graph` in `layout/phases/canvas.py`.
That function owns the full set of absolute coordinates a graph carries.
Routing is re-derived on the moved copy, because where a run lands is only known once it is routed.
A map that draws nothing outside its box envelope never moves.

The room such a run is owed is `CANVAS_ORIGIN_MARGIN` rather than `CANVAS_PADDING`.
The near sides already have something placed against them, namely the first section's box edge and the header badge above its top.
A run settled anywhere else would read as a second boundary beside that one.
The far sides have nothing placed against them, and they keep the flat padding.

`_content_origin` reports the left and top edges that the move settles on: the box envelope, carried outwards by any run drawn past it.
A decoration the author left unpinned is placed against those edges rather than against a box.
The legend therefore sits flush with the content whether a box or a run defines the boundary.
An authored pin, whether `legend: x,y`, `| canvas`, or `| dx,dy`, is placed as written.

## Bridges (`bridges.py`)

Two distinct metro lines may cross at a point that is not a shared station, port, junction, or merge.
Drawn plainly, that reads as an interchange.
`compute_bridges` resolves the ambiguity by inserting a short gap in the under-route where it passes beneath the over-route.

`compute_bridges(graph, routes)` takes the assembled polylines, with offsets already applied, and:

1. Identifies all genuine pairwise crossings, ignoring crossings between the same line, crossings at shared endpoints, and crossings within `BRIDGE_NODE_TOLERANCE` of any node.
2. For same-line crossings, distinguishes a fan-in or fan-out from an independent self-crossing that needs a bridge.
   Fan legs share a common ancestor and rejoin at a common descendant.
3. Groups nearby crossings into clusters and assigns "over" and "under" by 2-coloring the cluster graph.
4. Returns a list of `BridgeBreak` objects, one per under-route segment, each recording the segment index and the `cut_a`/`cut_b` endpoints of the span to omit.

The drawing half lives in `svg.py`, where `_render_bridged_edge` splits the polyline at the gap span and renders each piece separately.

## Interactive HTML (`html.py`)

`render_html(graph, theme, ...)` builds a plan without an SVG legend, because the HTML side panel replaces it.
It then passes the plan to `emit_render_plan_html` and returns a complete HTML page.

The HTML output has two delivery modes:

- **Standalone page** (`_STANDALONE_TEMPLATE`): a full `<!DOCTYPE html>` document with a two-column layout, canvas on the left and legend panel on the right.
  It supports pan and zoom, station tooltips, and an embed modal that generates copy-paste snippets.
  Per-line focus dims everything but the selected line chip and zooms to what remains visible.
- **Inline embed snippet** (`_INLINE_TEMPLATE`, through `_build_inline_snippet`): a `<div>` with scoped CSS and an IIFE, which pastes into any HTML host such as MkDocs, Confluence, or a blog template without hosting a separate file.

Both modes use the same driver from `get_driver_js`.
The interaction behavior is identical.
The embed snippet scopes CSS under a per-render hash (`.nfmm-<sha1[:8]>`) so multiple maps can coexist on one page.

## Manifest (`manifest.py`)

Plan construction maps the final render geometry onto the [embedded-manifest standard](/nf-metro/manifest/): stations become nodes, sections become groups, and visual regions (section bboxes) become regions.
The manifest is serialized to JSON and injected as a `<metadata>` element inside the SVG, keyed by `MANIFEST_ELEMENT_ID`.

The tool-neutral serialization and deserialization logic lives in `nf_metro.manifest`, a dependency-free package built to be lifted into its own distribution.
`render/manifest.py` is the thin nf-metro-specific adapter.
It imports from `nf_metro.manifest` and re-exports the public API, which keeps existing `nf_metro.render.manifest` import paths working.

`manifest_metadata_svg(manifest)` returns the raw SVG `<metadata>` XML string for cases where the caller assembles the SVG element manually.

## Render-geometry validation (`validate.py`)

Layout guards run before rendering applies line offsets and final label adjustments.
Some problems therefore appear only in the finished SVG.
An offset can move a line across a label, for example.

`validate_render(svg)` checks the finished output.
It reads station markers from the embedded manifest, routes from the drawn path elements, and labels from the drawn text elements.
It then runs three checks:

- **label-strike** finds lines drawn through station-label text, using `segment_strikes_label` at the rendered font size.
- **marker-cross** flags a route segment through a non-consumer station's marker.
  A station that carries the line is exempt, as are rail interchanges, where lines are meant to pass through the markers.
- **offset-collapse** flags two distinct lines drawn flush where the offset plan placed them at least one `OFFSET_STEP` apart.
  Lines assigned to the same slot may share a track by design.

The label and marker checks need only the SVG, and they run in `validate-svg --geometry` and `render --validate`.
The offset check also needs the original `RenderPlan`.
To enable it, call `validate_render(svg, plan=plan)`.

## Animation (`animate.py`)

`render_animation(d, graph, routes, station_offsets, theme)` appends animated `<circle>` elements to an existing `drawsvg.Drawing`.
CSS `offset-path` and `@keyframes` move each circle along its line.
`emit_render_plan` calls it when `animate` is `True`.

`animate.py` uses CSS animation rather than SMIL `<animateMotion>` because SMIL does not run when a host page injects an SVG through `innerHTML`, as happens in the playground preview, the inline embed snippet, and any host that inlines an exported map.
The timeline advances but the motion is never sampled, and every ball freezes at its path start.
CSS `offset-path` animates whether the SVG is opened standalone, referenced from `<img>`, or inlined.

Each metro line gets one ball.
Every ball is synchronized to the same cycle duration, `max_dur`, chosen so the slowest ball finishes exactly one lap per cycle.
Through a three-stop `@keyframes`, a shorter line covers its path in the first `move_frac` of the cycle and holds at the terminus for the rest.
No ball restarts while another is mid-track.

## Theming (`style.py`)

`Theme` is a keyword-only dataclass of visual properties: colors, font sizes, line widths, station radii, animation speed, and legend layout.

Brand identity and display mode are orthogonal axes.
Built-in themes live in `src/nf_metro/themes/`:

- `nfcore.py` holds the nf-core brand as a light/dark pair, `NFCORE_LIGHT_THEME` and `NFCORE_DARK_THEME`.
  They share one set of fonts and one line and station geometry, and differ only in the chrome palette.
- `seqera.py` holds the Seqera Platform brand, likewise as a light/dark pair.
- `light.py` holds `LIGHT_THEME`, the transparent embed theme with `background_color="none"`.
  It belongs to no brand family and has no mode counterpart.

`themes/__init__.py` holds two registries.
`THEME_MODES` maps a brand name to its `{light, dark}` pair.
`THEMES` is the flat by-name registry that direct selection looks up, covering the bare brand names resolved at `DEFAULT_MODE`, the mode-suffixed names such as `nfcore-light` and `seqera-dark`, and `light`.
`resolve_theme(theme, graph, mode)` combines the two axes.
Brand comes from the explicit name or the graph's `style`, where `dark` is an alias for `nfcore`, and mode comes from the explicit argument, `%%metro mode:` or `DEFAULT_MODE`.

Because the axes are separable, one render carries both palettes.
`mode_pair(theme)` recovers a brand's light and dark variants, and the chrome colors emit as CSS `light-dark(<light>, <dark>)`.
A single SVG therefore adapts to the viewer's `color-scheme`.
`--no-chrome-css` bakes one concrete palette instead, for consumers that cannot parse `light-dark()`.

To add a brand, define a light and a dark `Theme` sharing one `brand` value, register the pair under `THEME_MODES`, and add its names to `THEMES`.
It is then selectable through `%%metro style: <brand>` and `%%metro mode: <mode>`, or the `--theme` and `--mode` options.

## Module map

| Module              | Responsibility                                                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `plan.py`           | Immutable `RenderPlan` data and conversion helpers                                                                                    |
| `svg.py`            | `render_svg`, plan construction, SVG output, and drawing passes                                                                       |
| `bridges.py`        | `compute_bridges` - detects genuine non-merging crossings and returns `BridgeBreak` gap spans. Drawing is in `svg.py`                 |
| `html.py`           | `render_html` - standalone HTML page and inline embed snippet around the SVG                                                          |
| `manifest.py`       | nf-metro adapter for the embedded-manifest standard. `build_manifest`, `manifest_metadata_svg`                                        |
| `validate.py`       | `validate_render` - render-geometry guards that read the drawn SVG (markers, route ink, label ink) as their own oracle                |
| `animate.py`        | `render_animation` - animated balls via CSS `offset-path` + `@keyframes`                                                              |
| `style.py`          | `Theme` dataclass                                                                                                                     |
| `legend.py`         | `render_legend`, `compute_legend_dimensions`                                                                                          |
| `icons.py`          | `render_file_icon`, `render_files_icon`, `render_folder_icon`                                                                         |
| `driver.py`         | `get_driver_js` - the versioned `attachMetroMap` embed driver shared by the standalone page and the inline snippet                    |
| `font_embed.py`     | `embed_font` (inline an Inter subset as base64 `@font-face`) and `text_to_paths` (convert `<text>` to `<path>`) for font-portable SVG |
| `ns.py`             | `ns`, `class_prefix_context`, `adaptive_logo_mask_ids` - SVG class-namespace helpers shared across render modules                     |
| `path_geometry.py`  | render-time path geometry derived from the frozen route decisions                                                                     |
| `section_header.py` | `resolve_section_header_placement` - keeps a section's number badge and title clear of routes without moving a route                  |
| `constants.py`      | render magic numbers (canvas padding, legend sizing, animation params, debug overlay). Theme-dependent values remain in `style.py`    |
