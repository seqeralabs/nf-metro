/**
 * `<meta>` tag pair pointing og:image/twitter:image at an absolute image URL.
 * Pure (no site/base resolution) so it's safely importable from
 * astro.config.mjs, which runs outside Vite's `import.meta.env` pipeline and
 * resolves `base` a different way than the Astro runtime does - see
 * `ogImageHead` in og-head.ts for the page-level equivalent that resolves
 * the URL from a relative path via the runtime's `base`/`PAGES_ORIGIN`.
 * @param {string} url
 */
export function ogImageMetaTags(url) {
  return [
    { tag: "meta", attrs: { property: "og:image", content: url } },
    { tag: "meta", attrs: { name: "twitter:image", content: url } },
  ];
}
