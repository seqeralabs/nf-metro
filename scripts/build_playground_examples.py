#!/usr/bin/env python3
"""Generate the playground's grouped example manifest.

The browser playground is a static page, so it cannot read the repo's example
maps directly. This writes their contents to ``docs/playground/examples.json``
(an ordered list of ``{"label", "entries": [{"name", "mmd"}]}`` groups) for the
"load example" dropdown to fetch.

The set and grouping mirror the gallery / PR render-diff: it reuses
``build_gallery``'s curated, ordered entries and its ``CATEGORY_HEADERS`` so the
dropdown stays in lockstep with what the render diff shows. Run it before
building/serving the playground; the output is git-ignored and regenerated in
CI.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_gallery as gallery  # noqa: E402  (path set above)

OUTPUT = ROOT / "docs" / "playground" / "examples.json"

# Mirrors render_test_fixtures() in build_gallery.py.
TEST_FIXTURE_STEMS = ("multiline_labels", "rnaseq_simple", "genomeassembly_organellar")

_DIVIDER_RE = re.compile(r"^\s*#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
_STEM_RE = re.compile(r'^\s*"([A-Za-z0-9_]+)"\s*,\s*$')


def gallery_categories() -> dict[str, str]:
    """Map each gallery stem to its ``# --- ... ---`` divider in build_gallery.

    ``CATEGORY_HEADERS`` only marks 7 coarse sections, collapsing ~90 entries
    into "Fold Topologies". The source's divider comments carry the finer
    taxonomy the maintainer actually uses, so the dropdown reads those.
    """
    stems = {stem for stem, _dir, _desc in gallery.GALLERY_ENTRIES}
    category: dict[str, str] = {}
    current = "Gallery"
    for line in Path(gallery.__file__).read_text().splitlines():
        divider = _DIVIDER_RE.match(line)
        if divider:
            current = divider.group(1)
            continue
        stem = _STEM_RE.match(line)
        if stem and stem.group(1) in stems:
            category.setdefault(stem.group(1), current)
    return category


def main() -> None:
    groups: list[dict] = []
    by_label: dict[str, list] = {}
    seen: set[str] = set()

    def add(label: str, name: str, path: Path) -> None:
        if name in seen or not path.exists():
            return
        seen.add(name)
        if label not in by_label:
            by_label[label] = []
            groups.append({"label": label, "entries": by_label[label]})
        by_label[label].append({"name": name, "mmd": path.read_text()})

    # Gallery: main examples + topologies, grouped by the source's dividers.
    categories = gallery_categories()
    for stem, source_dir, _description in gallery.GALLERY_ENTRIES:
        add(categories.get(stem, "Gallery"), stem, source_dir / f"{stem}.mmd")

    for path in sorted(gallery.GUIDE_DIR.glob("*.mmd")):
        add("Guide", path.stem, path)

    for stem in TEST_FIXTURE_STEMS:
        add("Test fixtures", stem, gallery.TEST_FIXTURES_DIR / f"{stem}.mmd")

    # Nextflow `-with-dag` inputs are intentionally excluded: they are raw
    # mermaid, not metro maps, and only render via --from-nextflow conversion.

    OUTPUT.write_text(json.dumps(groups, indent=2) + "\n")
    total = sum(len(g["entries"]) for g in groups)
    rel = OUTPUT.relative_to(ROOT)
    print(f"wrote {total} examples in {len(groups)} groups -> {rel}")


if __name__ == "__main__":
    main()
