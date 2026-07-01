/**
 * `.mmd` sources for the fixed (non-per-entry) OG images. Named constants
 * rather than inlined in each `og/*.png.ts` route so render-metro.mjs's
 * prewarm can also see them and batch-render their chrome-less variant
 * alongside the gallery/pipelines content collections - without this, a
 * fixed OG route whose map isn't otherwise showcased would silently fall
 * back to an individual, unbatched nf-metro invocation on every build.
 */
export const OG_GALLERY_MAP = "examples/rnaseq_sections.mmd";
export const OG_PIPELINES_MAP = "examples/rnaseq_auto.mmd";
export const OG_DEFAULT_MAP = OG_GALLERY_MAP;
