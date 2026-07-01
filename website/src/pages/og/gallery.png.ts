import type { APIRoute } from "astro";
import { renderOgImage, pngResponse } from "../../lib/og-image.mjs";
import { OG_GALLERY_MAP } from "../../lib/og-targets.mjs";
import { GALLERY_PAGE } from "../../lib/page-meta";

export const GET: APIRoute = async () => {
  const png = await renderOgImage({
    kicker: "nf-metro",
    title: GALLERY_PAGE.title,
    subtitle: GALLERY_PAGE.description,
    mmdPath: OG_GALLERY_MAP,
  });
  return pngResponse(png);
};
