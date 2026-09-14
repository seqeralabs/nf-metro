#!/usr/bin/env python3
"""Build the docs gallery: render .mmd examples to SVG and generate gallery/index.md.

Usage:
    python scripts/build_gallery.py
    python scripts/build_gallery.py --debug   # include debug overlay
    python scripts/build_gallery.py --changed-list FILE   # incremental rebuild

In ``--changed-list`` mode, FILE holds newline-separated repo-relative paths of
the source ``.mmd`` files that changed; the renders directory is expected to be
pre-seeded with the base SVGs/manifest/metrics, and only entries whose source
appears in FILE are re-rendered. Every other entry reuses its base SVG. Only
safe when no rendering code changed.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import yaml

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))
sys.path.insert(0, str(project_root / "tests"))

from layout_metrics import compute_metrics  # noqa: E402

from nf_metro.api import prepare_graph, resolve_theme  # noqa: E402
from nf_metro.render.svg import build_render_plan, emit_render_plan  # noqa: E402

DEBUG_RENDERS = "--debug" in sys.argv


def _parse_changed_list(argv: list[str]) -> set[Path] | None:
    """Parse ``--changed-list FILE`` into a set of resolved source ``.mmd`` paths.

    Returns ``None`` when the flag is absent, selecting a full-corpus render.
    When present, FILE holds newline-separated repo-relative paths of the source
    files that changed; only entries rendered from those sources are re-rendered
    and every other entry reuses its pre-seeded base SVG. The caller MUST
    guarantee no rendering code changed before enabling this, since an engine
    change can alter any render and reused base SVGs would mask it.
    """
    if "--changed-list" not in argv:
        return None
    list_path = Path(argv[argv.index("--changed-list") + 1])
    return {
        (project_root / line.strip()).resolve()
        for line in list_path.read_text().splitlines()
        if line.strip()
    }


ONLY_CHANGED = _parse_changed_list(sys.argv)


def _skip_render(mmd_path: Path) -> bool:
    """True when an incremental build can reuse *mmd_path*'s pre-seeded base SVG."""
    return ONLY_CHANGED is not None and mmd_path.resolve() not in ONLY_CHANGED


EXAMPLES_DIR = project_root / "examples"
NEXTFLOW_FIXTURES_DIR = project_root / "tests" / "fixtures" / "nextflow"
TEST_FIXTURES_DIR = project_root / "tests" / "fixtures"
GUIDE_DIR = project_root / "examples" / "guide"
# Markdown content lives in the repo-root docs/ dir (the Astro site in website/
# loads it via a symlink: website/src/content/docs -> ../../docs).
RENDERS_DIR = project_root / "docs" / "assets" / "renders"
# JSON manifests consumed by Astro content collections (git-ignored, generated).
CONTENT_DIR = project_root / "website" / "src" / "content"

# Base-absolute URL prefix used in gallery entry descriptions.
SITE_BASE = "/nf-metro/"

# Gallery, pipeline, and render-diff config loaded from scripts/gallery.yaml.
_config = yaml.safe_load((Path(__file__).parent / "gallery.yaml").read_text())

GALLERY_ENTRIES: list[tuple[str, Path, str]] = [
    (entry["id"], project_root / entry["source_dir"], entry["description"])
    for entry in _config["gallery"]
]

# Per-stem category label, sourced from the explicit category field in gallery.yaml.
_GALLERY_CATEGORIES: dict[str, str] = {
    entry["id"]: entry["category"] for entry in _config["gallery"]
}

# Per-stem source dir, so a pipeline entry can point at a gallery id whose .mmd
# lives outside examples/ (e.g. examples/showcase/) rather than assuming every
# pipeline's source sits directly in EXAMPLES_DIR.
_GALLERY_SOURCE_DIRS: dict[str, Path] = {
    entry["id"]: project_root / entry["source_dir"] for entry in _config["gallery"]
}

# Ordered list of nf-core pipeline examples.
PIPELINE_ENTRIES: list[tuple[str, str, str, str]] = [
    (entry["id"], entry["title"], entry["repo_url"], entry["description"])
    for entry in _config["pipelines"]
]

# The render-diff-only corpus. The section is required in gallery.yaml; the keys
# below it are not.
_RENDER_ONLY = _config["render_only"]


def _resolve_guard(qualified_name: str) -> type[BaseException]:
    """The exception class an ``expected_aborts`` value names.

    Module-qualified, because a bare class name matches any same-named class
    from anywhere and cannot be resolved to the one that was meant.
    """
    module_name, _, class_name = qualified_name.rpartition(".")
    if not module_name:
        raise ValueError(
            f"expected_aborts needs a module-qualified guard, got {qualified_name!r}"
        )
    guard = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(guard, type) and issubclass(guard, BaseException)):
        raise TypeError(f"expected_aborts entry {qualified_name!r} is not a guard")
    return guard


# Registered render-diff stems whose render aborts, mapped to the guard that
# raises. A stem here produces no render-diff entry, so the annotation is what
# distinguishes a known reproducer from a regression in the build log.
EXPECTED_ABORTS: dict[str, type[BaseException]] = {
    stem: _resolve_guard(name)
    for stem, name in (_RENDER_ONLY.get("expected_aborts") or {}).items()
}


def _render_only_stems() -> list[str]:
    """The ``test_fixtures`` stems, checked for a flattened-name collision.

    Each stem renders to ``<basename>.svg``, so two stems from different
    subdirectories sharing a basename would land on one SVG and one manifest
    key, and the second would silently replace the first.
    """
    stems: list[str] = _RENDER_ONLY["test_fixtures"]
    by_name: dict[str, str] = {}
    for stem in stems:
        name = Path(stem).name
        if by_name.setdefault(name, stem) != stem:
            raise ValueError(
                f"render-only stems {by_name[name]!r} and {stem!r} both render to "
                f"{name}.svg; give one a distinct basename"
            )
    return stems


# Manifest mapping SVG filename -> section for the render diff page.
# Populated by each render function, written to RENDERS_DIR/manifest.json.
_manifest: dict[str, str] = {}

# Layout-quality scorecard per SVG filename, written to RENDERS_DIR/metrics.json
# and reported as per-render deltas in the render-diff page. Advisory only.
_metrics: dict[str, dict[str, float | None]] = {}

_SVG_DIMS_RE = re.compile(r'<svg[^>]*\bwidth="([\d.]+)"[^>]*\bheight="([\d.]+)"')


def _seed_from_base() -> None:
    """Load the pre-seeded base manifest/metrics in an incremental build so reused
    entries keep their section grouping and layout scorecard in the written JSON."""
    for name, target in (("manifest.json", _manifest), ("metrics.json", _metrics)):
        path = RENDERS_DIR / name
        if path.exists():
            target.update(json.loads(path.read_text()))


def _record_metrics(graph, plan, svg_name: str, svg_str: str) -> None:
    """Compute the layout-quality scorecard for a freshly rendered graph.

    Computed alongside the render so the scores reflect the same engine version
    that drew the SVG. A failure here never aborts a render.
    """
    match = _SVG_DIMS_RE.search(svg_str)
    canvas = (float(match.group(1)), float(match.group(2))) if match else None
    try:
        _metrics[svg_name] = compute_metrics(graph, plan=plan, canvas=canvas)
    except Exception as e:  # noqa: BLE001 - metrics are advisory, never fatal
        print(f"    metrics FAIL for {svg_name}: {e}")


# Every map that does not reach the render corpus, so the build can report a
# count and exit nonzero. A silently dropped map shrinks the render-diff surface
# without shrinking the manifest's credibility, which is how a whole class of
# breakage stayed invisible while this script still exited 0.
_dropped: list[str] = []


def _drop(what: str, why: str) -> None:
    _dropped.append(f"{what}: {why}")


def report_dropped() -> int:
    """Print the dropped-map summary and return the process exit status."""
    if not _dropped:
        return 0
    print(f"\n{len(_dropped)} map(s) did not reach the render corpus:")
    for line in _dropped:
        print(f"  {line}")
    return 1


def render_mmd(
    mmd_path: Path,
    svg_path: Path,
    *,
    debug: bool = DEBUG_RENDERS,
    self_color_scheme: bool = True,
    from_nextflow: bool = False,
) -> str:
    """Parse, layout, and render a .mmd file to SVG; write it and return it.

    Goes through :func:`nf_metro.api.prepare_graph`/:func:`~nf_metro.api.render_graph`
    so this script resolves the option cascade the same way the CLI does.

    ``self_color_scheme`` is forwarded to the renderer: pages that inline the SVG
    (gallery, pipelines) pass False so the map inherits the page's color-scheme
    and follows the light/dark toggle. The embedded data manifest carries no
    visual content, so it is disabled for every render here: the gallery is the
    visual-regression surface and the render diff compares these SVGs
    byte-for-byte.
    """
    text = mmd_path.read_text()
    graph = prepare_graph(
        text,
        from_nextflow=from_nextflow,
        source_dir=str(mmd_path.resolve().parent),
    )
    graph.embed_manifest = False
    theme = resolve_theme(None, graph)
    plan = build_render_plan(
        graph,
        theme,
        debug=debug,
        inactive_line_ids=graph.default_inactive_line_ids(),
    )
    svg_str = emit_render_plan(
        plan,
        self_color_scheme=self_color_scheme,
    )
    svg_path.write_text(svg_str)
    _record_metrics(graph, plan, svg_path.name, svg_str)
    return svg_str


def _render_registered_stem(stem: str, mmd_path: Path, svg_path: Path) -> bool:
    """Render one stem named in gallery.yaml, reporting what happened.

    Returns True when an SVG was written, which is what earns the stem a
    render-diff manifest entry. A stem in :data:`EXPECTED_ABORTS` reports the
    guard it trips rather than a failure, and reports its annotation as stale
    if it starts rendering.
    """
    expected_abort = EXPECTED_ABORTS.get(stem)
    try:
        render_mmd(mmd_path, svg_path)
    except Exception as e:
        if expected_abort is not None and isinstance(e, expected_abort):
            print(
                f"  {stem}: ABORTS ({expected_abort.__name__}) - registered "
                f"reproducer, no render-diff entry"
            )
        else:
            print(f"  {stem}: FAIL - {e}")
            _drop(stem, str(e))
        return False
    if expected_abort:
        print(
            f"  {stem}: OK - renders now; drop its expected_aborts entry in "
            f"scripts/gallery.yaml"
        )
    else:
        print(f"  {stem}: OK")
    return True


def clean_name(stem: str) -> str:
    """Convert filename stem to a display-friendly heading."""
    return stem.replace("_", " ").title()


def metro_src(stem: str, source_dir: Path) -> str:
    """Repo-relative path to a committed ``.mmd``, for a ``<Metro src=…>`` tag.

    The gallery and pipelines pages render each example live through the
    ``<Metro>`` component, which reads the source itself, so the page only names
    the file rather than importing it.
    """
    return (source_dir.relative_to(project_root) / f"{stem}.mmd").as_posix()


def build_gallery_manifest() -> None:
    """Emit website/src/content/gallery.json for the Astro content collection.

    Each entry carries the metadata the dynamic route needs: id, title, src
    (repo-relative path for ``<Metro>``), description, and category label.
    The file is git-ignored and regenerated by this script before every build.
    """
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for stem, source_dir, description in GALLERY_ENTRIES:
        mmd_path = source_dir / f"{stem}.mmd"
        if not mmd_path.exists():
            _drop(stem, f"registered in gallery.yaml but {mmd_path} is absent")
            continue
        entries.append(
            {
                "id": stem,
                "order": len(entries),
                "title": clean_name(stem),
                "src": metro_src(stem, source_dir),
                "description": description,
                "category": _GALLERY_CATEGORIES[stem],
            }
        )
    out = CONTENT_DIR / "gallery.json"
    out.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Gallery manifest written to {out} ({len(entries)} entries)")


def build_pipelines_manifest() -> None:
    """Emit website/src/content/pipelines.json for the Astro content collection."""
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for stem, display_name, repo_url, description in PIPELINE_ENTRIES:
        source_dir = _GALLERY_SOURCE_DIRS.get(stem, EXAMPLES_DIR)
        mmd_path = source_dir / f"{stem}.mmd"
        if not mmd_path.exists():
            _drop(stem, f"registered in gallery.yaml but {mmd_path} is absent")
            continue
        entries.append(
            {
                "id": stem,
                "title": display_name,
                "src": metro_src(stem, source_dir),
                "repo_url": repo_url,
                "description": description,
            }
        )
    out = CONTENT_DIR / "pipelines.json"
    out.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Pipelines manifest written to {out} ({len(entries)} entries)")


def render_guide_examples() -> None:
    """Render guide examples to docs/assets/renders/ for the CI render-diff.

    The docs render these live from source through the ``<Metro>`` component;
    these SVGs exist only so the render-diff and the layout-metrics scorecard
    cover the guide's examples. Pages never reference them.
    """
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Guide Examples"
    print("Guide examples:")

    for mmd_path in sorted(GUIDE_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"{mmd_path.stem}.svg"
        if _skip_render(mmd_path):
            _manifest[svg_path.name] = section
            continue
        try:
            render_mmd(mmd_path, svg_path)
            _manifest[svg_path.name] = section
            print(f"  {mmd_path.stem}: OK")
        except Exception as e:
            print(f"  {mmd_path.stem}: FAIL - {e}")
            _drop(mmd_path.stem, str(e))

    for stem in _config["render_only"]["guide_examples"]:
        mmd_path = EXAMPLES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            _drop(str(stem), f"registered for render but {mmd_path} is absent")
            continue
        svg_path = RENDERS_DIR / f"{stem}.svg"
        if _skip_render(mmd_path):
            _manifest[svg_path.name] = section
            continue
        if _render_registered_stem(stem, mmd_path, svg_path):
            _manifest[svg_path.name] = section

    debug_src = EXAMPLES_DIR / "rnaseq_auto.mmd"
    debug_svg = RENDERS_DIR / "rnaseq_auto_debug.svg"
    if debug_src.exists() and _skip_render(debug_src):
        _manifest[debug_svg.name] = section
    elif debug_src.exists():
        try:
            render_mmd(debug_src, debug_svg, debug=True)
            _manifest[debug_svg.name] = section
            print("  rnaseq_auto_debug: OK")
        except Exception as e:
            print(f"  rnaseq_auto_debug: FAIL - {e}")
    print()


def build_gallery() -> None:
    """Render gallery SVGs to docs/assets/renders/ for the CI render-diff."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)

    for stem, source_dir, description in GALLERY_ENTRIES:
        mmd_path = source_dir / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        # The SVG feeds the CI render-diff; pages render live via <Metro>.
        if _skip_render(mmd_path):
            status = "REUSED"
        else:
            try:
                render_mmd(mmd_path, svg_path, self_color_scheme=False)
                status = "OK"
            except Exception as e:
                status = f"FAIL: {e}"
                print(f"  {stem}: {status}")
                _drop(stem, str(e))
                continue

        _manifest[svg_path.name] = _GALLERY_CATEGORIES[stem]
        print(f"  {stem}: {status}")


def render_nextflow_examples() -> None:
    """Render Nextflow DAG fixtures and hand-tuned example to docs/assets/renders/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Nextflow Conversions"
    print("Nextflow examples:")

    # Auto-converted renders from Nextflow DAG fixtures
    for mmd_path in sorted(NEXTFLOW_FIXTURES_DIR.glob("*.mmd")):
        svg_path = RENDERS_DIR / f"nf_{mmd_path.stem}.svg"
        if _skip_render(mmd_path):
            _manifest[svg_path.name] = section
            continue
        try:
            render_mmd(mmd_path, svg_path, from_nextflow=True)
            _manifest[svg_path.name] = section
            print(f"  nf_{mmd_path.stem}: OK")
        except Exception as e:
            print(f"  nf_{mmd_path.stem}: FAIL - {e}")
            _drop(f"nf_{mmd_path.stem}", str(e))

    for entry in _config["render_only"]["nextflow_conversions"]:
        src_path = EXAMPLES_DIR / f"{entry['id']}.mmd"
        if not src_path.exists():
            _drop(str(entry["id"]), f"registered for render but {src_path} is absent")
            continue
        svg_path = RENDERS_DIR / f"{entry['output']}.svg"
        if _skip_render(src_path):
            _manifest[svg_path.name] = section
        else:
            try:
                render_mmd(src_path, svg_path)
                _manifest[svg_path.name] = section
                print(f"  {entry['output']}: OK")
            except Exception as e:
                print(f"  {entry['output']}: FAIL - {e}")

    print()


def build_pipelines_page() -> None:
    """Render pipeline SVGs to docs/assets/renders/ for the CI render-diff."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "nf-core Pipelines"
    print("nf-core pipelines:")

    for stem, display_name, repo_url, description in PIPELINE_ENTRIES:
        source_dir = _GALLERY_SOURCE_DIRS.get(stem, EXAMPLES_DIR)
        mmd_path = source_dir / f"{stem}.mmd"
        svg_path = RENDERS_DIR / f"pipeline_{stem}.svg"

        if not mmd_path.exists():
            print(f"  WARNING: {mmd_path} not found, skipping")
            continue

        # The SVG feeds the CI render-diff; pages render live via <Metro>.
        if _skip_render(mmd_path):
            status = "REUSED"
        else:
            try:
                render_mmd(
                    mmd_path, svg_path, debug=DEBUG_RENDERS, self_color_scheme=False
                )
                status = "OK"
            except Exception as e:
                status = f"FAIL: {e}"
                print(f"  {stem}: {status}")
                _drop(stem, str(e))
                continue

        _manifest[svg_path.name] = section
        print(f"  {stem}: {status}")

    print()


def render_test_fixtures() -> None:
    """Render test-only fixtures not duplicated in examples/."""
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    section = "Test Fixtures"
    print("Test fixtures:")
    for stem in _render_only_stems():
        mmd_path = TEST_FIXTURES_DIR / f"{stem}.mmd"
        if not mmd_path.exists():
            _drop(str(stem), f"registered for render but {mmd_path} is absent")
            continue
        svg_path = RENDERS_DIR / f"{Path(stem).name}.svg"
        if _skip_render(mmd_path):
            _manifest[svg_path.name] = section
            continue
        if _render_registered_stem(stem, mmd_path, svg_path):
            _manifest[svg_path.name] = section
    print()


def write_manifest() -> None:
    """Write render manifest mapping SVG filenames to sections."""
    manifest_path = RENDERS_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest, indent=2, sort_keys=True) + "\n")
    print(f"Manifest written to {manifest_path} ({len(_manifest)} entries)")


def write_metrics() -> None:
    """Write the per-render layout-quality scorecard for the render-diff page."""
    metrics_path = RENDERS_DIR / "metrics.json"
    metrics_path.write_text(json.dumps(_metrics, indent=2, sort_keys=True) + "\n")
    print(f"Metrics written to {metrics_path} ({len(_metrics)} entries)")


if __name__ == "__main__":
    if ONLY_CHANGED is None:
        # Clean stale renders so removed gallery entries don't persist
        if RENDERS_DIR.exists():
            for old_svg in RENDERS_DIR.glob("*.svg"):
                old_svg.unlink()
    else:
        # Incremental build: keep the pre-seeded base SVGs and carry their
        # section/metrics through into the written JSON.
        _seed_from_base()
    render_guide_examples()
    render_nextflow_examples()
    build_pipelines_page()
    render_test_fixtures()
    build_gallery()
    build_gallery_manifest()
    build_pipelines_manifest()
    write_manifest()
    write_metrics()
    sys.exit(report_dropped())
