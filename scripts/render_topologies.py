#!/usr/bin/env python3
"""Batch render all .mmd files in the repository to SVG and PNG.

Usage:
    python scripts/render_topologies.py
    python scripts/render_topologies.py --output-dir /tmp/my_renders
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from nf_metro.convert import convert_nextflow_dag  # noqa: E402
from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402
from nf_metro.render.svg import render_svg  # noqa: E402
from nf_metro.themes import THEMES  # noqa: E402

NEXTFLOW_DIR = project_root / "tests" / "fixtures" / "nextflow"


def _collect_mmd_files() -> list[Path]:
    """Collect all .mmd files in the repo, excluding .git.

    When two files have identical content, only the first is kept
    (sorted order means examples/ wins over tests/fixtures/).
    """
    all_paths = sorted(p for p in project_root.rglob("*.mmd") if ".git" not in p.parts)
    seen_hashes: dict[str, Path] = {}
    result: list[Path] = []
    for p in all_paths:
        content = p.read_text()
        h = hashlib.sha256(content.encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes[h] = p
        result.append(p)
    return result


def render_file(
    mmd_path: Path,
    output_dir: Path,
    *,
    debug: bool = False,
    straight_diamonds: bool = False,
) -> tuple[str, list[str]]:
    """Parse, layout, and render a .mmd file to SVG (and optionally PNG).

    Returns (name, list_of_issues).  The output filename is derived from
    the path relative to the project root so files in different directories
    don't collide.
    """
    rel = mmd_path.relative_to(project_root)
    name = str(rel).replace("/", "_").removesuffix(".mmd")
    is_nextflow = NEXTFLOW_DIR in mmd_path.parents or mmd_path.parent == NEXTFLOW_DIR
    issues: list[str] = []

    try:
        text = mmd_path.read_text()
        if is_nextflow:
            text = convert_nextflow_dag(text)
        graph = parse_metro_mermaid(text)
    except Exception as e:
        return name, [f"PARSE ERROR: {e}"]

    if straight_diamonds:
        graph.diamond_style = "straight"

    try:
        compute_layout(graph)
    except Exception as e:
        return name, [f"LAYOUT ERROR: {e}"]

    theme_name = graph.style if graph.style in THEMES else "nfcore"
    theme = THEMES[theme_name]

    try:
        # chrome_css=False bakes concrete colors so the cairosvg PNG step below
        # works (cairosvg cannot parse the var() chrome custom properties).
        svg_str = render_svg(graph, theme, debug=debug, chrome_css=False)
    except Exception as e:
        return name, [f"RENDER ERROR: {e}"]

    svg_path = output_dir / f"{name}.svg"
    svg_path.write_text(svg_str)

    # Try PNG conversion via cairosvg (optional)
    try:
        import cairosvg

        png_path = output_dir / f"{name}.png"
        cairosvg.svg2png(bytestring=svg_str.encode(), write_to=str(png_path), scale=2)
    except ImportError:
        issues.append("cairosvg not available, skipping PNG")
    except Exception as e:
        issues.append(f"PNG conversion error: {e}")

    return name, issues


def main():
    parser = argparse.ArgumentParser(
        description="Batch render all .mmd files in the repo",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: unique temp dir)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug overlay",
    )
    parser.add_argument(
        "--straight-diamonds",
        action="store_true",
        help="Keep top branch of diamond fork-joins on the main track",
    )
    args = parser.parse_args()

    if args.output_dir is not None:
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="nf_metro_renders_"))

    all_files = _collect_mmd_files()
    print(f"Rendering {len(all_files)} files to {output_dir}/")
    if args.debug:
        print("Debug overlay: ON")
    if args.straight_diamonds:
        print("Straight diamonds: ON")
    print()

    # Dry-run to get output names for alignment
    max_name_len = max(
        len(str(f.relative_to(project_root)).replace("/", "_").removesuffix(".mmd"))
        for f in all_files
    )
    any_errors = False

    for mmd_path in all_files:
        name, issues = render_file(
            mmd_path,
            output_dir,
            debug=args.debug,
            straight_diamonds=args.straight_diamonds,
        )
        status = "OK" if not issues else "ISSUES"
        if any("ERROR" in i for i in issues):
            status = "FAIL"
            any_errors = True

        print(f"  {name:<{max_name_len}}  [{status}]")
        for issue in issues:
            print(f"    - {issue}")

    print(f"\nOutputs in: {output_dir}/")

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
