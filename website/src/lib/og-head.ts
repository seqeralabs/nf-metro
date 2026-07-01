import { base, PAGES_ORIGIN } from "../site";
import { ogImageMetaTags } from "./og-meta-tags.mjs";

/**
 * `head` frontmatter entries pointing `og:image`/`twitter:image` at a
 * build-time OG PNG. Passed to `StarlightPage`/docs frontmatter to override
 * the site-wide default set in astro.config.mjs for a specific page.
 * @param relPath  Path under the site base, e.g. "og/pipelines/rnaseq_auto.png".
 */
export function ogImageHead(relPath: string) {
  return ogImageMetaTags(`${PAGES_ORIGIN}${base}${relPath}`);
}
