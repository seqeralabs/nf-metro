import { visit } from "unist-util-visit";

const PROD_BASE = "/nf-metro/";

/**
 * Rewrite production-base internal links to the build's actual base.
 *
 * Docs are authored with absolute `/nf-metro/<slug>` links - readable and
 * stable regardless of where a build is deployed. Versioned deploys set a
 * different base (e.g. `/nf-metro/dev/`, `/nf-metro/0.7.2/`), so the same page
 * lives under that prefix instead. This rewrites the authored prefix to the
 * active base so links resolve at runtime on every deploy, and so the links
 * validator (a rehype pass, which runs after this remark pass) sees the
 * resolved URL and validates it against real pages.
 *
 * @param {string} base The Astro `base`, always ending in `/`.
 */
export function remarkRebaseLinks({ base }) {
  if (base === PROD_BASE) return () => {};

  const rebase = (url) =>
    typeof url === "string" && url.startsWith(PROD_BASE)
      ? base + url.slice(PROD_BASE.length)
      : url;

  return (tree) => {
    visit(tree, (node) => {
      if (node.type === "link") {
        node.url = rebase(node.url);
      } else if (
        node.type === "mdxJsxFlowElement" ||
        node.type === "mdxJsxTextElement"
      ) {
        for (const attr of node.attributes ?? []) {
          if (attr.type === "mdxJsxAttribute" && attr.name === "href") {
            attr.value = rebase(attr.value);
          }
        }
      }
    });
  };
}
