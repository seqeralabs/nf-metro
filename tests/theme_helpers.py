"""Shared inputs and SVG lookups for the theme tests."""

from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RNASEQ = ROOT / "examples" / "rnaseq_sections.mmd"
SVG_NS = "{http://www.w3.org/2000/svg}"

MMD = textwrap.dedent("""\
    %%metro line: main | Main | #ff0000
    %%metro line: alt | Alt | #0000ff
    graph LR
        a -->|main,alt| b
        b -->|main| c
        b -->|alt| d
""")


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


def classed(root: ET.Element, tag: str, cls: str) -> list[ET.Element]:
    """Every *tag* element carrying class *cls*."""
    return [
        el
        for el in root.iter(f"{SVG_NS}{tag}")
        if cls in (el.get("class") or "").split()
    ]


def station_glyphs(root: ET.Element, tag: str = "rect") -> dict[str, ET.Element]:
    return {
        el.get("data-station-id"): el for el in classed(root, tag, "nf-metro-station")
    }
