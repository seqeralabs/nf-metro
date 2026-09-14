---
name: release
description: End-to-end release workflow for nf-metro. Checks the bioconda recipe for missing deps and bumps its build number if needed, bumps version in pyproject.toml and __init__.py, drafts a docs/releases/<version>.md page from the git log since the last tag (with illustrations where relevant), adds the releases index row and the condensed CHANGELOG.md section, then opens a PR. After merge, reminds you to create the GitHub Release. Trigger on phrases like "cut a release", "release X.Y.Z", "prepare release", "bump version to X.Y.Z".
---

# nf-metro Release Workflow

Covers everything from version bump to open PR. After the PR merges you
create the GitHub Release manually — that triggers PyPI publish and the
versioned docs deploy automatically.

## Step 0: Determine the new version

If the user didn't specify a version, read the current one:

```bash
grep '^version' ~/projects/nf-metro/pyproject.toml
```

Before asking, gather the cycle's changes (Step 1) and read the
`[Unreleased]` section of `CHANGELOG.md`. The CHANGELOG preamble defines the
public API (the CLI, the `.mmd` directive surface, and the embed contract)
and says semver applies from 1.0.0. Check the gathered entries against that
surface:

- Any entry marked **Breaking** against the public API → propose a major
  bump and say which entry makes it breaking.
- Otherwise, new features with no breaking entries → propose a minor bump.
- Only fixes, no new features → propose a patch bump.

Ask: "Current version is X.Y.Z, based on <reasoning> I'd propose X.Y.Z,
does that work, or would you like a different version?" Wait for
confirmation before proceeding either way.

Call the new version `NEW_VERSION` (e.g. `0.8.0`) and find the last
release tag:

```bash
# The repo carries non-version tags (wip/*, savepoint-*, backup-*), so
# `git describe` picks the wrong one. Select the newest `X.Y.Z` tag instead.
LAST_TAG=$(git -C ~/projects/nf-metro tag --sort=-creatordate \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
echo "Last tag: $LAST_TAG"
```

## Step 1: Gather changes since last release

For a small patch release, a plain commit log is enough:

```bash
git -C ~/projects/nf-metro log ${LAST_TAG}..origin/main --oneline
```

For anything larger, a commit log does not scale (a cycle can carry
hundreds of merges), so the primary source is merged PRs, not commits.
Get the merge commits for an overview and the PR list for the detail:

```bash
git -C ~/projects/nf-metro log --merges --first-parent ${LAST_TAG}..origin/main --format='%h %s'

# LAST_TAG_DATE is the last tag's commit date, e.g.:
LAST_TAG_DATE=$(git -C ~/projects/nf-metro log -1 --format=%cs $LAST_TAG)
gh pr list --repo seqeralabs/nf-metro --state merged \
    --search "merged:>${LAST_TAG_DATE}" --limit 1000 \
    --json number,title,labels,mergedAt
```

Group the PR list by theme, not by commit prefix, for example: layout and
routing engine, parser and directives, CLI options, render and themes,
playground and docs site, packaging and CI. A commit-prefix grouping
(`feat:`/`fix:`) stops being useful once a theme spans dozens of PRs.

Keep the plain `git log` as a fallback for reading the detail of an
individual change once you know which PR it belongs to:

```bash
git -C ~/projects/nf-metro log --format="%B" -1 <sha>
```

`CHANGELOG.md`'s `[Unreleased]` section may hold only part of the cycle
(entries land there only when a PR's author remembered to add one), so
treat it as a starting draft to reconcile against the PR list, not as the
complete record.

## Step 2: Bioconda recipe check

Fetch the live recipe and compare its `run:` dependencies against
`pyproject.toml`. Do this **before** touching any files — if deps are
missing from the recipe, fixing it now means the bioconda autobump PR
only needs a version + sha256 change and nothing has to be intercepted
mid-flight.

```bash
# Fetch the live recipe
gh api repos/bioconda/bioconda-recipes/contents/recipes/nf-metro/meta.yaml \
    --jq '.content' | base64 -d

# Read pyproject deps
grep -A 20 '^dependencies' ~/projects/nf-metro/pyproject.toml
```

**What to look for:**

- Any package in `pyproject.toml` `dependencies` that is **absent** from
  the recipe `run:` block is a missing dep.
- Any package in the recipe absent from `pyproject.toml` `dependencies`
  is an extra — flag it but don't remove it without asking. `fonttools` and
  `jsonschema` are deliberate extras: the recipe carries them in `run:` even
  though pyproject only lists them under the `font` and `validate` extras,
  because conda users get `--font` handling and `nf-metro validate-svg`
  without a separate install step. Don't flag those two; the "flag but
  don't remove" rule still applies to any other extra found.
- Version pins don't need to match exactly, but the recipe should cover
  at least the same lower bound as pyproject.toml.

**If there are missing or changed deps**, tell the user clearly before
continuing:

> ⚠️ **Bioconda recipe needs updating before release.**
>
> Missing from `run:` in the recipe:
> - `foo >=1.0`
>
> The recipe's `build: number:` must also be incremented (current: N → N+1)
> when deps change.

Then open a bioconda PR:

1. Fork `bioconda/bioconda-recipes` if needed (or use the existing fork).
2. Edit `recipes/nf-metro/meta.yaml` in the fork:
   - Add/update the missing `run:` entries
   - Increment `build: number:` by 1
   - Leave `version:` and `sha256:` at their current values — the autobump
     bot will update those when the PyPI release lands
3. Open a PR against `bioconda/bioconda-recipes` main:
   `Update nf-metro: add <dep> to run requirements`
4. Share the PR URL with the user and note:
   > This PR only changes deps and build number, not the version. When
   > `$NEW_VERSION` lands on PyPI, the bioconda autobump bot will open its
   > own PR to update `version:` and `sha256:`. Because the dep changes are
   > already in, that autobump PR needs no intervention.

If the recipe is already in sync, say so and continue.

## Step 3: Pre-flight checks

Run these three checks and report the results before touching any files.

**origin/main CI must be green.** Do not proceed if the HEAD commit's checks
are red or still in progress:

```bash
gh api repos/seqeralabs/nf-metro/commits/$(git -C ~/projects/nf-metro rev-parse origin/main)/check-runs \
    --jq '.check_runs[] | "\(.conclusion // .status)\t\(.name)"'
```

**Open PRs that would change default output.** List open, non-draft PRs and
check their descriptions for anything that changes default render output or
a default option value (layout defaults, a new default behaviour, and
similar):

```bash
gh pr list --repo seqeralabs/nf-metro --state open --json number,title,isDraft,mergeable
```

For each PR that qualifies, ask the user for an explicit in-or-out decision:
merging one of these right after a release means the very next release
changes default output again, which is worth deciding on purpose rather than
by accident of merge timing.

**Live strict xfails.** Collect every strict xfail currently in the suite,
with its reason and any issue number in that reason:

```bash
grep -rn "pytest.mark.xfail" tests/
```

Keep this list; it feeds the Known issues subsection in Step 6.

## Step 4: Worktree setup

```bash
git -C ~/projects/nf-metro fetch origin main
git -C ~/projects/nf-metro worktree add /tmp/nf-metro-release-$NEW_VERSION \
    -b release/$NEW_VERSION origin/main
```

All subsequent edits happen inside `/tmp/nf-metro-release-$NEW_VERSION`.

## Step 5: Bump the version

**`pyproject.toml`** — the `version = "X.Y.Z"` line under `[project]`.

**`src/nf_metro/__init__.py`** — the `__version__ = "X.Y.Z"` line.

Verify both:

```bash
grep '^version' /tmp/nf-metro-release-$NEW_VERSION/pyproject.toml
grep '__version__' /tmp/nf-metro-release-$NEW_VERSION/src/nf_metro/__init__.py
```

### CI-integration version references

The composite action (`action.yml`) ships from this repo, so it inherits the
release tag automatically. Its *example* refs in the docs are pinned to the
previous version and must be bumped to `$NEW_VERSION` so consumers copy the
current one:

```bash
cd /tmp/nf-metro-release-$NEW_VERSION
OLD_VERSION=${LAST_TAG}
grep -rn "${OLD_VERSION}" docs/automation.mdx README.md action.yml
# Bump the `- uses: seqeralabs/nf-metro@X.Y.Z` ref in docs/automation.mdx and
# the `(e.g. X.Y.Z)` example in the action's `version` input description.
```

The action's `runs:` block needs **no** version edit — with an empty `version`
input it reads the bumped `pyproject.toml` at runtime, so `@$NEW_VERSION`
installs `nf-metro==$NEW_VERSION` automatically. After editing, confirm no
stale refs remain:

```bash
grep -rn "${OLD_VERSION}" docs/automation.mdx README.md action.yml || echo "clean"
```

## Step 6: Draft the release page

Create `/tmp/nf-metro-release-$NEW_VERSION/docs/releases/$NEW_VERSION.md`.

The page is a Starlight content file: the title comes from frontmatter, not
from a heading in the body.

```markdown
---
title: "v$NEW_VERSION"
slug: releases/$NEW_VERSION
---

_<YYYY-MM-DD>_ · [GitHub release](https://github.com/seqeralabs/nf-metro/releases/tag/$NEW_VERSION) · [Diff](https://github.com/seqeralabs/nf-metro/compare/$LAST_TAG...$NEW_VERSION)

<one-sentence summary>

## <Feature or fix heading>

<prose for a user who hasn't read the PRs — what it does, why it matters,
how to use it>

<Metro src="examples/guide/<relevant_example>.mmd" />
```

**Illustration guidance:**

- Prefer the `<Metro>` component over a static image: it renders the map live
  at build time from a `.mmd` in the repo, so the page never goes stale. Its
  options are documented in `docs/contributing.mdx`; `mmd={false}` hides the
  source panel.
- Check what maps exist: `ls ~/projects/nf-metro/examples/`,
  `ls ~/projects/nf-metro/examples/guide/`.
- For a static image instead, use a versioned GitHub Pages URL so readers see
  the exact shipped render:
  `https://seqeralabs.github.io/nf-metro/$NEW_VERSION/assets/renders/<file>.svg`
  These resolve once the GitHub Release is published and the docs deploy runs.
  `docs/assets/renders/` itself is generated by `scripts/build_gallery.py` and
  gitignored, so list it in the main checkout to see what is available.
- For patch releases fixing a visual issue, describe the before/after even
  if you can only show the after state.
- Patch releases with no visual impact (CI fixes, permission fixes) can be
  a single short paragraph with no illustration.

**Known issues subsection:** add a `## Known issues` section listing any
live strict xfail (from the Step 3 grep) that locks a defect in a shipped
example map (one from `examples/*.mmd` that appears in
`scripts/gallery.yaml`). One line per issue, naming the affected example and
the tracking issue number. Derive this list fresh from the Step 3 grep each
time; don't carry forward a list from a previous release page, since the set
of live xfails changes release to release.

Present the draft. Ask: "Does this look right? Any changes before I commit?"
Wait for approval.

## Step 7: Wire the new page into the index and changelog

### Sidebar nav — nothing to do

The docs site is Astro / Starlight under `website/`. Its
`buildReleasesSidebar()` in `website/astro.config.mjs` reads
`docs/releases/*.md*` off disk and builds the `v<major>.<minor>.x` groups
itself, so the new page appears in the sidebar with no config edit.

### releases/index.md

Insert a new row at the **top** of the table (below the header row) in
`/tmp/nf-metro-release-$NEW_VERSION/docs/releases/index.md`, matching the
site-absolute link form the other rows use:

```markdown
| [v$NEW_VERSION](/nf-metro/releases/$NEW_VERSION/) | <YYYY-MM-DD> | <one-line summary> |
```

### CHANGELOG.md

`CHANGELOG.md` carries a condensed Keep-a-Changelog history; the release page is
the full account. At the top of
`/tmp/nf-metro-release-$NEW_VERSION/CHANGELOG.md`:

- Retitle `## [Unreleased]` to `## [$NEW_VERSION] — <YYYY-MM-DD>` if the
  release's entries were accumulated there.
- Otherwise add a `## [$NEW_VERSION] — <YYYY-MM-DD>` section, condensed from the
  release page into `### Added` / `### Changed` / `### Fixed` groups — a few
  bullets each, not the whole page — and separated from the section below it by
  a `---` rule.
- Leave a fresh, empty `## [Unreleased]` at the top for the next cycle.

Headings in this file use an em-dash date separator; match it.

## Step 8: Commit and push

```bash
cd /tmp/nf-metro-release-$NEW_VERSION

git add pyproject.toml \
        src/nf_metro/__init__.py \
        action.yml \
        docs/automation.mdx \
        docs/releases/$NEW_VERSION.md \
        docs/releases/index.md \
        CHANGELOG.md

git commit -m "chore: release $NEW_VERSION"
# No [skip ci] — CI must run on this commit when the PR lands.

git push -u origin release/$NEW_VERSION
```

## Step 9: Open the PR

```bash
gh pr create \
  --repo seqeralabs/nf-metro \
  --title "chore: release $NEW_VERSION" \
  --body "$(cat <<'EOF'
## Summary

- Bumps version to $NEW_VERSION in \`pyproject.toml\` and \`__init__.py\`
- Adds \`docs/releases/$NEW_VERSION.md\` and its \`docs/releases/index.md\` row
- Adds the condensed \`CHANGELOG.md\` section for $NEW_VERSION

<paste the highlights from the release page here>

## After merge

Create the GitHub Release at https://github.com/seqeralabs/nf-metro/releases/new
with tag \`$NEW_VERSION\` to trigger the PyPI publish and versioned docs deploy.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

## Step 10: After the PR merges

Remind the user:

> PR merged. Now create the GitHub Release:
> https://github.com/seqeralabs/nf-metro/releases/new
>
> - **Tag:** `$NEW_VERSION`
> - **Title:** `$NEW_VERSION - <short description>`
> - **Body:** paste the content of `docs/releases/$NEW_VERSION.md`
>   (drop the frontmatter block and the GitHub links line — GitHub generates
>   those itself, and `<Metro>` components will not render there)
>
> Publishing triggers:
> - `publish.yml` → builds and uploads to PyPI
> - `docs.yml` → deploys versioned docs at
>   `https://seqeralabs.github.io/nf-metro/$NEW_VERSION/` and updates
>   the `latest` alias
>
> The bioconda autobump bot will open a PR to `bioconda-recipes` within a
> few hours of the PyPI upload. If the dep check in Step 2 was clean (or
> the dep-update PR was already merged), that autobump PR needs no
> intervention — just approve and merge.

## Step 11: Cleanup

```bash
git -C ~/projects/nf-metro worktree remove /tmp/nf-metro-release-$NEW_VERSION
git -C ~/projects/nf-metro branch -d release/$NEW_VERSION
git -C ~/projects/nf-metro worktree prune
```
