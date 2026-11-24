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

### Breaking changes

- `jsonschema` is a core dependency, and the `validate` extra is gone:
  `pip install nf-metro` brings everything `nf-metro validate-svg` and theme
  files need, and `pip install "nf-metro[validate]"` no longer names an extra.
- Theme file errors take the form `theme file <path>: <JSON path>: <message>`
  (for example `$.light.label_color: '#12345' is not a colour`), as reported by
  the theme JSON Schema.
- The Python API raises `ThemeError` (an `NfMetroError` and `ValueError`) for
  an unknown theme name instead of falling back to `nfcore`.
- `nf-metro info` reports `style: transparent` for a map written with
  `style: light`; `light` stays accepted as an alias.
- Python API: `route_edges`, `route_edges_centred`, `observe_route_edges` and
  `observe_route_edges_centred` take no `diagonal_run` argument; set the
  `diagonal_run` option instead. `render.constants.line_style_kwargs` takes the
  theme its dash patterns come from.

### Added

- Theme stacks and theme files (#2195): `%%metro style:`, `--theme` and a
  per-output `theme=` take a comma-separated list of built-in names and YAML
  theme files, merged left to right over the `nfcore` base. A theme file can
  set any field of the theme schema per light/dark palette, extend other
  themes, fix one palette (`fixed_mode`), and set look-related layout options
  in a `layout:` block, which sits below `%%metro` directives and CLI flags.
  Every built-in theme is itself such a file. See the Theming page.
- Themes can set station and section shapes: tab-and-ring station glyphs in
  multiples of the line width, section outline width, dash and corners,
  number badges, title case and tracking, and a legend frame.
- `terminus_glyph` theme setting for a line end shared by several lines:
  `nub` (the default square box), `bar` (a track-weight buffer-stop bar
  across the bundle) or `ring`.
- `glyph_stroke` and `capsule_fill` theme settings give interchange rings,
  bar line ends, station capsules and interchanges colours that follow the
  viewer's light or dark mode, muted lines included.
- Theme files are validated against a JSON Schema generated from the theme
  definition and shipped as `nf_metro/themes/theme.schema.json`. Editors using
  the YAML language server complete and check theme files once pointed at it;
  the built-in themes do.
- Theme fields for values that were fixed: section badge fill, digit colour
  and size; dashed and dotted dash patterns; `line_cap`; `bridge_gap`; caption
  colour (light/dark aware) and size; watermark colour; file-icon banner fill
  and text colour; legend padding, row height and swatch width; group caption
  rule width and opacity; `canvas_padding`; `title_font_weight`;
  `animation_ball_opacity`. Each defaults to the value it replaces.
- `section_x_padding`, `section_y_padding`, `label_offset` and `diagonal_run`
  options, each a `%%metro` directive, a CLI flag and a theme `layout:` option;
  `directional` can be set by a theme too.
- `london-tube` built-in theme: a schematic transit look after the geometry of
  TfL's Line Diagram Standard, with track-weight station tabs, terminus bars,
  bold capsules at interchanges, bundled lines edge to edge and a 30% grey for
  inactive lines. `interchange_glyph: ring` over it gives the standard's
  interchange rings and bridged chains instead.
- `%%metro number: 0` hides a section's number badge and starts its title where
  the badge would have been. Automatic numbering skips such a section, so the
  remaining sections number from 1 as if it were absent (#2058).
- Hidden sections: a subgraph whose ID starts with `_` keeps its grid cell
  but draws no box, title or number badge, mirroring hidden stations. Use it
  to place stations independently without a visible section. Existing maps
  with a `_`-prefixed section ID lose that section's box.
- `%%metro output:` paths take per-output render overrides after a `|`:
  `animate`, `mode=`, `theme=`, `scale=`, and `raster_width=`. One
  `nf-metro render` pass now writes a static SVG, an animated SVG, and a
  light/dark PNG pair with no CLI flags, which is what the nf-core pipeline
  template needs from the GitHub Action and pre-commit hook. `mode` is
  baked at serialize time, so those four files cost two layout runs, not
  four.

### Changed

- Parallel lines turning together through any angle, not only 90 degrees,
  now turn about one shared centre, so a bundle on a diagonal turn keeps its
  lines touching instead of opening a thin gap between them. Maps with
  diagonal bundle turns redraw those curves slightly.
- Rounded corners draw as circular arcs (cubic Bézier circle
  approximations) where they were quadratic curves, with the same tangent
  points and radii. Parallel lines turning together stay exactly one stroke
  apart through the whole turn at every angle, closing the thin seam that a
  right-angle bundle turn showed between touching lines. Every rounded corner
  on every map redraws very slightly fuller.
- Animated dots ride the drawn strokes: each dot turns about a corner with its
  lane's own radius, so dots keep to their lines through concentric turns and
  source turnouts. Animation timing is measured on the unrounded geometry.
- The transparent embed theme is named `transparent`; `light` is an accepted
  alias.
- Layout reserves room for a theme's own file-icon size, line width and
  chevron size, and the canvas margin grows for a theme drawing wider lines or
  larger chevrons than any built-in.
- The title lines up with a thick section outline's outer edge;
  `london-tube`'s 1.5px outline moves its title 0.25px left.
- Layout measures file icons and their captions with the theme the map
  renders with, including `--theme` and a per-output `theme=`, rather than
  the map's own style or `nfcore`. A map rendered with a theme whose icons or
  captions are larger gets room for them.
- The interactive HTML page's chrome (header, side panel, buttons, tooltips,
  embed dialog) and the copyable inline-HTML snippet now follow the map's mode
  instead of always being dark. `--mode` or `%%metro mode:` fixes both light
  or dark. With no mode set they follow the viewer's light/dark preference, as
  the SVG already does, so a page with no mode now renders light for viewers
  who prefer light. `--no-self-color-scheme` makes the snippet follow its host
  page's `color-scheme` instead. The transparent `light` theme's labels track
  the page's mode rather than the OS setting, so they stay readable on either
  canvas.
- `--reject-output-outside-source` now scopes to the git working tree holding
  the `.mmd` rather than to the `.mmd`'s own directory, so
  `assets/metro_map.mmd` may declare `../docs/images/map.svg`. Paths escaping
  the repository, and any path whose resolved destination lies inside a
  `.git` directory (git config and hooks are a code-execution vector for a
  later `git` call in the same job), are still refused; outside a git
  working tree the old source-directory rule still applies. Declared paths
  are also reported collapsed
  (`docs/images/map.svg`, not `assets/../docs/images/map.svg`).
- The interactive HTML viewer moves the line legend from the right-hand
  column to a bar under the map on portrait viewports or ones 640px wide or
  narrower, such as phones and side-by-side editor panels. The page declares
  a device-width viewport and supports touch pan and pinch-zoom.
- A render whose curve check fails now aborts unless the failure lies on the
  lead-in to a perpendicular entry port whose feeder descends outside the
  section's box. Before, one such port anywhere in a map turned every curve failure
  in that map into a warning, so some maps that used to draw with a warning
  now abort and name the defect
  ([#2123](https://github.com/seqeralabs/nf-metro/issues/2123)).

### Fixed

- A source that feeds a station adding a second line stays on that station's
  row when other sources share its column. The other sources sit one row to
  either side and the added line's branch one row off, whichever source is
  declared first, instead of the trunk's first station dropping a row and the
  branch landing two rows down
  ([#2193](https://github.com/seqeralabs/nf-metro/issues/2193)).
- Two source stations in one column no longer put an unrelated line through a
  branch station. When one source sends a line off onto its own branch, the
  other source sits clear of the lane that branch takes, whichever line is
  declared first, instead of running its line flat through the branch's marker
  ([#2192](https://github.com/seqeralabs/nf-metro/issues/2192)).
- Animated dots share each fork fairly: branches that split and rejoin take
  turns, so a branch that forks internally no longer shows more dots than a
  one-hop alternative. Every start node of a line now animates, and the
  animation loops within 60 seconds (or one cycle, if longer) (#2009).
- Nextflow conversion preserves spur dependencies between subworkflows instead
  of silently omitting those edges from the emitted map.
- Nextflow conversion splits a section that runs both before and after another
  one (top-level processes around a subworkflow become `Main` and `Reporting`;
  a parent subworkflow's own processes are split around its child), instead of
  emitting a map that fails with "Sections form a cycle".
- A map with a `%%metro logo:` draws its title again. With the logo in the
  legend the title sits top-left as usual; with a standalone logo
  (`%%metro legend: none`, and every HTML output) the title sits beside the
  logo. The title, or a standalone logo, starts at the leftmost ink the map
  paints: a section box, a line, a label, the caption or the legend. The whole map,
  rail-mode maps included, now moves down clear of a standalone logo instead of
  running under it, laid out and routed exactly as it would be without one.
  `logo_scale` sizes the standalone logo as well as the legend one.
- A station sitting exactly midway between two grid rows snaps to the same
  row wherever its row sits on the canvas, and stations tied together by a
  line or a fan snap the same way, choosing the row that keeps their
  connections level. A fan's branches keep their spacing, and lines
  continuing between two neighbouring sections no longer kink.
- Section number badges skip sections that draw no badge. A map mixing loose
  stations with subgraphs numbered its visible sections from 2.
- A perpendicular (top or bottom) entry port fed through merges, or by
  feeders arriving along two different corridors, now gives each line the
  channel that matches its lane in the section, so the lines no longer cross
  or overlap inside the port's own turn.
- Nextflow subworkflows with repeated short names get distinct section and
  bypass line IDs, preserving each subworkflow's stations, section box and
  bypass line.
- `nf-metro info` now reports the section numbers the render draws, including
  route renumbering and `%%metro number:` overrides, instead of the numbers
  assigned at parse time. `--json` `sections[].number` is `null` for a section
  with no badge: number 0, a hidden section, or the implicit section of a map
  with no `subgraph` (#2058).
- A line that skips a station through a hidden helper now opens its bypass on
  the side its own lane sits, so the bypass no longer crosses the lines staying
  on the trunk. This applies under both `diamond_style: straight` and
  `diamond_style: symmetric`, in sections where at most one line enters.
  Under `diamond_style: symmetric`, a fan whose branches nest (one branch
  carries every line the others do) also seats each branch on its lines' lane
  side, in sections where at most one line enters (#2170).
- `--directional` chevrons sit on their line's drawn stroke and point along it
  through rounded corners; a chevron that would land in a bridge gap is
  omitted. `--validate` and `validate-svg --geometry` report a chevron that
  leaves its stroke (#2177).
- `--directional` draws one evenly spaced set of chevrons per line where several
  of its routes overlap, and continues that set through an invisible port or
  junction where the strokes meet at one point and heading. A stretch of at
  least half a spacing that the set skips gets a chevron at its middle,
  provided that stays half a spacing from the set's neighbours and clear of
  other chevrons of the line.
- When two stations feed the same exit and one of them also fans into
  dead-end branches, a branch no longer sits on the other feeder's row.
  That feeder's exit run was drawn straight through the branch's marker
  (and any file icon after it); the branch now peels below the exit rows
  (#2175).

## [2.1.0] — 2026-09-15

### Added

- `nf-metro render` writes PNG directly. A `.png` output path selects it, or pass
  `--format png`; `--scale` sets the pixel multiplier (default 2) and
  `--raster-width` pins an exact width. The PNG path bakes the palette, drops the `var()` chrome,
  and draws with the bundled Inter, so a map rasterises to the same bytes on any
  machine ([#1969](https://github.com/seqeralabs/nf-metro/issues/1969)).
- `-o` repeats, so one run writes several formats: `-o map.svg -o map.png`.
- `nf-metro render` writes a looping video of the animation: `-o map.gif`,
  `.webp`, `.mp4`, or `.webm` (or `--format`). The loop runs one animation cycle
  and stops a frame short of repeating it, so it wraps seamlessly; `--fps` and
  `--duration` set the frame rate and compress a long map's cycle into a shorter
  loop. Frames are sampled from the same motion paths the animated SVG uses, so
  a ball is where a browser would draw it. `--scale` and `--raster-width` size
  the frames as they size a PNG. Nothing is capped: a large export quotes its
  frame count and frame size before it starts, then shows a progress bar.
- New [Output formats](https://seqeralabs.github.io/nf-metro/latest/formats/)
  docs page, comparing SVG, HTML, PNG and the four looping video formats on what
  each is good at and where each one plays.

### Changed

- Rasterisation moved from cairosvg to `resvg-py`, now a runtime dependency.
  cairosvg needed system libcairo, which is why PNG was never a first-class
  output; resvg-py ships self-contained wheels and needs no system libraries.
- `av` (PyAV) joins the runtime dependencies, for the looping video formats.
  It ships FFmpeg in its own wheels, so an MP4 or WebM export needs nothing
  installed or found on `PATH`.

### Fixed

- A long tail of routing and layout fixes: fan-branch loop-side stations now
  recenter correctly for multi-line edges, convergence landings record their
  real cross-run start, exit-turn and perp-entry feeder ordering, bottom-entry
  fan mirroring, single-word section headers no longer abort the render, bundle
  corner-radius derivation is unified across concentric fans, and flat
  (sectionless) graphs get an implicit section so skip-line bypass detours
  resolve.

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
