# Changelog

All notable changes to nf-metro are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
nf-metro uses [semantic versioning](https://semver.org/spec/v2.0.0.html) from
1.0.0 onwards. The CLI, the `.mmd` directive surface, and the embed contract
(the `data-*` attributes, driver API, and manifest schema, versioned by
`DRIVER_CONTRACT_VERSION` and `MANIFEST_SCHEMA_VERSION`) are the public API. The
Python modules are not a semver-stable public API.

The full notes for each release live in `docs/releases/` and on the docs site at
<https://seqeralabs.github.io/nf-metro/>; this file carries the condensed
history.

## [Unreleased]

### Added

- `nf-metro render` writes PNG directly. A `.png` output path selects it, or pass
  `--format png`; `--scale` sets the pixel multiplier (default 2) and `--png-width`
  pins an exact width. The PNG path bakes the palette, drops the `var()` chrome,
  and draws with the bundled Inter, so a map rasterises to the same bytes on any
  machine ([#1969](https://github.com/seqeralabs/nf-metro/issues/1969)).
- `-o` repeats, so one run writes several formats: `-o map.svg -o map.png`.

### Changed

- Rasterisation moved from cairosvg to `resvg-py`, now a runtime dependency.
  cairosvg needed system libcairo, which is why PNG was never a first-class
  output; resvg-py ships self-contained wheels and needs no system libraries.

---

## [2.0.0] — 2026-09-05

A major release, roughly 300 pull requests after 1.1.0. Two changes to how
invalid input is handled drive the version number; a map that renders cleanly
under 1.1.0 keeps rendering, with section boxes now hugging their content. The
full account is in `docs/releases/2.0.0.md`.

### Added

- **Inactive lines**: a fifth `inactive` field on `%%metro line:` and a
  per-render `--inactive-lines <ids>` override grey out lines a run did not
  exercise, along with any station, label, legend swatch or icon touched only
  by inactive lines. `render_string` takes the same set as `inactive_line_ids`.
- **`%%metro stroke_scale:` / `--stroke-scale`** thickens track strokes and
  station pills, with bundle spacing, marker clearance and rail pitch scaling to
  match, for large maps that are downscaled to fit a screen.
- **`%%metro row_align: content|top` / `--row-align`** controls whether a
  section box hugs its own content (the new default) or grows to share the
  tallest row-mate's top edge.
- **`%%metro number:`** pins a section's number badge.
- **`--permissive` / `%%metro permissive:`** downgrades layout and render guard
  failures to a labelled warning block and renders best-effort instead of
  aborting with no output.
- **`nf-metro render` takes several input files**, rendering each to its
  sibling `.svg` in one process; `render-many` accepts the full render option
  set.
- **Composite GitHub Action** (`uses: seqeralabs/nf-metro@2.0.0`) and
  **pre-commit hook** (`id: nf-metro`) that render a pipeline repository's map
  and pin the nf-metro version they ship with.
- `NfMetroError` base class for every input-authoring error `render_string`
  raises; `py.typed` marker; `render_string`, `prepare_graph`, `render_graph`
  and `RenderConfig` re-exported from the `nf_metro` root.
- Versioned JSON Schema and normative description for the live progress
  server's state snapshot.
- `validate` extra installing `jsonschema` for `nf-metro validate-svg`.
- nf-core/riboseq map in the gallery; Theming and CI & automation docs pages.

### Changed

- **Breaking: an edge annotated with a line no `%%metro line:` declares is now
  rejected by `nf-metro render`, not just `nf-metro validate`.** A map that
  declared no lines at all was exempt from the render-side check, so it rendered
  every route in the placeholder grey the themes reserve for inactive lines,
  with an empty legend and exit 0, while `validate` reported one error per edge
  and exit 1. Both commands now read one detector and accept the same maps. A
  map that relied on the exemption needs one `%%metro line:` directive per id
  its edges name, and the error names the missing ids with the source line of
  each.
- **Breaking: a duplicate `%%metro line:` id now keeps the first declaration
  rather than the last, and warns.** A map declaring the same id twice silently
  took the later spelling, so redeclaring a line late in the file was a working
  way to change its colour, style or `inactive` state. The redeclaration is now
  reported and dropped. A map that relied on the old precedence needs its
  intended values on the first declaration of each id.
- Unknown values and unresolvable references in `%%metro` directives are
  reported instead of ignored. `style:` validates against the theme names;
  `off_track:`, `group:`, `marker:`, `grid:`, `line_spread:`, `file:`, `files:`,
  `dir:`, `entry:`, `exit:` and `interchange:` warn on a station, section or
  line id the map never defines; and a `line:` declaration missing its id, name
  or colour is rejected whole rather than registering a partial line. Line ids
  are constrained to the same identifier character set as station and section
  ids.
- Section boxes default to hugging their content (`row_align: content`); the
  former forced top alignment is available as `row_align: top`.
- Automatic section numbering follows connected visual routes rather than file
  order.
- Inter-section routing is planned once and emitted from the recorded plan: the
  legacy first-match dispatcher and its compatibility repair passes are retired,
  every inter-section turn is drawn at its full radius, and corridors are
  reserved at plan time. A route no owner can plan stops with a diagnostic
  instead of falling back.
- `--theme` takes the same seven names as `%%metro style:` (`nfcore`, `seqera`,
  their `-light`/`-dark` variants, `light`, and the `dark` alias); `serve` and
  `serve-multi` take the same choice list.
- A map with no stations is refused with a typed `EmptyGraphError`; numeric
  flags enforce their declared bounds and refuse non-finite values; `render`
  prints warnings as one labelled block on stderr.
- `convert` and `render --from-nextflow` report the feedback edges they remove
  to make the graph acyclic, and list them in a `%%` comment block in the
  converted `.mmd`.
- Theme constants are named by brand and mode: `NFCORE_THEME` and
  `SEQERA_THEME` are removed in favour of `NFCORE_DARK_THEME` and
  `SEQERA_DARK_THEME`.
- Packaging: development status `Production/Stable`, project URLs point at the
  seqeralabs organisation, the wheel omits the layout contract document and the
  candidate-execution harness, and the sdist omits tests, examples, docs and CI
  material.

### Fixed

- Symmetric fans centre their entry port, reconvergence join and fork hub on the
  join hub's centreline; a diamond's fan-in seats off the join hub; fan
  placement follows `diamond_style`.
- Every merge feeder lands on the trunk it converges onto; a clear adjacent
  feeder reaches the merge directly; confluence band and descent nesting order
  agree.
- Bypasses route around a packed cell-mate on the target entry row, keep steep
  multi-line bundles on distinct slots, and minimise lane crossings.
- Off-track outputs sit on their own row for a dead-end producer, beside the
  trunk in `TB`/`BT` sections, and clear of the next divergence.
- `BT` sections present flow-aligned ports, and an `LR` section fed from
  directly below takes a `BOTTOM` entry instead of backtracking through its own
  stations.
- Station labels wrap on whitespace, never mid-word; the canvas grows for ink
  drawn outside the section-box envelope; terminus icons scale with
  `font_scale`; multi-line `%%metro file:` labels render.
- Section-level cycles are rejected with a named diagnostic.
- Text metrics are deterministic, so renders are byte-identical across runs,
  platforms and hash seeds.
- Inactive labels, captions, marker outlines and icon labels stay muted under
  the chrome CSS.
- Shipped examples render from any working directory: logo paths resolve
  relative to the map file.
- Playground bug-report links no longer exceed GitHub's URL limit.

### Security

- Directive-authored text is escaped at every SVG and HTML injection point:
  `%%metro line:` colours and `marker:` fills can no longer break out of their
  attribute, `%%metro logo:` `data:` URIs are escaped and malformed base64 is
  rejected cleanly, the interactive HTML driver escapes line colours and labels
  before DOM insertion, and embedded JSON cannot break out of its `<script>`
  block.

---

## [1.1.0] — 2026-07-01

A routing-focused follow-up to 1.0.0, plus a new spacing control and several
playground fixes. Existing `.mmd` files render with no changes.

### Added

- **`%%metro track_gap: <pixels>`** / **`--track-gap`** — sets the visual gap
  between adjacent line strokes in a bundle: the empty space between their
  edges, not their centres. Defaults to 1 px; `0` brings strokes flush, and up
  to 3 px gives co-running lines more breathing room.
- **Packed grid cells** — hand-placed sections can share one cell
  (`%%metro grid: gatk, variant_calling | 1,0`) instead of taking one each. The
  named sections pack side by side along the flow axis and the cell sizes to fit
  them; each keeps its own direction, ports, and internal layout.
- Playground **"+ Logo"** button, reading a chosen image client-side and writing
  it into `%%metro logo:` as a `data:` URI. The playground runs in-browser via
  Pyodide, where a local file path cannot resolve.
- nf-core/seqinspector on the pipelines gallery page, showing a `%%metro grid:`
  stack of two single-row sections beside a rowspan-2 section.

### Changed

- Playground **Line gap** moved from Advanced options to the main toolbar, wired
  to the new `track_gap` directive.
- The playground shows its build's commit SHA beside **Report a bug** and
  pre-fills it into the report.
- Stacked single-row sections sharing a rowspan band distribute across the full
  band rather than leaving the bottom rows empty.

### Fixed

- Fold-back routing: serpentine multi-line folds under `direction: RL`, fold
  reversal through peel-off junctions, and a kink in the TB-exit-to-return-row
  connector.
- Bypass routing: same-row bypasses route around a packed cell-mate or an
  intervening section rather than through it, and clear exit rows run straight.
- Convergence sinks: entry lanes order by feeder approach direction, and
  interchange labels clear their connector bridge and the enlarged end-knob.
- Sectionless graphs: skip-lines with no subgraph detour around non-consumer
  markers instead of breezing through them.
- 2-way fans with an internal source centre on their equal siblings.
- `--mode` baked output selects the correct logo variant and pins
  `color-scheme`, keeping raster exports independent of the viewer's theme.
- `--embed-font` output falls back to a generic font family after `Inter`.
- The playground service worker served returning visitors a stale build after
  every deploy, because the dev wheel's filename never changed between builds.

---

## [1.0.0] — 2026-06-30

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
- **`%%metro auto_process: true`** (and `--auto-process`) — give every station
  with no explicit `process:` directive its own id as a default process
  pattern, anchored to the final segment of the process name, so a map whose
  station ids already name their processes lights up live with no per-station
  mapping. Opt-in; explicit directives override.
- **`%%metro process_scope: <prefix>`** (and `--process-scope`) — factor out the
  fully-qualified-name prefix shared by a pipeline's processes (e.g.
  `NFCORE_RNASEQ:RNASEQ`); each `process:` value is then the tail under that
  scope, matched literally and tolerant of intermediate subworkflow nesting.
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
- Route-system emission: inter-section routes are dispatched once per canonical
  semantic system and emitted from complete exit-turn, fan, convergence and
  reservation decisions, with emitted geometry validated against the plan that
  owns it. Every convergence states its own geometry, so none falls back to
  compatibility emission.

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
