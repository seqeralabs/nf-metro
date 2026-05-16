# Metro map

The pipeline overview metro map is generated from `assets/metro_map.mmd`
using [nf-metro](https://github.com/pinin4fjords/nf-metro). If you add or
rename pipeline steps, update the `.mmd` source and regenerate the images:

```bash
<install-line>

# Static SVG
nf-metro render assets/metro_map.mmd \
  -o docs/images/nf-core-<name>_metro_map.svg \
  --theme light --x-spacing <x-spacing> --y-spacing <y-spacing> \
  --no-straight-diamonds <extra-layout-flags> \
  --logo docs/images/nf-core-<name>_logo_light.png

# PNG conversion (cairosvg)
python -c "import cairosvg; cairosvg.svg2png(
    url='docs/images/nf-core-<name>_metro_map.svg',
    write_to='docs/images/nf-core-<name>_metro_map.png', output_width=2265)"

# Animated SVG (used in README)
nf-metro render assets/metro_map.mmd \
  -o docs/images/nf-core-<name>_metro_map_animated.svg \
  --theme light --x-spacing <x-spacing> --y-spacing <y-spacing> --animate \
  --no-straight-diamonds <extra-layout-flags> \
  --logo docs/images/nf-core-<name>_logo_light.png

# Ensure trailing newlines on SVGs (required by pre-commit)
for f in docs/images/nf-core-<name>_metro_map.svg \
         docs/images/nf-core-<name>_metro_map_animated.svg; do
  sed -i '' -e '$a\' "$f"
done
```

## Placeholders

- `<name>` - pipeline slug (e.g. `rnaseq`, `differentialabundance`,
  `funcscan`). Drop the `nf-core-` prefix on non-nf-core pipelines but
  keep the rest of the path consistent.
- `<install-line>` - one of:
  - `pip install 'nf-metro>=X.Y.Z' cairosvg` once a release contains the
    fixes the pipeline needs (the steady state).
  - `pip install 'git+https://github.com/<owner>/nf-metro.git@<pipeline-name>' cairosvg`
    while a fix chain against nf-metro main is still in flight. The
    `<pipeline-name>` branch on the fork carries the savepoint state used
    to produce the shipped images.
- `<x-spacing>`, `<y-spacing>` - defaults `60 40` (rnaseq baseline) or
  `70 55` (fan-heavy / multi-branch pipelines).
- `<extra-layout-flags>` - empty for the rnaseq baseline. For multi-branch
  pipelines, typically `--line-order definition --center-ports`.

## Platform note

The trailing-newline `sed` invocation uses BSD syntax (`sed -i '' -e '$a\'`).
On GNU/Linux drop the empty string argument:

```bash
sed -i -e '$a\' "$f"
```
