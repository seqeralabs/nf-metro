#!/usr/bin/env python3
"""Parse nf-metro's YAML render result (read from stdin) into output paths.

Prints one output path per line to stdout. Exits 1 with a stderr message if
``outputs`` is missing or empty, so a caller can tell a genuinely-empty
result apart from a broken parse: a silent empty match would leave the
GitHub Action's later ``git status --porcelain -- "${rendered[@]}"`` with no
pathspec at all, diffing the whole repository instead of nothing.
"""

import sys

import yaml


def main() -> int:
    try:
        document = yaml.safe_load(sys.stdin.read())
    except (yaml.YAMLError, RecursionError):
        document = None
    outputs = document.get("outputs") if isinstance(document, dict) else None
    if not isinstance(outputs, list):
        outputs = None
    if not outputs:
        print(
            "::error::nf-metro reported no output paths; its stdout contract "
            "may have changed",
            file=sys.stderr,
        )
        return 1
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
