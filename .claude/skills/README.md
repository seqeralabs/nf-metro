# `.claude/skills`

Claude Code skills for working on and with nf-metro. Skills auto-trigger
from the descriptions in each `SKILL.md` frontmatter; for the conventions
each one assumes (local paths, env names, render-preview URL), see the
preamble at the top of the relevant `SKILL.md`.

## Cohort map

The skills fall into three conceptual groups.

### Pipeline-side

For someone integrating nf-metro into their own Nextflow pipeline repo:

| Skill | When to use |
|---|---|
| [`pipeline-metro-diagram`](pipeline-metro-diagram/SKILL.md) | Author the `.mmd` content for a pipeline's metro map: lines, stations, sections, off-track inputs, the render-inspect-edit iteration loop. |
| [`pipeline-metro-setup`](pipeline-metro-setup/SKILL.md) | Wire the rendered map into the pipeline repo: file layout, render commands, README image swap, CHANGELOG, install-line pinning (released version vs named-branch on a fork). |

### nf-metro-side authoring

For someone changing nf-metro itself:

| Skill | When to use |
|---|---|
| [`fix-issue`](fix-issue/SKILL.md) | General end-to-end workflow for a GitHub issue: worktree, environment, implement, test, push, PR. The "skeleton" most other nf-metro authoring tasks build on. |
| [`nf-metro-layout-fix`](nf-metro-layout-fix/SKILL.md) | Drive code-level fixes to nf-metro layout when a real pipeline render exposes a bug. Savepoint pattern, invariant-test-first-then-fix-then-runtime-validator loop, conditional gating, the "improvement ratchet". |
| [`nf-metro-gate-triage`](nf-metro-gate-triage/SKILL.md) | Run a routing gate-arm triage slice: give every un-exercised branch in a `layout/routing/` module a verdict (reachable -> fixture, defensive, candidate-dead, or reachable-but-defective -> file a bug). Wraps the methodology in [`docs/dev/routing_gate_triage.md`](../../docs/dev/routing_gate_triage.md). |
| [`pr-chain-vet`](pr-chain-vet/SKILL.md) | Per-PR vetting on a stacked PR chain: gallery diff vs `main`, classify every changed example, `/simplify` pass, sweep narrative comments, get CI green, post-merge cleanup in the right order. |

### Publishing / CI ops

For keeping the docs site and PR render previews actually reaching the web:

| Skill | When to use |
|---|---|
| [`pages-ci-doctor`](pages-ci-doctor/SKILL.md) | Diagnose and un-stick GitHub Pages publishing when a `_pr/<N>/` preview 404s or shows stale content, or the docs site didn't refresh after a merge. Walks the `pr-renders -> pr-render-publish -> branch build` chain, detects the recurring legacy-builder stall, re-triggers it, and verifies live through the CDN-caching gotchas. |

### Visual verification

Opt-in only (`disable-model-invocation: true` — the user must invoke
explicitly):

| Skill | When to use |
|---|---|
| [`render-topologies`](render-topologies/SKILL.md) | Local pixel-diff of all gallery renders between the current branch and `origin/main`. Only needed for pre-push confidence; the CI render preview on the PR is the authoritative review. |

### Docs site

| Skill | When to use |
|---|---|
| [`serve-docs`](serve-docs/SKILL.md) | Spin up the Astro / Starlight docs site locally for live preview. Wraps `scripts/serve_docs.sh`, which generates the git-ignored gallery / pipelines / playground content and then starts the dev server. |
| [`live-demo-video`](live-demo-video/SKILL.md) | Re-record `website/public/assets/live_demo.mp4`, the live-progress clip embedded in `docs/live.mdx`, when it goes stale (old overlay style, old theme, a fix it predates). Playwright recording against a real `nf-metro serve` + Nextflow run, converted to mp4 without needing system ffmpeg. |

## How the skills relate

- `pipeline-metro-diagram`'s "is it mmd or nf-metro?" triage in Step 5
  hands off to `nf-metro-layout-fix` when the diagnosis is engine-side.
- `nf-metro-layout-fix` Step 4 hands off to `pr-chain-vet` for the
  per-PR vetting workflow that ships the resulting chain back to `main`.
- `pipeline-metro-setup` Stage 2 Case B (named-branch pin on a fork) is
  the bridge developers use *while* `nf-metro-layout-fix` +
  `pr-chain-vet` work is in flight.
- `fix-issue` Step 4 references `render-topologies` for the optional
  pre-push local diff.

## Conventions

Most skills assume:

- Local nf-metro checkout at `~/projects/nf-metro`
- Upstream slug `seqeralabs/nf-metro` (issues + PR targets)
- CI render preview at `seqeralabs.github.io/nf-metro/_pr/<N>/`

If your setup differs, substitute in the commands; the conventions are
called out at the top of each `SKILL.md` so the substitution points are
visible.

## Step / Stage nomenclature

Most skills use `## Step N: ...` for numbered procedural sections.
`pipeline-metro-setup` uses `## Stage N: ...` instead — its four stages
are deliberately coarser-grained (a pipeline-integration journey, not a
debugging checklist) and the word choice signals that.
