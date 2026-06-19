# Embed contract

!!! warning "Experimental - pre-1.0, not yet stable"
    The embed contract is new and still being shaped.  The `data-*` attribute
    names, the driver API, and the manifest schema may change without notice
    before the format is declared stable.  Pin to a specific nf-metro release
    if you depend on the exact surface.  The driver contract version
    (`DRIVER_CONTRACT_VERSION`) and manifest schema version
    (`MANIFEST_SCHEMA_VERSION`) are both `1.0` at this stage.

An nf-metro SVG is a **self-describing, driveable artifact**.  A host page
can:

1. Inline the SVG (or load it via `<img>` / `<object>`).
2. Load one driver script.
3. Call a documented API to highlight lines, select nodes by process pattern,
   or read the embedded manifest without touching internals.

This page documents the two halves of the contract: the **`data-*` attributes**
carried by the SVG and the **driver API** that a host uses to manipulate it.
The [Data manifest](manifest.md) page covers the manifest format (nodes, groups,
regions, overlays) in more depth.

---

## `data-*` attribute contract

Every rendered SVG carries two complementary sets of attributes.

### Interactive set

These attributes are consumed by the driver and are the stable addresses for
CSS-level interaction:

| Attribute | Element | Value |
|-----------|---------|-------|
| `data-station-id` | Station marker `<rect>`/`<circle>` and associated label/icon `<g>` | The station's stable id (matches `node.id` in the manifest). |
| `data-station-lines` | Station marker element only | Comma-separated list of line ids passing through the station. |
| `data-station-label` | Station marker element only | Human-readable label (HTML-escaped). |
| `data-section-id` | Section box and associated label `<g>` | The section's stable id (matches `region.id` in the manifest). |
| `data-section-name` | Station marker elements within a section | Human-readable section name (HTML-escaped). |
| `data-section-lines` | Section box element only | Comma-separated list of line ids present in the section. |
| `data-line-id` | Edge path elements | The id of the line this edge belongs to. |

**Querying examples:**

```js
// All station markers for a specific station id:
svg.querySelectorAll('[data-station-id="align"]')

// All edges belonging to a line:
svg.querySelectorAll('[data-line-id="star_salmon"]')

// All section boxes that include a given line:
svg.querySelectorAll('[data-section-lines]')
  .forEach(el => {
    const lines = el.getAttribute('data-section-lines').split(',');
    if (lines.includes('star_salmon')) { /* ... */ }
  });
```

### Manifest set

These attributes are written by the manifest system and carry the coordinate
and pattern data for overlays.  They are documented in full on the [Data
manifest](manifest.md) page.

| Attribute | Element | Value |
|-----------|---------|-------|
| `data-node-id` | Station wrapper `<g>` | Stable id, matches `data-station-id` and `node.id` in the manifest. |
| `data-node-cx` | Station wrapper `<g>` | Centre x in SVG user units. |
| `data-node-cy` | Station wrapper `<g>` | Centre y in SVG user units. |
| `data-node-r` | Station wrapper `<g>` | Nominal marker radius. |
| `data-node-groups` | Station wrapper `<g>` | Comma-separated line ids (same as `data-station-lines`). |
| `data-node-region` | Station wrapper `<g>` | Section id, omitted when the station has no section. |

Both sets join on the station id (`data-station-id` = `data-node-id` =
`node.id` in the manifest JSON).

---

## Driver API

### Obtaining the driver

**Option A - embed the HTML output** (simplest).  `nf-metro render --format
html` produces a fully self-contained interactive page with the driver already
inlined.  Copy the inline snippet from the Embed modal to paste it into any
host page.

**Option B - load the driver separately**.  Export the driver script and load
it alongside the SVG:

```bash
nf-metro embed-script -o nf-metro-embed.js
```

Then on the host page:

```html
<!-- 1. Inline the SVG (must contain data-* attributes and manifest) -->
<div id="my-map">
  <div class="nf-metro-canvas">
    <!-- paste SVG here -->
  </div>
  <div class="nf-metro-legend"></div>
  <div class="nf-metro-tip"></div>
</div>

<!-- 2. Load the driver -->
<script src="nf-metro-embed.js"></script>

<!-- 3. Attach and capture the API -->
<script>
const api = attachMetroMap({
  root: document.getElementById('my-map'),
  lines: [
    { id: 'star_salmon', label: 'STAR + Salmon', color: '#e05c5c', style: 'solid' },
    /* ... */
  ],
  embed: null,
});
</script>
```

The `lines` array must match the lines embedded in the SVG.  The easiest way to
obtain it is from the `groups` array in the manifest (see
[`getManifest`](#getmanifestid) below).

### API methods

`attachMetroMap(opts)` returns an API object with the following methods.  All
methods are no-ops when the SVG has no manifest or no matching elements.

#### `highlightLine(id)`

Activate a line by its id string.  All stations and edges not belonging to
that line are hidden; the map zooms to the visible subset.  Calling with the
currently active id clears the filter (same as `clearHighlight()`).

```js
api.highlightLine('star_salmon');
```

#### `clearHighlight()`

Remove any active line filter and station selection, returning the map to its
initial unfiltered state.

```js
api.clearHighlight();
```

#### `getManifest()`

Return the embedded manifest JSON object (parsed from the `<metadata
id="diagram-manifest">` element), or `null` if the SVG has no manifest.  Use
this to build `lines` arrays, read node coordinates for overlays, or look up
process patterns.

```js
const manifest = api.getManifest();
if (manifest) {
  console.log(manifest.nodes.map(n => n.id));
}
```

#### `selectNode(processName)`

Match `processName` against each node's `patterns` array (case-insensitive
regex) and visually highlight the matching stations.  Non-matching stations are
dimmed.  Calling with a string that matches no node is a no-op.

```js
// Highlight the station(s) whose patterns match this Nextflow process name:
api.selectNode('NFCORE_RNASEQ:RNASEQ:ALIGN_STAR_SALMON:STAR_ALIGN');
```

CSS classes written by `selectNode`:

| Class | Applied to |
|-------|-----------|
| `nf-metro-station-selected` | Matching station marker elements (`[data-station-lines]`). |
| `nf-metro-station-dim` | All `[data-station-id]` elements that are not a match. |
| `nf-metro-selecting` | The root element while a selection is active. |

The default templates ship CSS for these classes.  When loading the driver
separately, add your own styles:

```css
.nf-metro-station-selected rect,
.nf-metro-station-selected circle { stroke: #fff; stroke-width: 2; }
.nf-metro-station-dim { opacity: 0.2; transition: opacity 0.2s; }
```

#### `reset()`

Alias for `clearHighlight()`.

---

## Overlay path

For a coordinate-accurate progress overlay (e.g. lighting up stations as a
pipeline runs), use the manifest `overlay_svg()` helper to create a
transparent SVG layer that shares the base SVG's `viewBox`:

```python
from nf_metro.manifest import read_manifest, matching_node_ids, overlay_svg

base_svg = open('map.svg').read()
manifest = read_manifest(base_svg)

# Find which station(s) represent a running process:
ids = matching_node_ids(manifest, 'NFCORE_RNASEQ:RNASEQ:ALIGN_STAR_SALMON:STAR_ALIGN')

# Build status markers:
markers = ''.join(
    f'<circle cx="{n["x"]}" cy="{n["y"]}" r="{n["r"] + 4}" '
    f'fill="none" stroke="#4c9" stroke-width="2"/>'
    for n in manifest['nodes'] if n['id'] in ids
)
overlay = overlay_svg(manifest, body=markers)
```

Stack the overlay over the base SVG in your HTML using absolute positioning
and matching `viewBox` values.  The manifest's `width`/`height` fields give
the shared coordinate space.

The `highlightLine` / `selectNode` API and the overlay approach are
complementary:

- **Driver API** - manipulates the base SVG's existing DOM elements by adding
  CSS classes.  Zero extra elements; works without the manifest.
- **Overlay** - adds new elements in a separate layer at exact coordinates from
  the manifest.  Suitable for progress indicators, status badges, and
  annotation.

---

## Integration example

The following snippet builds a self-contained host page that loads a
separately-generated SVG and driver, then wires keyboard shortcuts to the
public API.

```html
<!doctype html>
<html>
<head>
<style>
  #map-root { position: relative; }
  .nf-metro-canvas svg { width: 100%; height: auto; }
  .nf-metro-legend { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; }
  .nf-metro-tip { position: fixed; pointer-events: none; }
  /* Station selection styles */
  .nf-metro-station-selected rect,
  .nf-metro-station-selected circle { stroke: #4cf; stroke-width: 2; }
  .nf-metro-station-dim { opacity: 0.15; transition: opacity 0.2s; }
</style>
</head>
<body>

<div id="map-root">
  <div class="nf-metro-canvas">
    <!-- Inline the SVG exported by: nf-metro render map.mmd -o map.svg -->
  </div>
  <div class="nf-metro-legend"></div>
  <div class="nf-metro-tip"></div>
</div>

<script src="nf-metro-embed.js"></script>
<script>
const manifest = (() => {
  const el = document.querySelector('#diagram-manifest');
  return el ? JSON.parse(el.textContent) : null;
})();

const lines = (manifest?.groups || []).map(g => ({
  id: g.id, label: g.label, color: g.color, style: 'solid',
}));

const api = attachMetroMap({
  root: document.getElementById('map-root'),
  lines,
  embed: null,
});

// Example: drive from your application state
function onProcessStarted(fqProcessName) {
  api.selectNode(fqProcessName);
}

function onPipelineDone() {
  api.clearHighlight();
}
</script>
</body>
</html>
```

---

## Versioning

Both the manifest schema and the driver contract are versioned.  The Python
constants are:

```python
from nf_metro.manifest import MANIFEST_SCHEMA_VERSION   # e.g. "1.0"
from nf_metro.render.driver import DRIVER_CONTRACT_VERSION  # e.g. "1.0"
```

The schema version follows `major.minor` semantics: the minor part increments
for additive (backward-compatible) changes; the major part increments for
breaking changes.  Consumers must ignore unknown fields (additive tolerance).

Until the contract is declared stable (version `2.0` or a dedicated
stability notice), pin to a specific nf-metro release when building against
this surface.
