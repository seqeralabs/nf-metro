// @ts-check
import { defineEcConfig } from "@astrojs/starlight/expressive-code";
import { pluginCodeblocks } from "starlight-codeblocks/expressive-code";
// Custom TextMate grammars, imported as JSON so they resolve at bundle/load
// time. A runtime fs read keyed on import.meta.url breaks when this config is
// bundled for the <Code> component (the URL points at the emitted chunk, not
// the source), which silently drops every option from that render path.
//
// `metro` highlights nf-metro's dialect in ```metro / ```mmd fences and <Code>
// blocks (%%metro directives, graph/subgraph keywords, edges, labels, hex
// colors); real Mermaid lives in ```mermaid fences, rendered as diagrams by the
// astro-mermaid integration. `lark` covers the Lark grammar in the parser docs,
// which Shiki has no bundled language for.
import metroGrammar from "./src/grammars/metro.tmLanguage.json" with { type: "json" };
import larkGrammar from "./src/grammars/lark.tmLanguage.json" with { type: "json" };

// Expressive Code config lives here (rather than inline in astro.config.mjs)
// because the <Code> component requires these options to be loadable on their
// own, and a plugin instance is not JSON-serializable.

export default defineEcConfig({
  shiki: { langs: [metroGrammar, larkGrammar] },
  // starlight-codeblocks options live on codeblocks() in astro.config.mjs.
  plugins: [pluginCodeblocks()],
});
