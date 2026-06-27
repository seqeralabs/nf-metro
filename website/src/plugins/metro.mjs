/**
 * Astro integration + satteri mdast plugin that renders %%metro fenced code
 * blocks as inline SVG at build time.
 *
 * Any ```mermaid fenced block whose content contains at least one %%metro
 * directive is intercepted before astro-mermaid sees it, rendered via the
 * nf-metro CLI (Python), and replaced with a raw <svg> HTML node. Blocks
 * without %%metro directives pass through untouched to astro-mermaid.
 *
 * Rendered SVGs are cached by SHA-256 of the block content under
 * website/.metro-cache/ so re-builds and hot-reload only shell out on the
 * first encounter of each unique block. The cache is excluded from git.
 *
 * Requires nf-metro to be on PATH (activate the nf-core micromamba env first).
 * If nf-metro is absent and the cache is cold, the build fails with a clear
 * error message.
 */

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { satteri, isSatteriProcessor } from '@astrojs/markdown-satteri';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CACHE_DIR = join(__dirname, '../../.metro-cache');
mkdirSync(CACHE_DIR, { recursive: true });

/** drawsvg emits an XML prolog; strip it before inlining into HTML. */
const XML_PROLOG_RE = /^<\?xml.*?>\s*/;

export function renderMetro(content) {
  const hash = createHash('sha256').update(content).digest('hex').slice(0, 16);
  const cacheFile = join(CACHE_DIR, `${hash}.svg`);

  try {
    return readFileSync(cacheFile, 'utf-8');
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }

  const tmpInput = join(tmpdir(), `metro-${hash}.mmd`);
  const tmpOutput = join(tmpdir(), `metro-${hash}.svg`);
  writeFileSync(tmpInput, content, 'utf-8');
  try {
    execFileSync(
      'nf-metro',
      ['render', tmpInput, '-o', tmpOutput, '--no-self-color-scheme'],
      { stdio: ['ignore', 'pipe', 'pipe'] },
    );
  } catch (err) {
    const stderr = err.stderr?.toString().trim() || '';
    throw new Error(
      `nf-metro render failed for %%metro block:\n${stderr}\n\n` +
        `Make sure 'nf-metro' is on PATH (activate the nf-core micromamba env).`,
    );
  } finally {
    try { unlinkSync(tmpInput); } catch {}
  }

  const svg = readFileSync(tmpOutput, 'utf-8').replace(XML_PROLOG_RE, '');
  try { unlinkSync(tmpOutput); } catch {}
  writeFileSync(cacheFile, svg, 'utf-8');
  return svg;
}

/**
 * Must be listed before astro-mermaid's plugin so %%metro blocks are claimed
 * first and regular mermaid blocks pass through intact.
 */
export const satteriMetroPlugin = {
  name: 'nf-metro',
  code(node) {
    if (node.lang !== 'mermaid' || !node.value.includes('%%metro')) return;
    return { type: 'html', value: renderMetro(node.value) };
  },
};

/** @returns {import('astro').AstroIntegration} */
export function metroPlugin() {
  return {
    name: 'nf-metro',
    hooks: {
      'astro:config:setup'({ config, updateConfig, logger }) {
        const existingProcessor = config.markdown?.processor;
        if (!isSatteriProcessor(existingProcessor)) {
          logger.warn('nf-metro: markdown processor is not satteri; %%metro blocks will not render');
          return;
        }
        const existingOptions = existingProcessor.options ?? {};
        updateConfig({
          markdown: {
            processor: satteri({
              ...existingOptions,
              mdastPlugins: [...(existingOptions.mdastPlugins ?? []), satteriMetroPlugin],
            }),
          },
        });
        logger.info('nf-metro: registered satteri mdast plugin');
      },
    },
  };
}
