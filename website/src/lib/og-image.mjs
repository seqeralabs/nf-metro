/**
 * Build-time Open Graph preview images: a branded 1200x630 card pairing a
 * pipeline/gallery map (rendered with baked colours via `renderMetroFile`,
 * since satori/resvg can't resolve the live `light-dark()`/`var()` chrome
 * `<Metro>` relies on) with the entry's title and description.
 *
 * satori lays out the card (title, description, embedded map image) as an
 * SVG using vector glyph paths - no system font or CSS text-rendering
 * dependency - which sharp then rasterizes to PNG, matching the SVG→PNG step
 * the CLI's own `--no-chrome-css` raster workflow already relies on.
 */

import satori from "satori";
import sharp from "sharp";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderMetroFile, REPO_ROOT } from "./render-metro.mjs";

const WIDTH = 1200;
const HEIGHT = 630;
// A header band across the top leaves the full card width for the map below -
// pipeline maps are usually landscape, so this fits far more of one than a
// side panel would.
const HEADER_HEIGHT = 170;
const HEADER_PADDING_X = 48;
const TEXT_MAX_WIDTH = 860;
const MAP_STAGE_HEIGHT = HEIGHT - HEADER_HEIGHT;
const MAP_STAGE_PADDING = 32;
const MAP_MAX_WIDTH = WIDTH - MAP_STAGE_PADDING * 2;
const MAP_MAX_HEIGHT = MAP_STAGE_HEIGHT - MAP_STAGE_PADDING * 2;

// Mirrors the dark-mode brand palette in website/src/styles/custom.css
// (--nfm-brand, --nfm-accent-text, --nfm-ink, --nfm-ink-dim) - satori can't
// consume CSS custom properties, so these are duplicated as literals; keep
// them in sync by hand if that palette changes.
const BRAND_DARK = "#201637";
const MAP_STAGE = "#161022";
const ACCENT = "#56d3ba";
const INK = "#ffffff";
const INK_MUTE = "#c9c2da";

// cwd is website/ (see render-metro.mjs), so this resolves directly without
// round-tripping through REPO_ROOT and back down into website/.
const FONT_DIR = join(process.cwd(), "node_modules/@fontsource/inter/files");
const fonts = [
  {
    name: "Inter",
    data: readFileSync(join(FONT_DIR, "inter-latin-400-normal.woff")),
    weight: 400,
    style: "normal",
  },
  {
    name: "Inter",
    data: readFileSync(join(FONT_DIR, "inter-latin-700-normal.woff")),
    weight: 700,
    style: "normal",
  },
];

// website/src/assets/logo.svg colours its lines/capsules with the site's CSS
// custom properties, which satori can't resolve - bake in the constant
// (mode-independent) Nextflow green and this card's own ink/background so
// the capsule "cuts into" the card the same way it does the live page.
const LOGO_HEIGHT = 34;
const LOGO_VIEWBOX_ASPECT = 192 / 45;
const LOGO_WIDTH = Math.round(LOGO_HEIGHT * LOGO_VIEWBOX_ASPECT);
const LOGO_SVG = readFileSync(
  join(REPO_ROOT, "website/src/assets/logo.svg"),
  "utf-8",
)
  .replaceAll("var(--nfm-line-a)", "#31c9ac")
  .replaceAll("var(--nfm-line-b)", "#0a967b")
  .replaceAll("var(--nfm-bg)", BRAND_DARK)
  .replaceAll("var(--nfm-ink)", INK);

let logoDataUri;
async function rasterizeLogo() {
  if (!logoDataUri) {
    const png = await sharp(Buffer.from(LOGO_SVG), { density: 400 })
      .resize({ height: LOGO_HEIGHT * 3 })
      .png()
      .toBuffer();
    logoDataUri = `data:image/png;base64,${png.toString("base64")}`;
  }
  return logoDataUri;
}

/** Truncate to `max` chars on a word boundary, appending an ellipsis. */
function truncate(text, max) {
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const lastSpace = cut.lastIndexOf(" ");
  return `${cut.slice(0, lastSpace > 0 ? lastSpace : max)}…`;
}

/** Smaller font for longer titles so the fixed-height header never clips. */
function titleFontSize(title) {
  if (title.length > 40) return 30;
  if (title.length > 26) return 36;
  return 42;
}

/**
 * Render the target `.mmd` with baked colours and rasterize it to a PNG
 * buffer sized to fit within the card's map stage.
 * @param {string} mmdRelPath  Repo-relative path, e.g. "examples/rnaseq_auto.mmd".
 */
async function rasterizeMap(mmdRelPath) {
  const svg = renderMetroFile(join(REPO_ROOT, mmdRelPath), {
    chromeCss: false,
  });
  const { data, info } = await sharp(Buffer.from(svg), { density: 200 })
    .resize({
      width: MAP_MAX_WIDTH,
      height: MAP_MAX_HEIGHT,
      fit: "inside",
      withoutEnlargement: true,
    })
    .png()
    .toBuffer({ resolveWithObject: true });
  return {
    dataUri: `data:image/png;base64,${data.toString("base64")}`,
    width: info.width,
    height: info.height,
  };
}

/**
 * Render an OG preview card: a title/description header band above the
 * pipeline's metro map, which gets the full card width to work with.
 * @param {{ kicker: string, title: string, subtitle: string, mmdPath: string }} opts
 * @returns {Promise<Buffer>} PNG bytes.
 */
export async function renderOgImage({ kicker, title, subtitle, mmdPath }) {
  const [map, logo] = await Promise.all([rasterizeMap(mmdPath), rasterizeLogo()]);

  const tree = {
    type: "div",
    props: {
      style: {
        width: WIDTH,
        height: HEIGHT,
        display: "flex",
        flexDirection: "column",
        fontFamily: "Inter",
        background: BRAND_DARK,
      },
      children: [
        {
          type: "div",
          props: {
            style: {
              width: WIDTH,
              height: HEADER_HEIGHT,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: `0 ${HEADER_PADDING_X}px`,
            },
            children: [
              {
                type: "div",
                props: {
                  style: {
                    display: "flex",
                    flexDirection: "column",
                    maxWidth: TEXT_MAX_WIDTH,
                  },
                  children: [
                    {
                      type: "div",
                      props: {
                        style: {
                          fontSize: 18,
                          fontWeight: 700,
                          color: ACCENT,
                          textTransform: "uppercase",
                          letterSpacing: 3,
                        },
                        children: kicker,
                      },
                    },
                    {
                      type: "div",
                      props: {
                        style: {
                          fontSize: titleFontSize(title),
                          fontWeight: 700,
                          color: INK,
                          marginTop: 10,
                          lineHeight: 1.15,
                        },
                        children: truncate(title, 70),
                      },
                    },
                    {
                      type: "div",
                      props: {
                        style: {
                          fontSize: 19,
                          fontWeight: 400,
                          color: INK_MUTE,
                          marginTop: 8,
                          lineHeight: 1.35,
                        },
                        children: truncate(subtitle, 115),
                      },
                    },
                  ],
                },
              },
              {
                type: "img",
                props: {
                  src: logo,
                  width: LOGO_WIDTH,
                  height: LOGO_HEIGHT,
                  style: { flexShrink: 0 },
                },
              },
            ],
          },
        },
        {
          type: "div",
          props: {
            style: {
              width: WIDTH,
              height: MAP_STAGE_HEIGHT,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: MAP_STAGE,
            },
            children: {
              type: "img",
              props: { src: map.dataUri, width: map.width, height: map.height },
            },
          },
        },
      ],
    },
  };

  const svg = await satori(tree, { width: WIDTH, height: HEIGHT, fonts });
  return sharp(Buffer.from(svg)).png().toBuffer();
}

/**
 * Wrap a PNG buffer as an immutable `Response` for a static `.png.ts` route.
 * Every OG image lives at a URL scoped to one build (versioned deploys and PR
 * previews each get their own `base`), so its bytes never change post-deploy.
 * @param {Buffer} png
 */
export function pngResponse(png) {
  return new Response(png, {
    headers: {
      "Content-Type": "image/png",
      "Cache-Control": "public, max-age=31536000, immutable",
    },
  });
}
