/** Site base path, normalised to a trailing slash (respects `base` in astro.config). */
export const base = import.meta.env.BASE_URL.replace(/\/?$/, "/");

/**
 * GitHub Pages project root, constant across versioned deploys. `base` carries
 * the version segment (e.g. /nf-metro/latest/); SITE_BASE is the root the
 * version switcher strips and re-prefixes to build cross-version URLs.
 */
export const SITE_BASE = "/nf-metro/";

/** GitHub owner/repo, URL, and Pages origin - single source of truth lives in ./repo. */
export { REPO, GITHUB_URL, PAGES_ORIGIN } from "./repo";
