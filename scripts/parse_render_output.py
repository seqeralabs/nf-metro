#!/usr/bin/env python3
"""Parse nf-metro's YAML render result (read from stdin) into output paths.

Prints one output path per line to stdout. Exits 1 with a stderr message if
no path line is found, so a caller can tell a genuinely-empty result apart
from a broken parse: a silent empty match would leave the GitHub Action's
later ``git status --porcelain -- "${rendered[@]}"`` with no pathspec at
all, diffing the whole repository instead of nothing.

A path line is ``  - <path>`` under the ``outputs:`` key (see
``_print_render_result`` in ``src/nf_metro/cli.py``). The emitter quotes a
scalar only where a bare word would not read back unchanged, so both forms
are accepted; a quoted one is JSON-decoded exactly, including unicode
escapes and embedded quotes/backslashes, using only the standard library.
The ``inputs:`` list is skipped: those are sources, not results.
"""

import json
import sys


def main() -> int:
    found = False
    in_outputs = False
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line.startswith((" ", "-")):
            # A top-level key ends the previous block; only outputs' items
            # are paths to report, never the inputs listed alongside them.
            in_outputs = line.startswith("outputs:")
            continue
        if not in_outputs or not line.startswith("  - "):
            continue
        item = line[4:]
        if item.startswith('"'):
            try:
                value = json.loads(item)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, str):
                continue
        else:
            # A plain YAML scalar: written unquoted where quoting would not
            # change how it reads back.
            value = item
        print(value)
        found = True

    if not found:
        print(
            "::error::nf-metro reported no output paths; its stdout contract "
            "may have changed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
