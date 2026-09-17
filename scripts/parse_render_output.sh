#!/usr/bin/env bash
# Parse nf-metro's YAML render result (read from stdin) into output paths,
# one per line, on stdout. Exits 1 with a stderr message if no path line is
# found, so a caller can tell a genuinely-empty result apart from a broken
# parse: a silent empty match would leave the Action's later
# `git status --porcelain -- "${rendered[@]}"` with no pathspec at all,
# diffing the whole repository instead of nothing.
#
# A path line is `  - "<path>"`, JSON-quoted (see _print_render_result in
# src/nf_metro/cli.py). python3's json module decodes it exactly, including
# unicode escapes and embedded quotes/backslashes, using only the standard
# library.
set -euo pipefail

python3 -c '
import json
import sys

found = False
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line.startswith("  - "):
        continue
    try:
        value = json.loads(line[4:])
    except json.JSONDecodeError:
        continue
    if isinstance(value, str):
        print(value)
        found = True

if not found:
    print(
        "::error::nf-metro reported no output paths; its stdout contract may have changed",
        file=sys.stderr,
    )
    sys.exit(1)
'
