import type { APIRoute } from "astro";
import { renderOgImage, pngResponse } from "../../lib/og-image.mjs";
import { OG_DEFAULT_MAP } from "../../lib/og-targets.mjs";

/** Site-wide fallback OG image for pages without a more specific one (guide, CLI reference, etc). */
export const GET: APIRoute = async () => {
  const png = await renderOgImage({
    kicker: "nf-metro",
    title: "Metro-map diagrams for Nextflow pipelines",
    subtitle:
      "Generate metro-map-style SVG diagrams from Mermaid graph definitions.",
    mmdPath: OG_DEFAULT_MAP,
  });
  return pngResponse(png);
};
