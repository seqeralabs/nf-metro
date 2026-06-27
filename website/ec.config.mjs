// @ts-check
import { readFileSync } from "node:fs";
import {
  defineEcConfig,
  definePlugin,
} from "@astrojs/starlight/expressive-code";
import { pluginColorChips } from "expressive-code-color-chips";

// expressive-code-color-chips v≥0.2 supports a `languages` option so we can
// include metro/mmd fences alongside the default CSS dialects.
const COLOR_CHIP_LANGS = [
  "css",
  "scss",
  "sass",
  "less",
  "stylus",
  "metro",
  "mmd",
];

// The official plugin hardcodes `vertical-align: text-bottom`, which sits the
// chip too low against the line text. This plugin, loaded after
// pluginColorChips(), nudges all chips up to the text midline.
function pluginChipVerticalAlign() {
  return definePlugin({
    name: "ChipVerticalAlign",
    baseStyles() {
      return `.ec-css-color-chip::before { vertical-align: middle; margin-bottom: 2px; }`;
    },
  });
}

// Expressive Code config lives here (rather than inline in astro.config.mjs)
// because the <Code> component requires these options to be loadable on their
// own, and a plugin instance like pluginColorChips() is not JSON-serializable.

// Custom TextMate grammar so ```metro / ```mmd blocks highlight nf-metro's
// dialect (%%metro directives, graph/subgraph keywords, edges, node labels,
// hex colors). Real Mermaid lives in ```mermaid fences, rendered as diagrams
// by the astro-mermaid integration in astro.config.mjs.
const metroGrammar = JSON.parse(
  readFileSync(
    new URL("./src/grammars/metro.tmLanguage.json", import.meta.url),
    "utf8",
  ),
);

// Shiki has no bundled Lark grammar, so ```lark fences in the parser docs would
// render unhighlighted. This custom TextMate grammar covers Lark's rule/terminal
// definitions, priorities, regex/string literals, and %directives.
const larkGrammar = JSON.parse(
  readFileSync(
    new URL("./src/grammars/lark.tmLanguage.json", import.meta.url),
    "utf8",
  ),
);

export default defineEcConfig({
  shiki: { langs: [metroGrammar, larkGrammar] },
  // Render a color swatch next to hex/rgb values — handy for the `#hex` line
  // colors in %%metro directives. Square-ish chips (15%) rather than the
  // plugin's default circle (50%).
  plugins: [
    pluginColorChips({ languages: COLOR_CHIP_LANGS }),
    pluginChipVerticalAlign(),
  ],
  styleOverrides: {
    colorChips: {
      borderRadius: "15%",
      size: "0.9em", // a little smaller than the default 1.2em
    },
  },
});
