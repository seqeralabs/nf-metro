"""Print the topology fixture catalogue: all .mmd files in examples/topologies/,
their %%metro title, and whether they are already named in the README.

Usage:
    python scripts/list_topology_fixtures.py

The script compares fixtures on disk against names mentioned anywhere in
examples/topologies/README.md and prints a count summary.  Pipe to a file or
copy the output into the README's "Regression Catalogue" section.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOPOLOGIES_DIR = Path(__file__).parent.parent / "examples" / "topologies"
README_PATH = TOPOLOGIES_DIR / "README.md"


def _title(mmd: Path) -> str:
    for line in mmd.read_text().splitlines():
        m = re.match(r"%%metro\s+title:\s*(.*)", line)
        if m:
            return m.group(1).strip()
    return mmd.stem


def main() -> None:
    fixtures = sorted(TOPOLOGIES_DIR.glob("*.mmd"))
    readme_text = README_PATH.read_text()
    documented = {
        name
        for name in (f.stem for f in fixtures)
        if name in readme_text
    }
    undocumented = [f for f in fixtures if f.stem not in documented]

    print(f"Total fixtures : {len(fixtures)}")
    print(f"In README      : {len(documented)}")
    print(f"Not in README  : {len(undocumented)}")
    print()

    if undocumented:
        print("Undocumented fixtures:")
        for f in undocumented:
            print(f"  {f.name:55s}  {_title(f)}")
    else:
        print("All fixtures are mentioned in the README.")


if __name__ == "__main__":
    sys.exit(main())
