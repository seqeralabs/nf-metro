#!/usr/bin/env python3
"""Generate the playground's example manifest from ``examples/*.mmd``.

The browser playground is a static page, so it cannot read the repo's
``examples/`` directory directly. This writes their contents to
``docs/playground/examples.json`` (a list of ``{"name", "mmd"}``) for the
"load example" dropdown to fetch. Run it before building/serving the
playground; the output is git-ignored and regenerated in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples"
OUTPUT = ROOT / "docs" / "playground" / "examples.json"


def main() -> None:
    entries = [
        {"name": path.stem, "mmd": path.read_text()}
        for path in sorted(EXAMPLES_DIR.glob("*.mmd"))
    ]
    OUTPUT.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"wrote {len(entries)} examples -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
