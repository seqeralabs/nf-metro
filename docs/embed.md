---
title: "Embed contract: `data-*` attributes and driver API"
description: Reference for the stable data-* attribute vocabulary and driver API surface that host applications depend on.
---

This is the **reference** for the stable surface a host depends on: the exact attribute vocabulary and the driver method signatures.
For a task-oriented introduction that covers which output to produce and how to size, theme, and drive a map, start with the [Embedding guide](/nf-metro/embedding/).

:::note[Stable as of nf-metro 1.0]
The embed contract is a public, versioned surface.
The `data-*` attribute names, the driver API, and the manifest schema are governed by the driver contract version (`DRIVER_CONTRACT_VERSION`) and manifest schema version (`MANIFEST_SCHEMA_VERSION`), both `1.0`.
Changes follow `major.minor` semantics (see [Versioning](#versioning)).
Additive changes bump the minor version, and breaking changes bump the major.
Consumers must tolerate unknown fields.
:::

An nf-metro SVG is a **self-describing, driveable artifact**.
A host page can:

1. Inline the SVG (or load it via `<img>` / `<object>`).
2. Load one driver script.
3. Call a documented API to highlight lines, select nodes by process pattern, or read the embedded manifest without touching internals.

The contract has two halves: the **`data-*` attributes** carried by the SVG and the **driver API** a host uses to manipulate it.
The [Data manifest](/nf-metro/manifest/) page covers the manifest format (nodes, groups, regions, and overlays) in more depth.

---

## `data-*` attribute contract

Every rendered SVG carries two sets of attributes.

### Interactive set

The driver consumes these attributes, and they are the stable addresses for CSS-level interaction:

| Attribute            | Element                                                            | Value                                                          |
| -------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------- |
| `data-station-id`    | Station marker `<rect>`/`<circle>` and associated label/icon `<g>` | The station's stable id (matches `node.id` in the manifest).   |
| `data-station-lines` | Station marker element only                                        | Comma-separated list of line ids passing through the station.  |
| `data-station-label` | Station marker element only                                        | Human-readable label (HTML-escaped).                           |
| `data-section-id`    | Section box and associated label `<g>`                             | The section's stable id (matches `region.id` in the manifest). |
| `data-section-name`  | Station marker elements within a section                           | Human-readable section name (HTML-escaped).                    |
| `data-section-lines` | Section box element only                                           | Comma-separated list of line ids present in the section.       |
| `data-line-id`       | Edge path elements                                                 | The id of the line this edge belongs to.                       |

**Query examples:**

```js
// All station markers for a specific station id:
svg.querySelectorAll('[data-station-id="align"]');

// All edges belonging to a line:
svg.querySelectorAll('[data-line-id="star_salmon"]');

// All section boxes that include a given line:
svg.querySelectorAll("[data-section-lines]").forEach((el) => {
  const lines = el.getAttribute("data-section-lines").split(",");
  if (lines.includes("star_salmon")) {
    /* ... */
  }
});
```

### Manifest set

A second set carries the coordinate and pattern data that overlays need: `data-node-id`, `data-node-cx`/`-cy`/`-r`, `data-node-groups`, and `data-node-region`.
The manifest system writes them, and [Per-node attributes](/nf-metro/manifest/#per-node-attributes) on the Data manifest page specifies them in full.

Both sets join on the station id (`data-station-id` = `data-node-id` = `node.id` in the manifest JSON).

---

## Driver API

### Obtain the driver

**Option A: embed the HTML output.** `nf-metro render --format html` produces a self-contained interactive page with the driver already inlined.
Copy the inline snippet from the Embed modal and paste it into any host page.

**Option B: load the driver separately.** Export the driver script and load it alongside the SVG:

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
    root: document.getElementById("my-map"),
    lines: [
      {
        id: "star_salmon",
        label: "STAR + Salmon",
        color: "#e05c5c",
        style: "solid",
      },
      /* ... */
    ],
    embed: null,
  });
</script>
```

The `lines` array must match the lines embedded in the SVG.
Build it from the `groups` array in the manifest (see [`getManifest`](#getmanifest)).

### API methods

`attachMetroMap(opts)` returns an API object with the following methods.
Every method is a no-op when the SVG has no manifest or no matching elements.

#### `highlightLine(id)`

Activate a line by its id string.
The driver hides stations and edges that do not belong to that line, then zooms the map to the visible subset.
Calling it with the currently active id clears the filter, the same as `clearHighlight()`.

```js
api.highlightLine("star_salmon");
```

#### `clearHighlight()`

Remove any active line filter and station selection, returning the map to its initial unfiltered state.

```js
api.clearHighlight();
```

#### `getManifest()`

Return the embedded manifest JSON object, parsed from the `<metadata id="diagram-manifest">` element, or `null` if the SVG has no manifest.
Use it to build `lines` arrays, read node coordinates for overlays, or look up process patterns.

```js
const manifest = api.getManifest();
if (manifest) {
  console.log(manifest.nodes.map((n) => n.id));
}
```

#### `selectNode(processName)`

Match `processName` against each node's `patterns` array using a case-insensitive regex, then highlight the matching stations and dim the rest.
A string that matches no node is a no-op.

```js
// Highlight the station(s) whose patterns match this Nextflow process name:
api.selectNode("NFCORE_RNASEQ:RNASEQ:ALIGN_STAR_SALMON:STAR_ALIGN");
```

CSS classes written by `selectNode`:

| Class                       | Applied to                                                 |
| --------------------------- | ---------------------------------------------------------- |
| `nf-metro-station-selected` | Matching station marker elements (`[data-station-lines]`). |
| `nf-metro-station-dim`      | All `[data-station-id]` elements that are not a match.     |
| `nf-metro-selecting`        | The root element while a selection is active.              |

The default templates ship CSS for these classes.
If you load the driver separately, add your own styles:

```css
.nf-metro-station-selected rect,
.nf-metro-station-selected circle {
  stroke: #fff;
  stroke-width: 2;
}
.nf-metro-station-dim {
  opacity: 0.2;
  transition: opacity 0.2s;
}
```

#### `reset()`

Alias for `clearHighlight()`.

---

## Overlay path

For a coordinate-accurate progress overlay, such as lighting up stations as a pipeline runs, draw a transparent layer that shares the base SVG's `viewBox`.
Place markers at each node's manifest coordinates.
The [`overlay_svg()`](/nf-metro/manifest/#the-toolkit-functions) helper builds that layer, and the manifest tutorial, [Light up a diagram as a job runs](/nf-metro/manifest/#tutorial-light-up-a-diagram-as-a-job-runs), walks through the full read-match-draw recipe.

The `highlightLine` and `selectNode` API and the overlay approach solve different problems:

- **Driver API.** Manipulates the base SVG's existing DOM elements by adding CSS classes.
  It adds no elements and works without the manifest.
- **Overlay.** Adds new elements in a separate layer at exact coordinates from the manifest.
  Use it for progress indicators, status badges, and annotation.

---

## Integration example

This snippet builds a self-contained host page that loads a separately generated SVG and driver, then drives the public API from application state.

```html
<!doctype html>
<html>
  <head>
    <style>
      #map-root {
        position: relative;
      }
      .nf-metro-canvas svg {
        width: 100%;
        height: auto;
      }
      .nf-metro-legend {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        padding: 8px;
      }
      .nf-metro-tip {
        position: fixed;
        pointer-events: none;
      }
      /* Station selection styles */
      .nf-metro-station-selected rect,
      .nf-metro-station-selected circle {
        stroke: #4cf;
        stroke-width: 2;
      }
      .nf-metro-station-dim {
        opacity: 0.15;
        transition: opacity 0.2s;
      }
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
        const el = document.querySelector("#diagram-manifest");
        return el ? JSON.parse(el.textContent) : null;
      })();

      const lines = (manifest?.groups || []).map((g) => ({
        id: g.id,
        label: g.label,
        color: g.color,
        style: "solid",
      }));

      const api = attachMetroMap({
        root: document.getElementById("map-root"),
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

Both the manifest schema and the driver contract are versioned.
The Python constants are:

```python
from nf_metro.manifest import MANIFEST_SCHEMA_VERSION   # e.g. "1.0"
from nf_metro.render.driver import DRIVER_CONTRACT_VERSION  # e.g. "1.0"
```

The minor part increments for additive, backward-compatible changes, and the major part increments for breaking changes.
Consumers must ignore unknown fields.

This surface is stable as of nf-metro 1.0.
Within a major version the contract only grows in backward-compatible ways.
Pin to a specific nf-metro release only if you depend on the exact bytes of the output.
