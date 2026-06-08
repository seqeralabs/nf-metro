# Feature stability tiers

> **Status: PROPOSED.** The tier assigned to each item below is a proposal
> pending maintainer sign-off. Until this notice is removed, treat the tiers as
> a recommendation, not a commitment.

nf-metro's control surface (the `%%metro` directive grammar and the CLI flags)
is the contract authors write against. As the project approaches its v1
release, this page declares which parts of that surface are load-bearing core
idiom, which are well-supported advanced controls, and which are experimental
or showcase features that may still change.

## Semver scope at v1

At v1 the public surface freezes under [semantic versioning](https://semver.org/):

- **Covered by the compatibility promise (core and advanced):** the directive
  names and their grammar, and the CLI flag names and their semantics. A `.mmd`
  written against v1 keeps rendering, and a CLI invocation keeps working, across
  the whole v1.x series. Breaking either requires a major version bump.
- **Excluded from the compatibility promise (experimental):** experimental
  items may change name, grammar, default, or be removed in any minor release.
  They are documented and usable, but authors should not treat them as stable.

What is explicitly **not** part of the contract, at any tier:

- Exact rendered pixel output. Renders may shift between minor versions as the
  layout engine improves; the gallery render-diff in CI guards against
  *unintended* change, not against all change.
- Internal Python APIs (anything under `nf_metro.*` imported in code rather
  than driven through the CLI or a `.mmd` file).
- Warning message text and the precise wording of validation errors.

## Tiers

- **Core** - the metro idiom proper: lines, sections, ports, file icons,
  legends, Nextflow conversion. These are what nf-metro *is*. Expected to be
  used heavily; will not change incompatibly within v1.
- **Advanced** - broadly composable, well-supported controls for authors who
  need to override the defaults. Stable within v1, but more specialised.
- **Experimental** - features with known edge-case limitations or a narrow
  showcase target. Documented and usable, but outside the v1 compatibility
  promise; may change or be removed in a minor release.

## Directive tiers

| Directive | Tier | What it does |
|---|---|---|
| `title:` | core | Pipeline title. |
| `style:` | core | Theme selector (`dark`/`light`). |
| `line:` | core | Define a metro line (`id \| name \| #color [\| style]`). |
| `entry:` / `exit:` | core | Section port-side hints (`side \| line1, line2`). |
| `file:` / `files:` / `dir:` | core | File / paired-file / directory terminus icons. |
| `legend:` | core | Position the legend+logo block. |
| `legend_combo:` | advanced | Render several lines as one combined legend row. |
| `marker:` | advanced | Per-station marker shape/fill. |
| `marker_legend:` | advanced | Caption rows explaining marker shapes. |
| `group:` | advanced | Decorative caption spanning a set of stations. |
| `logo:` | advanced | Logo image path. |
| `logo_scale:` | advanced | Scale the logo within the legend block. |
| `legend_min_height:` | advanced | Minimum legend content height. |
| `legend_logo_gap:` | advanced | Gap between logo and legend entries. |
| `font_scale:` | advanced | Scale all text and label-width metrics. |
| `line_order:` | advanced | Track-assignment ordering (`definition`/`span`). |
| `grid:` | advanced | Pin a section to a grid cell (`section \| col,row[,rowspan[,colspan]]`). |
| `direction:` | advanced | Section internal flow direction (`LR`/`RL`/`TB`). |
| `off_track:` | advanced | Lift a station off the main trunk. |
| `compact_offsets:` | advanced | Size stations only for the lines passing through them. |
| `center_ports:` | advanced | Centre inter-section ports on the shorter section. |
| `diamond_style:` | advanced | Fork-join layout (`straight`/`symmetric`). |
| `fold_threshold:` | advanced | Station-columns before a section row wraps. |
| `x_spacing:` / `y_spacing:` | advanced | Layer / track spacing overrides. |
| `section_x_gap:` / `section_y_gap:` | advanced | Inter-section gaps. |
| `width:` / `height:` | advanced | Output dimensions. |
| `animate:` | advanced | Animated balls travelling along the lines. |
| `label_angle:` | experimental | Rotate station labels (diagonal labels). |
| `line_spread: rails` | experimental | Parallel-rail line mode (the `bundle` and `centered` modes are advanced). |

## CLI flag tiers

Every registry-backed flag mirrors a directive of the same name (kebab-cased)
and inherits that directive's tier; an explicitly-set flag overrides the
directive (see Precedence). The flags below have **no** directive twin and are
tiered on their own.

| Command / flag | Tier | What it does |
|---|---|---|
| `render` | core | Render a `.mmd` to SVG or HTML. |
| `render -o/--output` | core | Output file path. |
| `render --format` | core | `svg` (default) or `html`. |
| `render --theme` | core | Theme override (`nfcore`/`light`). |
| `render --logo` | advanced | Logo path override. |
| `render --title` | core | Title override. |
| `render --legend` | core | Legend-position override. |
| `render --from-nextflow` | core | Convert Nextflow `-with-dag` input inline before rendering. |
| `render --debug` | advanced | Debug overlay (ports, hidden stations, waypoints). |
| `render --line-spread` | advanced* | Line-spread override (`bundle`/`centered` advanced, `rails` experimental). |
| `convert` | core | Convert a Nextflow DAG `.mmd` to nf-metro format. |
| `validate` | core | Validate a `.mmd`. |
| `info` | core | Summarise a `.mmd` (title, lines, sections). |

\* `--line-spread rails` selects the experimental rails mode; the flag itself is
otherwise advanced.

## Experimental items and why

- **`label_angle:` (diagonal station labels)** - currently only honoured on
  left-to-right trunks ([#556](https://github.com/pinin4fjords/nf-metro/issues/556));
  the angle, axis handling, and collision behaviour are still settling.
- **`line_spread: rails` (parallel-rail mode)** - the most layout-invasive of
  the three line-spread modes; interchange-station handling has the most
  edge-case surface. The `bundle` (default) and `centered` modes are advanced.
- **`graph TB` / `graph TD` primary direction** - top-down graphs are not
  supported; the parser warns and falls back to left-to-right
  ([#533](https://github.com/pinin4fjords/nf-metro/issues/533)). This is a
  parser-header behaviour rather than a `%%metro` directive, but it is listed
  here because authors may reasonably expect it to work and should treat it as
  unsupported until #533 lands.

## Precedence

For any control that exists in both planes, precedence is uniform:

> **CLI flag (when explicitly set) > `%%metro` directive > built-in default.**

Every registry-backed flag (`nf_metro.options.LAYOUT_OPTIONS`) defaults to
"unset" on the CLI, so omitting the flag leaves the directive value in place.

## For contributors

A new directive or CLI flag must declare its tier in the PR that adds it. If it
has known edge-case limitations or a narrow showcase target, tier it
**experimental** so it stays outside the v1 compatibility promise until it has
proven itself.
