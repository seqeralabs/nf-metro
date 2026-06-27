#!/usr/bin/env bash
#
# Spin up the Astro / Starlight docs site locally - optionally for a specific
# nf-metro branch.
#
# The site (website/) loads dynamic content that is git-ignored and generated
# from the Python package: the gallery + pipelines pages and their SVGs
# (scripts/build_gallery.py) and the playground's example manifest
# (scripts/build_playground_examples.py). Without that content the dev server
# still runs, but the Gallery / nf-core pipelines / playground pages are empty
# or 404. This script generates it (once, then reuses it) and starts the dev
# server, so a clean checkout reaches a complete local site in one command.
#
# The renders on the Gallery / pipelines pages are produced by the nf_metro
# package, so previewing a branch means rendering with that branch's code. With
# --branch, the script checks the branch out into an isolated git worktree,
# installs that worktree's nf_metro into the active Python env, and serves from
# it - so the local site reflects the branch end to end.
#
# Usage:
#   scripts/serve_docs.sh                      # serve from the current checkout
#   scripts/serve_docs.sh --branch <ref>       # serve from a worktree on <ref>
#   scripts/serve_docs.sh --rebuild            # force-regenerate gallery + playground content
#   scripts/serve_docs.sh --skip-content       # skip content generation entirely (fastest)
#   scripts/serve_docs.sh --preview            # production build + preview instead of dev
#   scripts/serve_docs.sh --no-install         # skip the npm dependency check
#   scripts/serve_docs.sh --worktree-dir <p>   # where --branch puts its worktree
#   scripts/serve_docs.sh -- --host 0.0.0.0    # forward remaining args to astro
#
# Any arguments after `--` are forwarded to `astro dev` / `astro preview`
# (e.g. `-- --host 0.0.0.0 --port 4000`).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BRANCH=""
WORKTREE_DIR=""
REBUILD=false
SKIP_CONTENT=false
PREVIEW=false
NO_INSTALL=false
ASTRO_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH="${2:?--branch needs a ref}"; shift 2 ;;
    --worktree-dir) WORKTREE_DIR="${2:?--worktree-dir needs a path}"; shift 2 ;;
    --rebuild) REBUILD=true; shift ;;
    --skip-content) SKIP_CONTENT=true; shift ;;
    --preview) PREVIEW=true; shift ;;
    --no-install) NO_INSTALL=true; shift ;;
    --) shift; ASTRO_ARGS=("$@"); break ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'
      exit 0 ;;
    *) echo "Unknown option: $1 (use -- to pass args to astro)" >&2; exit 1 ;;
  esac
done

# --- Branch worktree ---------------------------------------------------------
# Point the rest of the script at an isolated worktree for the requested ref and
# install that branch's nf_metro so the gallery renders reflect the branch.
if [[ -n "$BRANCH" ]]; then
  slug="$(printf '%s' "$BRANCH" | tr '/ ' '--')"
  WT="${WORKTREE_DIR:-$REPO_ROOT/../nf-metro-serve-$slug}"

  echo "==> Preparing worktree for '$BRANCH' at $WT"
  git -C "$REPO_ROOT" fetch origin --quiet || true
  # Serve a detached HEAD at the resolved ref: this is a read-only preview, and
  # detaching lets the worktree coexist with the same branch checked out
  # elsewhere (e.g. a dev worktree). Prefer the remote-tracking ref so PR
  # branches reflect what's pushed.
  if git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
    SERVE_REF="origin/$BRANCH"
  else
    SERVE_REF="$BRANCH"
  fi
  if [[ -d "$WT" ]]; then
    git -C "$WT" fetch origin --quiet || true
    git -C "$WT" checkout --detach --quiet "$SERVE_REF"
  else
    git -C "$REPO_ROOT" worktree add --detach "$WT" "$SERVE_REF"
  fi

  REPO_ROOT="$(cd "$WT" && pwd)"

  echo "==> Installing $BRANCH's nf_metro into the active env (gallery renders reflect the branch)"
  if ! pip install -e "$REPO_ROOT[docs]" -q; then
    echo "WARNING: editable install of the branch failed; gallery renders may not" >&2
    echo "         reflect '$BRANCH'. Activate the project env and retry." >&2
  fi
fi

WEBSITE_DIR="$REPO_ROOT/website"

# --- Dynamic content (gallery, pipelines, playground manifest) ---------------
# The gallery markdown is the marker for "content already generated".
GALLERY_MARKER="$REPO_ROOT/docs/gallery/index.md"

generate_content() {
  if ! python -c "import nf_metro" >/dev/null 2>&1; then
    echo "WARNING: the 'nf_metro' package is not importable in this Python." >&2
    echo "         Activate the project env (e.g. 'source ~/.local/bin/mm-activate nf-metro')" >&2
    echo "         or 'pip install -e .', then re-run. Skipping content generation;" >&2
    echo "         the Gallery / pipelines / playground pages will be empty." >&2
    return
  fi
  echo "==> Generating playground example manifest"
  python "$REPO_ROOT/scripts/build_playground_examples.py"
  echo "==> Generating gallery + pipelines pages and render SVGs (slow on first run)"
  python "$REPO_ROOT/scripts/build_gallery.py"
}

if [[ "$SKIP_CONTENT" == true ]]; then
  echo "==> Skipping dynamic content generation (--skip-content)"
elif [[ "$REBUILD" == true || -n "$BRANCH" || ! -f "$GALLERY_MARKER" ]]; then
  # Always regenerate for --branch: stale content would show another ref's renders.
  generate_content
else
  echo "==> Reusing existing generated content (use --rebuild to refresh)"
fi

# --- Node dependencies -------------------------------------------------------
if [[ "$NO_INSTALL" == false ]]; then
  if [[ ! -d "$WEBSITE_DIR/node_modules" || "$WEBSITE_DIR/package-lock.json" -nt "$WEBSITE_DIR/node_modules" ]]; then
    echo "==> Installing npm dependencies"
    (cd "$WEBSITE_DIR" && npm install)
  else
    echo "==> npm dependencies up to date (use --no-install to skip this check)"
  fi
fi

# --- Serve -------------------------------------------------------------------
# bash 3.2 (the macOS system default) errors on "${arr[@]}" for an empty array
# under `set -u`; the guarded form below expands to nothing when ASTRO_ARGS is
# empty and preserves per-element quoting otherwise.
if [[ "$PREVIEW" == true ]]; then
  echo "==> Building static site, then serving the production preview"
  (cd "$WEBSITE_DIR" && npm run build && npm run preview -- ${ASTRO_ARGS[@]+"${ASTRO_ARGS[@]}"})
else
  echo "==> Starting Astro dev server at http://localhost:4321/nf-metro/"
  (cd "$WEBSITE_DIR" && npm run dev -- ${ASTRO_ARGS[@]+"${ASTRO_ARGS[@]}"})
fi
