---
name: pages-ci-doctor
description: Diagnose and un-stick GitHub Pages publishing for nf-metro when a PR render preview or the docs site isn't updating. Use when a `_pr/<N>/` preview 404s or shows stale content, the "render preview is ready" link doesn't work, the docs site didn't refresh after a merge, or you suspect a Pages build stalled. Trigger on phrases like "the preview isn't updating", "the render preview link is broken", "check the pages ci", "poke the pages build", "docs site is stale", "did my preview publish". Covers the legacy-builder stall (the usual culprit), the re-trigger fix, the pr-renders -> pr-render-publish -> build chain, the render content marker, and the CDN caching gotchas that make verification misleading.
allowed-tools: Bash(gh *), Bash(git *), Bash(curl *)
---

# Pages CI doctor

Diagnose why an nf-metro PR render preview (or the docs site) isn't showing up,
and apply the known fix. **The usual culprit is a stalled GitHub Pages build**,
not the workflows.

## Constants

- Repo: `seqeralabs/nf-metro`. Owner/site: `https://seqeralabs.github.io/nf-metro/`.
- Preview URL for PR N: `https://seqeralabs.github.io/nf-metro/_pr/<N>/`.
- Pages source is `build_type: legacy` (branch builder serving `gh-pages`).
  Actions-source (`build_type: workflow`) was tried and reverted: it can't
  publish this repo's accumulate-into-one-branch model (every deploy shares
  `main`'s commit SHA, so new content silently isn't served). **Do not switch
  back to `workflow`.**
- A healthy legacy build completes in **~40-140s**. A build sitting in
  `status: building` for several minutes is **stalled** - the recurring failure
  mode (likely a flaky Pages backend from the `pinin4fjords -> seqeralabs`
  transfer; not fixable from CI).

## How publishing works (the chain)

```
PR push -> "PR render preview" (pr-renders.yml, on: pull_request)
         -> "Publish render preview" (pr-render-publish.yml, on: workflow_run)
              pushes _pr/<N>/ to the gh-pages branch (git, retried)
         -> GitHub legacy Pages build (async, per gh-pages push)  <- STALLS HERE
         -> live at _pr/<N>/
```

Merges to `main` go through "Deploy docs" (docs.yml) into `gh-pages`, same build
step. A push to `gh-pages` only updates the *branch*; the *build* is what makes
it live.

## Verification gotcha - read before curling

GitHub Pages/Fastly caches responses **including 404s**, and `?query`
cache-busting is unreliable (the cache key sometimes ignores the query string).
So a `200`/`404` you see may be stale.

- To read **true origin**, request a path you have **not** requested this
  session. The cleanest: the **no-trailing-slash** form `_pr/<N>` -> a `301` to
  `_pr/<N>/` means the dir exists at origin; a `404` means it doesn't.
- Trust `x-cache: MISS` + `age: 0`. Distrust `x-cache: HIT` (stale).
- The diff page carries `<meta name="nf-metro-render-run" content="<run-id>">`.
  Content is genuinely current when that marker equals the `pr-renders` run id
  that produced it (the publisher gates the "ready" comment on this).

## Step 1 - identify the PR and its head

```bash
PR=<number>
gh pr view $PR --repo seqeralabs/nf-metro --json headRefName,headRefOid,state \
  --jq '{branch:.headRefName, head:.headRefOid[:9], state}'
```

## Step 2 - did the render chain run?

```bash
BR=<branch-from-step-1>
gh run list --repo seqeralabs/nf-metro --branch "$BR" --limit 5 \
  --json name,event,status,conclusion,headSha,createdAt \
  --jq '.[] | "[\(.name)] \(.event) \(.status)/\(.conclusion) \(.headSha[:9]) \(.createdAt)"'
gh run list --repo seqeralabs/nf-metro --workflow pr-render-publish.yml --limit 5 \
  --json databaseId,status,conclusion,createdAt \
  --jq '.[] | "\(.databaseId) \(.status)/\(.conclusion) \(.createdAt)"'
```

- No "PR render preview" run on the head SHA -> the push didn't trigger it.
  Check the last commit for a `[skip ci]` marker (it skips `pull_request` too).
- "PR render preview" ran but no matching "Publish render preview" -> the
  `workflow_run` chain didn't fire, or the render found no changes.

## Step 3 - did the content reach the branch?

```bash
git fetch origin gh-pages --quiet
git log origin/gh-pages -1 --format='%cI %s' -- _pr/$PR/    # when _pr/<N> last changed
git ls-tree origin/gh-pages _pr/$PR/ --name-only | head     # index.html present?
```

If `_pr/<N>/index.html` is on the branch but the URL is stale, the branch is
fine and the **build** is the problem -> Step 4.

## Step 4 - is the build stalled? (the usual answer)

```bash
gh api repos/seqeralabs/nf-metro/pages/builds/latest \
  --jq '"status=\(.status) created=\(.created_at) commit=\(.commit[:9]) dur=\(.duration) err=\(.error.message)"'
gh api "repos/seqeralabs/nf-metro/pages/builds?per_page=5" \
  --jq '.[] | "\(.created_at) \(.status) \(.commit[:9]) dur=\(.duration)"'
```

- `status: building` for more than ~3 min (compare against the ~40-140s of past
  `built` entries) = **stalled**.
- `status: errored` = failed.
- Either way -> Step 5.

## Step 5 - poke it (the fix)

Re-trigger the branch build, then poll to completion:

```bash
gh api -X POST repos/seqeralabs/nf-metro/pages/builds --jq '.status'
for i in $(seq 1 15); do
  st=$(gh api repos/seqeralabs/nf-metro/pages/builds/latest --jq '.status')
  echo "$(date -u +%H:%M:%SZ) $st"
  [ "$st" = "built" ] && break
  [ "$st" = "errored" ] && { echo "errored"; break; }
  sleep 20
done
```

A single re-trigger usually clears it (builds in ~40-140s). If the re-triggered
build **also** stalls, re-trigger again - it can take two. If it keeps stalling
across several tries, it's a live GitHub incident (check
https://www.githubstatus.com/); wait it out, the branch content is safe.

## Step 6 - confirm it's genuinely live

Use a true-origin probe (Step 0 gotcha):

```bash
# dir exists at origin? (never-requested no-slash form -> 301 if live)
curl -sS -o /dev/null -w "_pr/$PR -> HTTP %{http_code} loc=%header{location} x-cache=%header{x-cache}\n" \
  "https://seqeralabs.github.io/nf-metro/_pr/$PR"
# and the freshest content, checking the marker matches the pr-renders run id
body=$(curl -sS "https://seqeralabs.github.io/nf-metro/_pr/$PR/index.html?cb=$(date +%s%N)")
printf '%s' "$body" | grep -oiE "Generated[^<]*|[0-9]+ changed out of [0-9]+"
printf '%s' "$body" | grep -o '<meta name="nf-metro-render-run" content="[^"]*">'
```

Compare the marker's run id to the latest "PR render preview" run id from Step 2;
equal means the served page is the current render.

## Where's the break? (quick table)

| Symptom | Likely cause | Action |
| --- | --- | --- |
| No "PR render preview" run | push skipped CI, or not a PR to `main` | re-push without `[skip ci]` |
| Render ran, no publish run | `workflow_run` miss / no render changes | check publish run logs; a no-change render is expected to skip publishing |
| `_pr/<N>` on branch, URL stale/404 | **build stalled** | Step 5 re-trigger |
| Build `built`, URL still stale | CDN negative cache or old marker | true-origin probe (no-slash / marker); wait out the 404 cache TTL |
| Build keeps stalling across retries | GitHub Pages incident | check githubstatus.com, wait |

## Docs site (not a PR preview)

Same diagnosis, minus the PR steps: after a merge to `main`, "Deploy docs" pushes
to `gh-pages`; if the site didn't refresh, it's Step 4/5 (build stalled -> poke).
