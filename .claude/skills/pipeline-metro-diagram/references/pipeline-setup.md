# Pipeline metro map setup template

Drop-in templates for adding nf-metro to a pipeline repo. Replace
`<name>` with the pipeline name (e.g. `rnaseq`, `differentialabundance`).

## Files to add

```
assets/metro_map.mmd
docs/dev/metro_map.md
docs/images/nf-core-<name>_metro_map.png
docs/images/nf-core-<name>_metro_map.svg
docs/images/nf-core-<name>_metro_map_animated.svg
```

## `docs/dev/metro_map.md` template (rnaseq-style)

```markdown
# Metro map

The pipeline overview metro map is generated from `assets/metro_map.mmd` using
[nf-metro](https://github.com/pinin4fjords/nf-metro). If you add or rename
pipeline steps, update the `.mmd` source and regenerate the images:

\`\`\`bash
pip install 'nf-metro>=0.5.4' cairosvg

# Static SVG + PNG
nf-metro render assets/metro_map.mmd \
  -o docs/images/nf-core-<name>_metro_map.svg \
  --theme light --x-spacing 60 --y-spacing 40 \
  --no-straight-diamonds \
  --logo docs/images/nf-core-<name>_logo_light.png

python -c "import cairosvg; cairosvg.svg2png(
    url='docs/images/nf-core-<name>_metro_map.svg',
    write_to='docs/images/nf-core-<name>_metro_map.png', output_width=2265)"

# Animated SVG (used in README)
nf-metro render assets/metro_map.mmd \
  -o docs/images/nf-core-<name>_metro_map_animated.svg \
  --theme light --x-spacing 60 --y-spacing 40 --animate \
  --no-straight-diamonds \
  --logo docs/images/nf-core-<name>_logo_light.png

# Ensure trailing newlines on SVGs (required by pre-commit)
for f in docs/images/nf-core-<name>_metro_map.svg \
         docs/images/nf-core-<name>_metro_map_animated.svg; do
  sed -i '' -e '$a\' "$f"
done
\`\`\`
```

## When to use savepoint-quality params

For pipelines with many sections, multiple study-type lines, or visible
section-boundary issues at the default 60/40 spacing, switch to:

```
--theme light --x-spacing 70 --y-spacing 55 \
--no-straight-diamonds --line-order definition --center-ports
```

This is what nf-core/differentialabundance uses. It costs a bit of canvas
size but produces a much more readable diagram for branching workflows.

## README embed

In the pipeline `README.md`, embed the animated SVG:

```markdown
<picture>
  <source media="(prefers-color-scheme: dark)"
          srcset="docs/images/nf-core-<name>_metro_map_animated.svg">
  <img alt="nf-core/<name> metro map"
       src="docs/images/nf-core-<name>_metro_map_animated.svg">
</picture>
```

(Adjust the `<picture>` block to whatever pattern the pipeline already uses
for its logo or hero image.)

## Pre-commit / linting notes

- nf-core pre-commit hooks insist on trailing newlines in SVGs. The `sed`
  one-liner above fixes this on macOS; on Linux use `sed -i -e '$a\'` (no
  empty string after `-i`).
- The animated SVG is large (often >500 KB). That's expected — don't try
  to minify it; the animations rely on inline scripts and styling.
- If the pipeline has a linter rule that flags large files, add an
  exception for `docs/images/*_animated.svg`.

## Minimum nf-metro version

Pin to the lowest version that has the directives you actually use. As of
this skill's writing:

- `%%metro file:` and basic icons — 0.4.x
- `%%metro off_track:` — 0.5.0
- `%%metro grid:` with `rowspan`/`colspan` — 0.5.2
- Stacked-files and folder icons — 0.5.4

Check this repo's `CHANGELOG.md` for the current state.
