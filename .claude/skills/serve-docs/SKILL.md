---
name: serve-docs
description: Spin up the Astro / Starlight documentation site locally for live preview, optionally for a specific nf-metro branch. Use when the user wants to run, serve, preview, or develop the docs site (website/) - phrases like "serve the docs", "spin up the site", "run the docs locally", "preview the website", "start the astro server", "preview the docs for branch X / this PR", or when iterating on docs content, theme/layout overrides, or the gallery/pipelines/playground pages and wanting to see changes in a browser. Wraps scripts/serve_docs.sh, which generates the git-ignored dynamic content (gallery, pipelines, playground manifest) before starting the dev server, and with --branch checks the branch out into an isolated worktree and installs its nf_metro so the renders reflect that branch.
allowed-tools: Bash(scripts/serve_docs.sh*), Bash(./scripts/serve_docs.sh*), Bash(source *), Bash(cd *), Bash(curl *)
---

# Serve the docs site locally

The docs live in two places (see `CLAUDE.md`): `docs/` holds the hand-written
Markdown, and `website/` is the Astro / Starlight app that renders it. The
Gallery, nf-core pipelines, and playground pages plus all rendered example SVGs
are **generated** and git-ignored, so a bare `npm run dev` gives an incomplete
site. `scripts/serve_docs.sh` handles the generation and the dev server in one
command.

## Conventions

- Local checkout: `~/projects/nf-metro` (substitute if yours differs).
- The gallery/playground generators import the `nf_metro` Python package, so the
  project env must be active. The convention here is the `nf-metro` micromamba
  env: `source ~/.local/bin/mm-activate nf-metro`. If `nf_metro` is not
  importable the script warns and still starts the server, but the
  Gallery / pipelines / playground pages will be empty.
- The dev server serves at **http://localhost:4321/nf-metro/** (note the
  `/nf-metro/` base path).

## Step 1: Activate the Python env

```bash
source ~/.local/bin/mm-activate nf-metro
```

## Step 2: Run the script

Preview a specific branch (the common case - e.g. reviewing a PR's docs or
render changes):

```bash
# Checks <ref> out into an isolated worktree (../nf-metro-serve-<ref>),
# installs that branch's nf_metro so the gallery renders match the branch,
# regenerates content, and serves.
scripts/serve_docs.sh --branch <ref>
```

Or preview the current checkout:

```bash
# First run: generates gallery + playground content (slow), then serves.
# Subsequent runs reuse the content and start almost immediately.
scripts/serve_docs.sh
```

Useful flags:

| Flag | Effect |
|---|---|
| _(none)_ | Generate dynamic content if missing (for the current checkout), then start the dev server. |
| `--branch <ref>` | Serve a specific branch: set up/reuse a worktree, install its `nf_metro`, regenerate content, serve. Always regenerates so renders match the ref. |
| `--worktree-dir <p>` | Where `--branch` places its worktree (default `../nf-metro-serve-<ref>`). |
| `--rebuild` | Force-regenerate gallery + playground content (use after editing `examples/` or the layout engine). |
| `--skip-content` | Skip content generation entirely - fastest when you only touch theme/layout/Markdown and already have content. |
| `--preview` | Production `astro build` + `astro preview` instead of the dev server (mirrors the deployed output more closely). |
| `--no-install` | Skip the npm dependency check. |
| `-- <args>` | Everything after `--` is forwarded to astro, e.g. `-- --host 0.0.0.0 --port 4000`. |

`--branch` installs the branch's `nf_metro` (editable) into the **active** env,
so afterwards that env points at the worktree. Re-point it with
`pip install -e ~/projects/nf-metro` when you go back to the main checkout, or
use a dedicated env for branch previews.

## Step 3: Open it

Visit **http://localhost:4321/nf-metro/**. The dev server hot-reloads on
edits to `website/` and `docs/`. Stop it with `Ctrl-C`.

## Notes

- `--branch` serves a **detached** checkout of the ref, so it works even when
  that branch is already checked out in another worktree (e.g. a dev worktree).
- Content (gallery/pipelines/playground) is regenerated, not hot-reloaded:
  re-run with `--rebuild` after changing `examples/` or the rendering code.
- For a visual regression diff of renders against `origin/main`, that's a
  different job - see the `render-topologies` skill.
