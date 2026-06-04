#!/usr/bin/env python3
"""Build the docs gallery: render .mmd examples to SVG and generate gallery/index.md.

Usage:
    python scripts/build_gallery.py
    python scripts/build_gallery.py --debug   # include debug overlay
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from nf_metro.convert import convert_nextflow_dag  # noqa: E402
from nf_metro.layout.engine import compute_layout  # noqa: E402
from nf_metro.parser.mermaid import parse_metro_mermaid  # noqa: E402
from nf_metro.render.svg import render_svg  # noqa: E402
from nf_metro.themes import THEMES  # noqa: E402

DEBUG_RENDERS = "--debug" in sys.argv

EXAMPLES_DIR = project_root / "examples"
NEXTFLOW_FIXTURES_DIR = project_root / "tests" / "fixtures" / "nextflow"
TEST_FIXTURES_DIR = project_root / "tests" / "fixtures"
TOPOLOGIES_DIR = project_root / "examples" / "topologies"
GUIDE_DIR = project_root / "examples" / "guide"
GALLERY_DIR = project_root / "docs" / "gallery"
PIPELINES_DIR = project_root / "docs" / "pipelines"
RENDERS_DIR = project_root / "docs" / "assets" / "renders"

# Ordered list of examples. Each entry is (filename_stem, source_dir, description).
# Main examples first, then topologies grouped by category.
GALLERY_ENTRIES: list[tuple[str, Path, str]] = [
    # --- Main examples ---
    (
        "simple_pipeline",
        EXAMPLES_DIR,
        "Minimal two-line pipeline with no sections.",
    ),
    (
        "rnaseq_auto",
        EXAMPLES_DIR,
        "Demonstrates fully auto-inferred layout: no `%%metro grid:` directives "
        "needed. See [nf-core Pipelines](../pipelines/index.md) for the full gallery.",
    ),
    (
        "rnaseq_sections",
        EXAMPLES_DIR,
        "Same pipeline with manual `%%metro grid:` overrides and file markers, "
        "showing how explicit directives can fine-tune placement.",
    ),
    (
        "genomic_pipeline",
        EXAMPLES_DIR,
        "Multi-section genomic variant-calling pipeline: same-direction sections "
        "stacked in one column (serpentine carriage-return) plus a multi-row QC "
        "collector fan-in descending a shared inter-column corridor.",
    ),
    (
        "longread_variant_calling",
        EXAMPLES_DIR,
        "Dense long-read variant-calling pipeline (six lines, nine sections): "
        "exercises inter-section routing around non-connecting section boxes, "
        "cross-row feeds via the inter-row gap, same-line bundle coincidence, "
        "and a same-colour crossover bridge.",
    ),
    (
        "differentialabundance",
        EXAMPLES_DIR,
        "nf-core/differentialabundance with four input lines, off-track "
        "gene-set inputs, and a bypass-heavy reporting row. Uses "
        "`%%metro center_ports: true`.",
    ),
    (
        "differentialabundance_default",
        EXAMPLES_DIR,
        "Same nf-core/differentialabundance map at default (uncentered) "
        "layout — useful for spotting regressions that only show with "
        "default port placement.",
    ),
    (
        "legend_logo_placement",
        EXAMPLES_DIR,
        "Demonstrates positioning the bundled legend+logo block: `%%metro "
        "legend: br | canvas` pins it to the empty lower-right canvas corner and "
        "`%%metro logo_scale:` enlarges the embedded logo. The directive also "
        "supports `| dx,dy` offsets and absolute `x,y` placement; the block "
        "auto-avoids sections and routes. The QC line shows a downward "
        "cross-column feeder dropping straight into its consumer section.",
    ),
    (
        "tb_file_termini",
        EXAMPLES_DIR,
        "A `%%metro direction: TB` reporting section whose file outputs are "
        "line termini. Regression fixture for #254 (terminus file icons now "
        "orient to a vertical flow, with the connector entering from the top).",
    ),
    (
        "genomeassembly_staggered",
        EXAMPLES_DIR,
        "sanger-tol/genomeassembly with explicit `%%metro grid:` directives "
        "stacking each section in its own grid row. Regression fixture for "
        "#250 (cross-column junction routes were going backward in X).",
    ),
    (
        "rail_mode",
        EXAMPLES_DIR,
        "Opt-in `%%metro rail_mode: true`: lines run as fixed parallel "
        "horizontal rails and a station shared by several lines draws as one "
        "vertical pill spanning those rails (the nf-core/sarek "
        '"Example analysis pathways" subway idiom). Line-exclusive callers '
        "sit on their own rail; the rails never converge to a point.",
    ),
    (
        "rail_section",
        EXAMPLES_DIR,
        "Per-section rail mode (`%%metro rail_section: <id>`): a connected "
        "trunk of three normal sections renders with the usual converging "
        "metro routing, while a separate disconnected panel is laid out as "
        "parallel rails with spanning pills. Mixed normal + rail layout in one "
        "map.",
    ),
    # --- Simple topologies ---
    (
        "single_section",
        TOPOLOGIES_DIR,
        "One section, one line. The simplest possible case.",
    ),
    (
        "deep_linear",
        TOPOLOGIES_DIR,
        "Seven sections in a straight chain. Exercises the grid fold threshold.",
    ),
    (
        "parallel_independent",
        TOPOLOGIES_DIR,
        "Two disconnected pipelines stacked vertically.",
    ),
    # --- Fan-out and fan-in ---
    (
        "wide_fan_out",
        TOPOLOGIES_DIR,
        "One source fanning out to four target sections.",
    ),
    (
        "wide_fan_in",
        TOPOLOGIES_DIR,
        "Four sources converging into one target section.",
    ),
    (
        "section_diamond",
        TOPOLOGIES_DIR,
        "Section-level fork-join: fan-out then reconverge.",
    ),
    (
        "terminal_symmetric_fan",
        TOPOLOGIES_DIR,
        "A terminal section whose entry fans into equal-rank sinks; the "
        "fan stays symmetric about the entry port (regression lock for "
        "top-anchored terminal fans).",
    ),
    (
        "trunk_through_fan",
        TOPOLOGIES_DIR,
        "A pass-through section: the trunk runs straight through a "
        "symmetric fan-and-reconverge, exit on the merge row (regression "
        "lock for detached reconvergence exits).",
    ),
    (
        "wide_label_fan",
        TOPOLOGIES_DIR,
        "A two-column fan whose station labels are wider than the column "
        "pitch; the engine wraps the labels and widens spacing so they "
        "don't collide (issue #405).",
    ),
    # --- Branching and multipath ---
    (
        "asymmetric_tree",
        TOPOLOGIES_DIR,
        "One root branching into three paths of different depths.",
    ),
    (
        "complex_multipath",
        TOPOLOGIES_DIR,
        "Four lines taking different routes through six sections.",
    ),
    # --- Multi-line bundles ---
    (
        "multi_line_bundle",
        TOPOLOGIES_DIR,
        "Six lines travelling through the same three-section chain.",
    ),
    (
        "mixed_port_sides",
        TOPOLOGIES_DIR,
        "A section with both RIGHT and BOTTOM exits.",
    ),
    # --- Realistic pipelines ---
    (
        "rnaseq_lite",
        TOPOLOGIES_DIR,
        "Simplified RNA-seq pipeline with three analysis routes.",
    ),
    (
        "variant_calling",
        TOPOLOGIES_DIR,
        "Variant calling pipeline with four lines sharing alignment.",
    ),
    # --- Fold topologies ---
    (
        "fold_fan_across",
        TOPOLOGIES_DIR,
        "Three lines diverge, converge at a fold, then continue on the return row.",
    ),
    (
        "fold_double",
        TOPOLOGIES_DIR,
        "Ten-section linear pipeline with two fold points (serpentine layout).",
    ),
    (
        "fold_stacked_branch",
        TOPOLOGIES_DIR,
        "Stacked analysis sections feeding through a fold into branching targets.",
    ),
    (
        "stacked_lr_serpentine",
        TOPOLOGIES_DIR,
        "Same-direction sections stacked in one grid column, chained via short "
        "vertical drops on alternating sides (serpentine), no wrap-around.",
    ),
    # --- Offset and bypass ---
    (
        "mismatched_tracks",
        TOPOLOGIES_DIR,
        "Lines with mismatched track counts at shared stations.",
    ),
    (
        "upward_bypass",
        TOPOLOGIES_DIR,
        "Tall section bypass where the trunk is above the source (upward gap1).",
    ),
    (
        "off_track_convergence",
        TOPOLOGIES_DIR,
        "Multiple off-track file inputs converging on a single consumer. "
        "The trunk stays horizontal while the inputs stack above the consumer column.",
    ),
    (
        "around_section_below",
        TOPOLOGIES_DIR,
        "Cross-row route to a LEFT-entry target where the natural inter-row "
        "channel would cut through an intervening section's bbox. Exercises "
        "`_route_around_section_below` (collector-fan-in geometry).",
    ),
    (
        "inter_row_wrap_clearance",
        TOPOLOGIES_DIR,
        "A right-exit bundle wrapping down to a left-entry in the row below. "
        "The horizontal run sits centred in the inter-row gap, clear of both "
        "the section above and the next row's header.",
    ),
    # --- #484 regression isolation ---
    (
        "route_around_intervening",
        TOPOLOGIES_DIR,
        "A line skips a middle section: it routes around the intervening "
        "section's bbox rather than slicing through it.",
    ),
    (
        "self_crossing_bridge",
        TOPOLOGIES_DIR,
        "One colour whose vertical bus crosses its own independent horizontal "
        "connector earns a bridge gap (same-colour crossover, not a fan).",
    ),
    (
        "convergence_stacked_sink",
        TOPOLOGIES_DIR,
        "A leaf sink that would otherwise sit alone in the spine band migrates "
        "into the convergence return row (no grid-cell collision).",
    ),
    (
        "cross_row_gap_wrap",
        TOPOLOGIES_DIR,
        "A cross-row feed runs its horizontal in the inter-row gap and drops "
        "straight in, rather than diving under the return row counter to its "
        "flow.",
    ),
]

# Category headers inserted before specific entries
CATEGORY_HEADERS: dict[str, str] = {
    "simple_pipeline": "Main Examples",
    "single_section": "Simple Topologies",
    "wide_fan_out": "Fan-out and Fan-in",
    "asymmetric_tree": "Branching and Multipath",
    "multi_line_bundle": "Multi-line Bundles",
    "rnaseq_lite": "Realistic Pipelines",
    "fold_fan_across": "Fold Topologies",
}


# Ordered list of nf-core pipeline examples.
# Each entry is (filename_stem, display_name, repo_url, description).
PIPELINE_ENTRIES: list[tuple[str, str, str, str]] = [
    (
        "rnaseq_auto",
        "nf-core/rnaseq",
        "https://github.com/nf-core/rnaseq",
        "RNA-seq analysis with multiple aligner and quantification routes "
        "(STAR/RSEM, STAR/Salmon, HISAT2, Salmon pseudo-alignment, Kallisto).",
    ),
    (
        "epitopeprediction",
        "nf-core/epitopeprediction",
        "https://github.com/nf-core/epitopeprediction",
        "MHC binding prediction from VCF, protein FASTA, or peptide TSV inputs "
        "through five prediction tools.",
    ),
    (
        "hlatyping",
        "nf-core/hlatyping",
        "https://github.com/nf-core/hlatyping",
        "HLA typing from FASTQ or BAM inputs via OptiType and HLA-HD.",
    ),
    (
        "variantprioritization",
        "nf-core/variantprioritization",
        "https://github.com/nf-core/variantprioritization",
        "Somatic and germline variant prioritization using PCGR and CPSR.",
    ),
    (
        "variantbenchmarking",
        "nf-core/variantbenchmarking",
        "https://github.com/nf-core/variantbenchmarking",
        "Benchmarking of variant callers against truth sets with "
        "Truvari, hap.py, RTGtools, and more.",
    ),
    (
        "genomeassembly",
        "sanger-tol/genomeassembly",
        "https://github.com/sanger-tol/genomeassembly",
        "Genome assembly from long reads and Hi-C data through "
        "purging, polishing, scaffolding, and QC.",
    ),
]

# Manifest mapping SVG filename -> section for the render diff page.
# Populated by each render function, written to RENDERS_DIR/manifest.json.
_manifest: dict[str, str] = {}


def render_mmd(mmd_path: Path, svg_path: Path, *, debug: bool = DEBUG_RENDERS) -> None:
    """Parse, layout, and render a .mmd file to SVG."""
    text = mmd_path.read_text()
    graph = parse_metro_mermaid(text)
    compute_layout(graph)
    theme_name = graph.style if graph.style in THEMES else "nfcore"
    theme = THEMES[theme_name]
    svg_str = render_svg(graph, theme, debug=debug)
    svg_path.write_text(svg_str)


def clean_name(stem: str) -> str:
    """Convert filename stem to a display-friendly heading."""
    return stem.replace("_", " ").title()


def render_guide_examples() -> None:
    """Render all guide examples to docs/assets/renders/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Guide Examples"
    print("Guide examples:")
    for mmd_path in sorted(GUIDE_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"{mmd_path.stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {mmd_path.stem}: OK")
        except Exception as e:
            print(f"  {mmd_path.stem}: FAIL - {e}")

    # Top-level examples referenced directly from the guide
    for stem in ("rnaseq_auto", "variantbenchmarking", "variantbenchmarking_auto"):
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")

    # Debug overlay render for the guide
    debug_src = EXAMPLES_DIR / "rnaseq_auto.mmd"
    debug_svg = RENDERS_DIR / "rnaseq_auto_debug.svg"
    if debug_src.exists():
        try:
            text = debug_src.read_text()
            graph = parse_metro_mermaid(text)
            compute_layout(graph)
            theme_name = graph.style if graph.style in THEMES else "nfcore"
            theme = THEMES[theme_name]
            svg_str = render_svg(graph, theme, debug=True)
            debug_svg.write_text(svg_str)
            _manifest[debug_svg.name] = section
            print("  rnaseq_auto_debug: OK")
        except Exception as e:
            print(f"  rnaseq_auto_debug: FAIL - {e}")

    print()


def build_gallery() -> None:
    """Generate docs/gallery/index.md and docs/assets/renders/*.svg."""
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "# Gallery",
        "",
        "Rendered examples covering a range of layout patterns. "
        "Click any heading in the right-hand table of contents to jump to an example.",
        "",
    ]

    current_category = "Gallery"
    for stem, source_dir, description in GALLERY_ENTRIES:
        mmd_path = source_dir / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        # Category header
        if stem in CATEGORY_HEADERS:
            current_category = CATEGORY_HEADERS[stem]
            lines.append("---\n")
            lines.append(f"## {current_category}\n")

        # Render SVG
        try:
            render_mmd(mmd_path, svg_path)
            status = "OK"
        except Exception as e:
            status = f"FAIL: {e}"
            print(f"  {stem}: {status}")
            continue

        _manifest[svg_path.name] = current_category
        print(f"  {stem}: {status}")

        # Determine the CLI command path
        if source_dir == EXAMPLES_DIR:
            cli_path = f"examples/{stem}.mmd"
        else:
            cli_path = f"examples/topologies/{stem}.mmd"

        heading = clean_name(stem)
        mmd_source = mmd_path.read_text()

        lines.append(f"### {heading}\n")
        lines.append(f"{description}\n")
        lines.append("**CLI command:**\n")
        lines.append(f"```bash\nnf-metro render {cli_path} -o {stem}.svg\n```\n")
        lines.append('??? note "Mermaid source"\n')
        lines.append("    ```text")
        for src_line in mmd_source.rstrip().split("\n"):
            lines.append(f"    {src_line}")
        lines.append("    ```\n")
        lines.append("**Rendered output:**\n")
        lines.append(f"![{heading}](../assets/renders/{stem}.svg)\n")

    gallery_md = "\n".join(lines)
    gallery_path = GALLERY_DIR / "index.md"
    gallery_path.write_text(gallery_md)
    print(f"\nGallery written to {gallery_path}")
    print(f"SVG renders in {RENDERS_DIR}")


def render_nextflow_examples() -> None:
    """Render Nextflow DAG fixtures and hand-tuned example to docs/assets/renders/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Nextflow Conversions"
    print("Nextflow examples:")

    # Auto-converted renders from Nextflow DAG fixtures
    for mmd_path in sorted(NEXTFLOW_FIXTURES_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"nf_{mmd_path.stem}.svg"
        try:
            text = mmd_path.read_text()
            converted = convert_nextflow_dag(text)
            graph = parse_metro_mermaid(converted)
            compute_layout(graph)
            theme = THEMES[graph.style if graph.style in THEMES else "nfcore"]
            svg_str = render_svg(graph, theme, debug=DEBUG_RENDERS)
            svg_path.write_text(svg_str)
            _manifest[svg_path.name] = section
            print(f"  nf_{mmd_path.stem}: OK")
        except Exception as e:
            print(f"  nf_{mmd_path.stem}: FAIL - {e}")

    # Hand-tuned variant calling example (without file icons)
    tuned_path = EXAMPLES_DIR / "variant_calling.mmd"
    if tuned_path.exists():
        svg_path = RENDERS_DIR / "nf_variant_calling_tuned.svg"
        try:
            render_mmd(tuned_path, svg_path)
            _manifest[svg_path.name] = section
            print("  nf_variant_calling_tuned: OK")
        except Exception as e:
            print(f"  nf_variant_calling_tuned: FAIL - {e}")

    # Hand-tuned variant calling with file icons
    tuned_icons_path = EXAMPLES_DIR / "variant_calling_tuned.mmd"
    if tuned_icons_path.exists():
        svg_path = RENDERS_DIR / "nf_variant_calling_tuned_icons.svg"
        try:
            render_mmd(tuned_icons_path, svg_path)
            _manifest[svg_path.name] = section
            print("  nf_variant_calling_tuned_icons: OK")
        except Exception as e:
            print(f"  nf_variant_calling_tuned_icons: FAIL - {e}")

    print()


def build_pipelines_page() -> None:
    """Generate docs/pipelines/index.md and render pipeline SVGs."""
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "nf-core Pipelines"
    print("nf-core pipelines:")

    lines: list[str] = [
        "# nf-core Pipelines",
        "",
        "Real-world pipelines rendered with nf-metro. These are maintained as "
        "`.mmd` files alongside the pipeline source code and rendered automatically.",
        "",
        "See the [Gallery](../gallery/index.md) for layout pattern examples and the "
        "[Guide](../guide.md) for how to write your own.",
        "",
    ]

    for stem, display_name, repo_url, description in PIPELINE_ENTRIES:
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"pipeline_{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        try:
            render_mmd(mmd_path, svg_path, debug=True)
            status = "OK"
        except Exception as e:
            status = f"FAIL: {e}"
            print(f"  {stem}: {status}")
            continue

        _manifest[svg_path.name] = section
        print(f"  {stem}: {status}")

        mmd_source = mmd_path.read_text()

        lines.append(f"## [{display_name}]({repo_url})\n")
        lines.append(f"{description}\n")
        lines.append(f"![{display_name}](../assets/renders/pipeline_{stem}.svg)\n")
        lines.append('??? note "Mermaid source"\n')
        lines.append("    ```text")
        for src_line in mmd_source.rstrip().split("\n"):
            lines.append(f"    {src_line}")
        lines.append("    ```\n")

    pipelines_md = "\n".join(lines)
    pipelines_path = PIPELINES_DIR / "index.md"
    pipelines_path.write_text(pipelines_md)
    print(f"\nPipelines page written to {pipelines_path}")
    print()

    # Also render rnaseq_sections_manual for the guide (not on pipelines page)
    for stem in ("rnaseq_sections_manual",):
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = "Guide Examples"
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")


def render_test_fixtures() -> None:
    """Render test-only fixtures not duplicated in examples/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Test Fixtures"
    print("Test fixtures:")
    for stem in ("multiline_labels", "rnaseq_simple"):
        mmd_path = TEST_FIXTURES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {stem}: OK")
        except Exception as e:
            print(f"  {stem}: FAIL - {e}")
    print()


def write_manifest() -> None:
    """Write render manifest mapping SVG filenames to sections."""
    manifest_path = RENDERS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest, indent=2, sort_keys=True) + "\n")
    print(f"Manifest written to {manifest_path} ({len(_manifest)} entries)")


if __name__ == "__main__":
    # Clean stale renders so removed gallery entries don't persist
    if RENDERS_DIR.exists():
        for old_svg in RENDERS_DIR.glob("*.svg"):
            old_svg.unlink()
    render_guide_examples()
    render_nextflow_examples()
    build_pipelines_page()
    render_test_fixtures()
    build_gallery()
    write_manifest()
