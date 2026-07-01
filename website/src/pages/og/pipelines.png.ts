import type { APIRoute } from "astro";
import { renderOgImage, pngResponse } from "../../lib/og-image.mjs";
import { OG_PIPELINES_MAP } from "../../lib/og-targets.mjs";
import { PIPELINES_PAGE } from "../../lib/page-meta";

export const GET: APIRoute = async () => {
  const png = await renderOgImage({
    kicker: "nf-metro",
    title: PIPELINES_PAGE.title,
    subtitle: PIPELINES_PAGE.description,
    mmdPath: OG_PIPELINES_MAP,
  });
  return pngResponse(png);
};
