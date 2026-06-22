# Changelog

All notable changes to nf-metro are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
nf-metro uses [semantic versioning](https://semver.org/spec/v2.0.0.html) from
1.0.0 onwards. The CLI, the `.mmd` directive surface, and the embed contract
(the `data-*` attributes, driver API, and manifest schema, versioned by
`DRIVER_CONTRACT_VERSION` and `MANIFEST_SCHEMA_VERSION`) are the public API. The
Python modules are not a semver-stable public API.

## [Unreleased] — 1.0.0

418 commits since 0.7.2, touching every layer of the stack. Existing `.mmd`
files render with no changes unless you opt in to a new rendering feature.

### New commands and CLI flags

- **`nf-metro serve` / `nf-metro serve-multi`** — live-progress overlay: a
  metro map lights up in real time as a Nextflow pipeline runs, driven by weblog
  events over SSE. `serve` is a single-map one-command mode (auto-stop on
  pipeline exit); `serve-multi` is a persistent multi-run dashboard.
- **`nf-metro check-mapping`** — lints a `%%metro process:` mapping against the
  real process graph and reports unmapped or misspelled names.
- **`nf-metro explain`** — explains the rule behind each inferred layout
  decision for a `.mmd` file (direction inference, section order, port
  placement).
- **`nf-metro embed-script`** — prints the versioned embed driver JS to stdout
  or writes it to `-o <file>` for use on host pages.
- **`nf-metro render --format html`** — interactive HTML output with pan/zoom,
  animated line highlighting, and the data manifest wired to the overlay. (The
  basic HTML output existed since 0.7.0; this release stabilises the embed
  contract and adds `--bare` / `--responsive` embedding modes.)
- **`nf-metro validate --with-layout` / `--strict`** — layered validation: the
  base command checks authoring, `--with-layout` runs the full layout pipeline
  and reports any guard violations, `--strict` turns violations into hard errors.
- **`nf-metro render --validate`** / **`nf-metro validate-svg --geometry`** —
  post-render geometry check reads the drawn SVG to catch label strikes, marker
  crossings, and offset-pitch collapse.
- **`--directional` / `--no-directional`** — draw open `>` chevrons along each
  route pointing in the flow direction (source to target). Off by default.
- **`--bare`** — omit the title block and outer padding for tight embedding in
  docs pages or apps.
- **`--responsive`** — emit `viewBox` only (no fixed `width`/`height`) for
  fluid SVG embedding.
- **`--embed-font`** — inline Inter as a base64 `@font-face` so the SVG renders
  identically on any host without a font CDN.
- **`--font-paths`** — convert all text to paths for pixel-perfect PDF/PNG
  export.
- **`--no-chrome-css`** — bake concrete colors (disabling CSS custom property
  overrides) for rasterisation pipelines like cairosvg.
- **`--theme seqera`** — Seqera Platform visual theme.

### New `%%metro` directives

- **`%%metro process: <station> | <regex>`** — tie a station to the Nextflow
  process(es) it represents for live-progress mode. The regex matches the
  fully-qualified process name; repeat to attach several patterns to one
  station. Pure metadata — never affects the rendered map.
- **`%%metro directional: true`** — graph-wide opt-in for flow direction
  chevrons (mirrors `--directional`).
- **`%%metro marker: <station> | <shape>, <fill>`** — override a station's
  marker shape (`circle`, `square`, `pill`) and fill (`open`, `solid`, or any
  literal color). Opt-in; unmarked diagrams render byte-identically.
- **`%%metro marker_legend:`** — add a marker shape/fill key below the line
  legend.
- **`%%metro group: <station_list> | <label>`** — visually group a list of
  stations within a section with a band caption (e.g. to call out a sub-process
  cluster).
- **`%%metro caption: <text>`** — figure attribution or caption, rendered below
  the map.
- **`%%metro line_spread: rails`** — parallel-rails mode: each line gets its
  own fixed rail and shared stations render as classic interchange bars
  (line circles joined by a connector bar) rather than a stacked bundle.
- **`%%metro line_spread: centered`** — center-balanced bundle: the trunk is
  centred about the midline rather than cascading from the top.
- **`%%metro label_angle: 45`** — opt-in diagonal station labels for dense
  trunks.
- **`%%metro font_scale: <factor>`** — per-render font size multiplier.
- **`%%metro logo_scale: <factor>`** — logo size multiplier.
- **`%%metro legend_logo_gap: <px>`** — gap between legend and logo.
- **`%%metro manifest: false`** — suppress the embedded data manifest (the
  manifest is on by default).

### Embedded data manifest

Every rendered SVG now carries a machine-readable manifest so the committed
file is a self-contained, durable artifact. A downstream tool can position
overlays, restyle nodes, or resolve which processes a station represents without
re-running the layout engine. Two redundant, sanitization-safe mechanisms (no
`<script>`):

1. A JSON block in `<metadata id="diagram-manifest">`: schema version, title,
   canvas dimensions, groups, regions, and nodes (each with `id`, `label`,
   absolute `x/y/r`, group membership, region, and process regex patterns).
2. `data-node-*` attributes on each station's `<g>` element, making each
   station an addressable DOM node.

`nf_metro.manifest` is a dependency-free package (no nf-metro imports) that
can be extracted into its own distribution. It exposes `read_manifest()`,
`match_station_ids()`, and the JSON Schema. See [Data manifest](docs/manifest.md).

### Embed contract

`driver.js` ships as a versioned resource (`DRIVER_CONTRACT_VERSION = "1.0"`).
The public JS API on any rendered HTML map:

- `attachMetroMap(el)` — wire interactivity to a mounted SVG.
- `highlightLine(lineId)` / `clearHighlight()` — toggle line emphasis.
- `getManifest()` — return the parsed data manifest.
- `selectNode(nodeId)` — programmatically focus a station.

`data-*` attribute tables and a copy-paste integration snippet are documented
at [docs/embed.md](docs/embed.md).

### Rendering improvements

- SVG classes namespaced with `nfm-` prefix to avoid collisions when the SVG
  is inlined on a host page.
- Chrome colors (backgrounds, badges, section fills) driven by CSS custom
  properties, enabling dark-mode theming through a host stylesheet without
  re-rendering.
- Label halos and increased section contrast for legibility on complex maps.
- Wider bundle separation on dense maps.
- Non-merging line crossings bridged with a visible gap so tracks that merely
  cross are visually distinct from merge junctions.
- Bridge rendering for same-colour independent arms.
- Terminus file icons orient to TB section flow direction.
- Section headers relocated clear of top-entry drop routes (above, below, or
  rotated to the side).
- Positionable legend + logo block with `%%metro legend_logo_gap:`.

### Layout improvements

- Stacked-section serpentine routing and inter-column corridor fan-in for
  complex multi-row layouts.
- Cross-track interchange stations (visual interchange bar for `rails` mode).
- Independent disconnected section components placed on their own grid cells.
- Canvas Y-grid re-snap, junction reposition, and icon-pad pass for cleaner
  spacing.
- Principled inter-section gap-width formula (A/B clearances).
- Bidirectional section-top primitive for symmetric top/bottom padding.
- Per-phase coordinate snapshots for regression localisation.
- Guard registry with tier table (layout invariant guards, always-on Tier-A
  and opt-in Tier-B).
- AxisFrame primitive: axis-generic row/inter-section vocabulary, reducing
  direction-specific `if direction == TB` branches.
- Routing gate coverage matrix and ratchet (CI-enforced).

### Notable fixes since 0.7.2

- Bundle order preserved through TB exit reversal corners.
- Flow-axis ports anchored to their consumer/producer end (fold-back eliminated).
- Single-carrier flow-aligned exit ports anchored to their carrying row.
- Cross-column perpendicular drops kept in-bbox with bridge rendering.
- Section header relocated clear of top-entry drop routes.
- Diagonal bundles given a true perpendicular gap.
- Distinct lines bundled out of shared fan-out junctions.
- Convergence into shared-port ordering made first-class.
- Bundle order preserved on up-direction left-entry wraps.
- RIGHT entry dropped straight down its outward side from above.
- Bottommost-row climb kept at row level over a clear corridor.
- RL return-row convergence settled into shared entry ports.
- Dead cross-column TB TOP-entry shift removed.
- Wide-label sections widened to clear bypassed-label rake.
- Reversed-fold reconvergence levelled and vertical fans ordered.
- Multi-carrier off-row exit ports anchored to the carrier row.
- Post-convergence trunk continued on the merge row.
- Flow-aligned exit offset kept on the onward bypass run.

---

## [0.7.2] — 2026-05-18

Patch release.

### Fixed

- `_fan_source_inputs_upward` (Stage 6.2) lifts source-input chains above the
  trunk, but the bbox-bottom shrink (Stage 6.13) was blocked by a self-protecting
  row-mate predicate. LR/RL sections now match on starting grid row only (with
  rowspan respected); TB sections keep the Y-overlap check. Most visible on the
  nf-core/differentialabundance map where `data_prep` ended mid-air.
  ([#382](https://github.com/pinin4fjords/nf-metro/issues/382))

---

## [0.7.1] — 2026-05-17

Patch release.

### Fixed

- Cross-column bypass routes that descend below intervening sections could land
  close enough to the next row's section header that the stacked-line bundle
  visually crowded the badge. Section placement is now bypass-aware: the row gap
  is sized against the deepest predicted bypass route.
  ([#380](https://github.com/pinin4fjords/nf-metro/issues/380))

---

## [0.7.0] — 2026-05-17

228 commits since 0.6.1. Existing `.mmd` files render with no changes.

### Added

- **Interactive HTML output** — `nf-metro render --format html` produces a
  self-contained interactive HTML file with pan/zoom and animated line
  highlighting.
- **`%%metro off_track: <node>`** — lifts file inputs above the line tracks so
  they sit clear of the metro lines instead of breaking them.
- **`%%metro center_ports: true`** / `--center-ports` — centres inter-section
  ports on the shorter section, tidying many fan-in/fan-out cases.
- **`%%metro legend_min_height: <px>`** — reserves a minimum legend height for
  single-line maps.
- **`files` and `dir` file icon types** — `%%metro files:` (stacked-documents)
  and `%%metro dir:` (folder) icons join the existing `file` icon. All three
  accept an optional caption.
- **Dashed and dotted lines** — add a fourth field to `%%metro line:` to
  indicate optional or conditional routes: `| dashed` or `| dotted`.
- **nf-core/differentialabundance** and **genomeassembly** gallery examples.
- Sections numbered in visual reading order.
- Per-line path grouping for consistent line z-order at crossings.
- Layout invariant framework with phase-boundary guards
  (`compute_layout(validate=True)`), full-corpus parametrised tests, and the
  C13 row-gap runtime guard.
- Layout pipeline reorganised into six named stages with flat `Stage X.Y`
  naming.
- Spatial-index validation guards with closed-form intersection.
- Cached `station_lines()` (~40 call sites, O(1) amortised).
- New topology fixtures: `upward_bypass`, `mismatched_tracks`, `fan_in_merge`.

### Fixed

- Fan-in merge junctions route cleanly onto the trunk.
- Animated balls no longer fly off-piste at merge junctions.
- Per-line path grouping gives consistent line z-order at crossings.
- Dozens of fan-out, fan-in, bypass, and off-track routing fixes.

---

## [0.6.1] — 2026-03-06

Patch release.

### Fixed

- Docs build: render variantbenchmarking and debug SVGs during the documentation
  build.

---

## [0.6.0] — 2026-03-06

### Added/Fixed

- Layout and routing improvements driven by the variantbenchmarking pipeline
  example.

---

## [0.5.4] — 2026-02-27

### Fixed

- Synchronize animation timing and reduce diamond path explosion.

---

## [0.5.3] — 2026-02-27

### Changed

- Increased section header prominence and improved section label hierarchy.

---

## [0.5.2] — 2026-02-25

### Fixed

- Increased label spacing and reduced file icon font size.

---

## [0.5.1] — 2026-02-25

### Added

- nf-core/variantbenchmarking pipeline example.

### Fixed

- Layout fixes surfaced by the variantbenchmarking example.

---

## [0.5.0] — 2026-02-24

### Added

- Reduced default animation ball count to 1 per track.
- Multi-line station labels via `\n`.

### Fixed

- Dark-mode CSS for transparent-background SVGs.

---

## [0.4.7] — 2026-02-20

### Fixed

- Reduced animation ball count; removed section box transparency.

---

## [0.4.6] — 2026-02-20

### Fixed

- SVG output now ends with a trailing newline.
- More vertical space in TB sections above the first station.
- Section bbox expanded for terminus file icons.

---

## [0.4.5] — 2026-02-20

### Added

- Stroke support for animation balls.

### Fixed

- Clear error message for unannotated edges.
- Equidistant spacing for cross-line fork stations.
- Symmetric diagonal slopes at convergence/divergence stations.

---

## [0.4.4] — 2026-02-19

### Changed

- Improved light theme visibility.

---

## [0.4.3] — 2026-02-19

### Changed

- Bumped font sizes and added entry divergence padding.
- Increased fork/join gap multiplier.

### Fixed

- TB section bbox widened for long labels.

---

## [0.4.2] — 2026-02-19

### Fixed

- Resolved label overlaps in the rnaseq example.

---

## [0.4.1] — 2026-02-19

### Fixed

- Only enforce minimum column gap between row-overlapping sections.
- Label bbox clamping no longer overlaps the station pill.
- Removed double padding from canvas sizing.

---

## [0.4.0] — 2026-02-19

### Added

- **Nextflow DAG import** — `nf-metro render --from-nextflow` converts
  Nextflow `-with-dag` Mermaid output before rendering, so you can pipe a
  Nextflow-generated DAG straight into nf-metro.
- Bioconda and Seqera Containers installation options.

### Fixed

- Detect and report unsupported Nextflow DAG input with a clear error.

---

## [0.3.0] — 2026-02-18

### Fixed

- Strip explicit port hints from topology examples.
- Guide examples and layout engine fixes.

---

## [0.2.2] — 2026-02-17

### Fixed

- GitHub Pages `pages:write` permission and deployment trigger.

---

## [0.2.1] — 2026-02-17

### Fixed

- GitHub Pages deployment trigger after `mike deploy`.

---

## [0.2.0] — 2026-02-17

### Fixed

- Section overlap; rnaseq layout and rendering improvements.
- Layout engine bug fixes; topology stress-test suite added.

---

## [0.1.1] — 2026-02-16

### Fixed

- README hero image URL corrected for PyPI display.

---

## [0.1] — 2026-02-16

Initial release.

### Added

- Auto-infer section layout from graph topology.
- Animated balls traveling along metro lines.
- Transparent background for the light theme.
- CLI commands: `render`, `validate`, `info`.
- nfcore (dark) and light visual themes.
- Mermaid `graph LR` / `graph TD` input with `%%metro` directive extensions.
