# nf-metro-layout-triage

Reusable triage tool for the failing / xfailing layout-invariant tests in `tests/test_layout_invariants.py`. Produces a single self-contained HTML page where each card pairs the rendered fixture with a red-bbox overlay of the offending element, a plain-English explanation, and bug / not-a-bug / ambiguous radio buttons whose state lives in `localStorage` and exports to JSON.

See `SKILL.md` for the full recipe Claude follows when this skill is triggered.

## One-liner (from an nf-metro checkout or worktree)

```bash
source ~/.local/bin/mm-activate nf-metro
export PYTHONPATH="$PWD/src"

OUT=/tmp/nf-metro-triage          # or a project dir like ~/projects/nf-metro-triage
mkdir -p "$OUT"

python .claude/skills/nf-metro-layout-triage/build_review.py \
    --worktree "$PWD" \
    --output-dir "$OUT"

cd "$OUT" && python -m http.server 8765
# then open http://localhost:8765
```

## What gets produced

```
<output-dir>/
  index.html                       # open this
  fail-list.txt                    # raw pytest output (only when --fail-list not passed)
  renders/
    <fixture>.svg                  # base render per fixture (cached)
    annotated/
      <fixture>__<invariant>.svg   # base + red dashed overlay per row
```

The page itself is fully self-contained (SVGs are inlined as base64) - you can scp `index.html` anywhere and it still works.

## Inputs

The script either runs pytest itself (default) or reads a pre-captured pytest log.

- **Default**: invoke `pytest tests/test_layout_invariants.py -rfX --tb=no -q --no-header` in the worktree, parse the `FAILED` and `XFAIL` lines.
- **`--fail-list <path>`**: skip pytest and parse the given text file. The file just needs to contain pytest-shaped `FAILED tests/test_layout_invariants.py::<name>[<fixture>]` or `XFAIL ...` lines somewhere in it; everything else is ignored.

## Exporting triage state

Inside the HTML page click **Export JSON**. This downloads `xfail-review-tags-<timestamp>.json` of the form:

```json
{
  "<fixture>__<invariant>": {"tag": "bug", "notes": "..."}
}
```

State persists in the browser's `localStorage` under key `nfmetro-xfail-review-v1`; click **Reset localStorage** in the page header to start over.

## Re-running

- Re-running with the same output dir reuses cached fixture renders (under `<output-dir>/renders/`). Delete that directory to force a fresh render.
- If new invariants are added to `tests/test_layout_invariants.py`, the page will still surface them but show a generic explanation block until a tailored finder is added in `build_review.py`.
