// @ts-check
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, fontProviders } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightLinksValidator from "starlight-links-validator";
import sitemap from "@astrojs/sitemap";
import mermaid from "astro-mermaid";
import { metroVitePlugin } from "./src/lib/render-metro.mjs";
import { starlightGitFix } from "./src/lib/starlight-git-fix.mjs";
import { remarkRebaseLinks } from "./src/lib/rebase-links.mjs";
import { GITHUB_URL, PAGES_ORIGIN } from "./src/repo";

// Expressive Code options (custom grammars + the color-chips plugin) live in
// ec.config.mjs - the <Code> component requires them to be loadable separately.

// Canonical Pages origin (owner-specific value lives in src/repo).
const site = PAGES_ORIGIN;
// Versioned deploys live at /nf-metro/<latest|dev|x.y.z>/; the deploy workflow
// passes the target path via DOCS_BASE so each build's links self-resolve.
const base = process.env.DOCS_BASE ?? "/nf-metro/";

// The committed example .mmd files live at the repo root (../examples), one
// level above this Astro project. The guide imports them as raw strings so its
// code blocks stay in lockstep with the renders they document - single source
// of truth, no copy-paste drift. `@examples` aliases that dir; `fs.allow` opens
// it to the dev server (which otherwise restricts /@fs/ to the project root).
const examplesDir = fileURLToPath(new URL("../examples", import.meta.url));
const componentsDir = fileURLToPath(
  new URL("./src/components", import.meta.url),
);
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

// Compare two dotted version strings (e.g. "0.7.2", "0.1") so the larger sorts
// first (descending). Missing patch components count as 0.
/** @param {string} a @param {string} b */
function compareVersionsDesc(a, b) {
  const pa = a.split(".").map(Number);
  const pb = b.split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pb[i] ?? 0) - (pa[i] ?? 0);
    if (diff) return diff;
  }
  return 0;
}

// Build the Releases sidebar group from the release Markdown files on disk:
// an "Overview" link followed by `v<major>.<minor>.x` sub-groups, newest first.
// New release pages appear automatically - no manual sidebar edits needed.
function buildReleasesSidebar() {
  const dir = new URL("./src/content/docs/releases", import.meta.url);
  const versions = readdirSync(dir)
    .filter((file) => /\.mdx?$/.test(file) && !/^index\.mdx?$/.test(file))
    .map((file) => file.replace(/\.mdx?$/, ""))
    .sort(compareVersionsDesc);

  /** @type {Map<string, string[]>} */
  const groups = new Map();
  for (const version of versions) {
    const [major, minor] = version.split(".");
    const key = `v${major}.${minor}.x`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)?.push(version);
  }

  return [
    { label: "Overview", slug: "releases" },
    ...[...groups.entries()].map(([label, vers], index) => ({
      label,
      // Most recent version group stays expanded; older ones collapse by default.
      collapsed: index !== 0,
      items: vers.map((version) => ({
        label: `v${version}`,
        slug: `releases/${version}`,
      })),
    })),
  ];
}

// https://astro.build/config
export default defineConfig({
  site,
  base,
  markdown: {
    remarkPlugins: [[remarkRebaseLinks, { base }]],
  },
  vite: {
    // Renders `<path>.mmd?metro` imports to inline SVG via the nf-metro CLI.
    plugins: [starlightGitFix(), metroVitePlugin()],
    resolve: {
      alias: {
        "@examples": examplesDir,
        "@components": componentsDir,
      },
      // docs/ is symlinked into src/content/docs, so guide.mdx's real path sits
      // outside this project. Keep the symlinked path during resolution so its
      // bare imports (@astrojs/starlight/components) find website/node_modules.
      preserveSymlinks: true,
    },
    server: { fs: { allow: [repoRoot] } },
  },
  // Degular (Seqera display face) via Astro's Fonts API: self-hosts, emits the
  // @font-face + a metric-matched fallback, and (with <Font preload> in
  // src/components/Head.astro) preloads it to avoid the page-title FOUT.
  fonts: [
    {
      name: "Degular",
      cssVariable: "--nfm-degular",
      provider: fontProviders.local(),
      // The local provider reads its @font-face variants from `options`.
      options: {
        variants: [
          {
            weight: 600,
            style: "normal",
            src: ["./src/fonts/Degular-Semibold.woff2"],
          },
        ],
      },
    },
  ],
  integrations: [
    sitemap(),
    // Renders ```mermaid fences as diagrams. Must come BEFORE starlight so its
    // transform runs before Expressive Code claims the code block. autoTheme
    // follows Starlight's `data-theme` toggle (dark <-> light/neutral).
    mermaid({
      theme: "dark",
      autoTheme: true,
      mermaidConfig: { securityLevel: "loose" },
    }),
    starlight({
      plugins: [
        starlightLinksValidator({
          // Docs author internal links with the production `/nf-metro/` base;
          // remarkRebaseLinks rewrites that prefix to the active build base
          // before this validator runs, so cross-references validate against
          // real pages on every base. Only links the validator structurally
          // cannot resolve are excluded:
          // - gallery/ and pipelines/ are custom Astro routes, not Starlight
          //   content entries, so the validator can only see them as opaque
          //   custom pages.
          // - live_demo.mp4 is a public/ static asset, not a navigable page.
          exclude: ({ link }) =>
            link.startsWith(`${base}gallery`) ||
            link.startsWith(`${base}pipelines`) ||
            link === "../assets/live_demo.mp4",
        }),
      ],
      title: "nf-metro",
      description:
        "Metro-map-style SVG diagrams from Mermaid graph definitions with %%metro directives - for visualizing bioinformatics pipeline workflows.",
      favicon: "/favicon.svg",
      lastUpdated: true,
      // Expressive Code options (grammars + color-chips plugin) are in ec.config.mjs.
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: GITHUB_URL,
        },
      ],
      editLink: {
        baseUrl: `${GITHUB_URL}/edit/main/docs/`,
      },
      components: {
        Head: "./src/components/Head.astro",
        Header: "./src/components/Header.astro",
        ThemeSelect: "./src/components/ThemeSelect.astro",
        PageFrame: "./src/components/PageFrame.astro",
        PageTitle: "./src/components/PageTitle.astro",
        EditLink: "./src/components/EditLink.astro",
      },
      customCss: ["./src/styles/custom.css"],
      // The docs-nav sidebar. Identical on every page (including the custom home).
      sidebar: [
        {
          label: "Overview",
          items: [
            // Starlight prepends `base` to sidebar `link` values, so these are
            // base-relative ("/" -> "/nf-metro/"); passing `base` here doubled it.
            { label: "Home", link: "/" },
            { label: "Guide", slug: "guide" },
            { label: "CLI reference", slug: "cli" },
            { label: "Gallery", link: "/gallery/" },
            { label: "nf-core pipelines", link: "/pipelines/" },
            {
              label: "Playground",
              link: "/playground/",
              badge: { text: "beta", variant: "caution" },
              // data-astro-reload forces a full navigation so the CDN scripts
              // (CodeMirror, Pyodide) and app.js re-initialise from scratch
              // instead of being skipped by Astro's ClientRouter.
              attrs: { target: "_self", "data-astro-reload": true },
            },
          ],
        },
        {
          label: "Embedding & data",
          items: [
            { label: "Embedding", slug: "embedding" },
            { label: "Embed contract", slug: "embed" },
            { label: "Data manifest", slug: "manifest" },
            { label: "Live progress", slug: "live" },
            { label: "Nextflow import", slug: "nextflow" },
            { label: "How it's built", slug: "how-its-built" },
          ],
        },
        {
          label: "Internals",
          collapsed: true,
          // Labels + order come from each page's `sidebar` frontmatter in dev/.
          items: [{ autogenerate: { directory: "dev" } }],
        },
        { label: "Contributing", slug: "contributing" },
        { label: "Credits", slug: "credits" },
        {
          label: "Releases",
          collapsed: true,
          // Built from the release Markdown files on disk (see buildReleasesSidebar).
          items: buildReleasesSidebar(),
        },
      ],
    }),
  ],
});
