#!/usr/bin/env bash
# Render each changed stem at the base and candidate SHAs and rasterise both, so
# a reviewer compares like with like. Reports every stem it could not render
# rather than skipping it: a stem that renders on base and fails on the candidate
# is the regression.
set -euo pipefail

die() { echo "RENDER FAILED: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
usage: render_pairs.sh --base SHA --candidate SHA [--art DIR] [--stems FILE] [--repo DIR]
       render_pairs.sh --self-test

Reads stems from $ART/stems.txt (or --stems); with no stem list it reviews the
whole corpus. Source paths come from corpus_map.py, which reads gallery.yaml.
Writes $ART/{base,cand}-<stem>.png and prints a reconciliation summary.
USAGE
}

BASE=""
CANDIDATE=""
ART=""
STEMS=""
REPO="$HOME/projects/nf-metro"
SELF_TEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE=$2; shift 2 ;;
    --candidate) CANDIDATE=$2; shift 2 ;;
    --art) ART=$2; shift 2 ;;
    --stems) STEMS=$2; shift 2 ;;
    --repo) REPO=$2; shift 2 ;;
    --self-test) SELF_TEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument $1" ;;
  esac
done

# The corpus map is authoritative: gallery.yaml already states every output name
# and its source, so resolving by basename search guesses at known data and gets
# duplicate basenames, regex metacharacters and output-differs-from-id wrong.
corpus_map() {
  python3 "$(dirname "$0")/corpus_map.py" "$REPO"
}

self_test() {
  local tmp; tmp=$(mktemp -d); local rc=0
  check() { [ "$2" = "$3" ] && echo "  ok    $1" || { echo "  FAIL  $1: got $2 want $3"; rc=1; }; }

  # Source resolution is delegated to corpus_map.py, which has its own
  # --self-test against the real gallery.yaml. What belongs here is the lookup:
  # exact match on the output name, so no pattern metacharacter can mis-resolve.
  printf 'alpha\texamples/alpha.mmd\nnf_x\texamples/y.mmd\na.b\texamples/a.b.mmd\n' > "$tmp/corpus.tsv"
  look() { awk -F'\t' -v s="$1" '$1 == s { print $2; exit }' "$tmp/corpus.tsv"; }
  check "exact lookup" "$(look alpha)" "examples/alpha.mmd"
  check "output name differing from id" "$(look nf_x)" "examples/y.mmd"
  check "a dot is literal, not a wildcard" "$(look 'a.b')" "examples/a.b.mmd"
  check "unknown stem yields nothing" "$(look nope)" ""

  rm -rf "$tmp"
  [ $rc -eq 0 ] && echo "render_pairs.sh self-test OK" || echo "render_pairs.sh self-test FAILED"
  return $rc
}

[ "$SELF_TEST" = 1 ] && { self_test; exit $?; }
[ -n "$BASE" ] && [ -n "$CANDIDATE" ] || { usage; die "missing argument"; }
: "${ART:=/tmp/nf-metro-visual-$CANDIDATE}"
: "${STEMS:=$ART/stems.txt}"
mkdir -p "$ART" || die "cannot create $ART"

corpus_map > "$ART/corpus.tsv" || die "corpus_map.py failed"
if [ ! -s "$STEMS" ]; then
  echo "no stem list; reviewing the whole corpus" >&2
  cut -f1 "$ART/corpus.tsv" > "$STEMS"
fi

unresolved=0 failed=0 rendered=0
for side in base cand; do
  case $side in base) sha=$BASE ;; cand) sha=$CANDIDATE ;; esac
  wt="$ART/wt-$sha"
  git -C "$REPO" worktree add --detach "$wt" "$sha" >/dev/null || die "worktree add $side"
  while read -r stem; do
    [ -n "$stem" ] || continue
    # Exact lookup on the output name; no pattern matching, so a stem containing
    # regex metacharacters resolves correctly.
    rel=$(awk -F'\t' -v s="$stem" '$1 == s { print $2; exit }' "$ART/corpus.tsv")
    if [ -z "$rel" ]; then
      echo "UNRESOLVED: $stem" >&2; unresolved=$((unresolved + 1)); continue
    fi
    if ! PYTHONPATH="$wt/src" python3 -m nf_metro render "$wt/$rel" \
           -o "$ART/$side-$stem.svg" -o "$ART/$side-$stem.png" \
           2>"$ART/$side-$stem.err"; then
      echo "RENDER-FAILED($side): $stem" >&2; failed=$((failed + 1)); continue
    fi
    rendered=$((rendered + 1))
  done < "$STEMS"
  git -C "$REPO" worktree remove --force "$wt" >/dev/null || true
done

echo "stems=$(wc -l < "$STEMS" | tr -d ' ') rendered=$rendered unresolved=$unresolved render_failed=$failed"
echo "Read the base-<stem>.png / cand-<stem>.png pairs in $ART."
echo "A stem that renders on base and fails on cand IS the regression; failures on"
echo "both sides pre-date this PR - diff the .err files, a new reason is a finding."
