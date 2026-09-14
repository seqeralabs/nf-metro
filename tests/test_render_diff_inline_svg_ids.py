"""Regression tests for cross-panel contamination in inlined render-diff panels.

The render-diff page (``scripts/build_render_diff.py``) inlines two copies of
the same pipeline's SVG side by side (base + PR) into one HTML document.
Inline SVG ``id`` scope and ``<style>`` scope are both document-global, so two
panels sharing an ``id`` or a presentation class name apply to each other: a
``url(#id)`` reference in a hidden panel (the base/PR/side-by-side toggle
buttons switch panels via ``display:none``) resolves into a visible panel's
element, and a CSS rule in one panel's stylesheet matches the other panel's
identically-classed elements. Both effects mask a genuine difference between
the base and PR renders that the diff page exists to surface.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from nf_metro.api import render_string

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_render_diff import (  # noqa: E402
    _CLASS_ATTR_RE,
    _CLASS_SELECTOR_RE,
    _STYLE_BLOCK_RE,
    _URL_REF_RE,
    _inline_svg,
    build_diff,
)
from build_render_diff import _ID_ATTR_RE as _ID_RE  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

_MUTED_RULE_PAIR_RE = re.compile(
    r"\.nf-metro-station-label\.nf-metro-muted\s*\{[^}]*\}\n?"
    r"\.nf-metro-marker-stroke\.nf-metro-muted\s*\{[^}]*\}\n?"
)


def _render_inactive_lines_svg() -> str:
    text = (EXAMPLES / "inactive_lines.mmd").read_text()
    return render_string(text, self_color_scheme=False)


def _strip_muted_rule(svg_text: str) -> str:
    """Delete the muted CSS rule pair from *svg_text*'s own stylesheet, leaving
    its ``nf-metro-muted``-classed elements with no local rule to give that
    class a meaning."""
    stripped, n = _MUTED_RULE_PAIR_RE.subn("", svg_text)
    assert n == 1, (
        "fixture must declare the muted CSS rule pair for this test to be meaningful"
    )
    return stripped


def _classes_used_by_elements(fragment: str) -> set[str]:
    """Every class token that appears on an element in *fragment*."""
    tokens: set[str] = set()
    for m in _CLASS_ATTR_RE.finditer(fragment):
        tokens.update(m.group(1).split())
    return tokens


def _classes_named_in_style_selectors(fragment: str) -> set[str]:
    """Every class name a selector inside *fragment*'s <style> block(s) references."""
    names: set[str] = set()
    for style_match in _STYLE_BLOCK_RE.finditer(fragment):
        names.update(_CLASS_SELECTOR_RE.findall(style_match.group(2)))
    return names


def _assert_no_cross_panel_class_match(selector_side: str, class_side: str) -> None:
    """Fail if *selector_side*'s <style> block selects any element in *class_side*."""
    selectors = _classes_named_in_style_selectors(selector_side)
    classes = _classes_used_by_elements(class_side)
    matched = selectors & classes
    assert not matched, (
        f"one panel's <style> selects the other panel's element(s) via shared "
        f"class name(s) {matched}"
    )


def _render_adaptive_logo_svg() -> str:
    text = (EXAMPLES / "sarek_metro.mmd").read_text()
    return render_string(text, source_dir=str(EXAMPLES), self_color_scheme=False)


def test_url_referenced_ids_are_unique_across_inlined_panels(tmp_path):
    """No two url(#...)-referenced ids may collide once base+PR are inlined together."""
    svg_text = _render_adaptive_logo_svg()
    assert _URL_REF_RE.search(svg_text), (
        "fixture must contain a url(#...) reference (e.g. an adaptive logo mask) "
        "for this test to be meaningful"
    )

    base_path = tmp_path / "sarek_metro.svg"
    pr_path = tmp_path / "sarek_metro_pr.svg"
    base_path.write_text(svg_text)
    pr_path.write_text(svg_text)

    base_inlined = _inline_svg(base_path, "sarek_metro-base")
    pr_inlined = _inline_svg(pr_path, "sarek_metro-pr")
    combined = base_inlined + pr_inlined

    referenced_ids = _URL_REF_RE.findall(combined)
    assert referenced_ids, "expected url(#...) references to survive inlining"

    defined_ids = _ID_RE.findall(combined)
    for ref in referenced_ids:
        assert defined_ids.count(ref) == 1, (
            f"id {ref!r} referenced by url(#{ref}) must be unique across "
            "inlined panels, or a display:none toggle on one panel breaks "
            "mask resolution for the other"
        )


def test_each_panel_still_resolves_its_own_references(tmp_path):
    """Renaming ids must not break a panel's own url(#...) -> id(...) resolution."""
    svg_text = _render_adaptive_logo_svg()
    path = tmp_path / "sarek_metro.svg"
    path.write_text(svg_text)

    inlined = _inline_svg(path, "sarek_metro-pr")
    defined_ids = set(_ID_RE.findall(inlined))
    for ref in _URL_REF_RE.findall(inlined):
        assert ref in defined_ids, (
            f"url(#{ref}) no longer resolves within its own panel"
        )


def test_build_diff_output_has_no_id_collisions(tmp_path):
    """End-to-end: the generated diff page itself must not collide any panel's ids."""
    svg_text = _render_adaptive_logo_svg()
    base_dir = tmp_path / "base"
    pr_dir = tmp_path / "pr"
    base_dir.mkdir()
    pr_dir.mkdir()
    (base_dir / "sarek_metro.svg").write_text(svg_text)
    # A trivial byte difference is enough to classify this as a "changed" render,
    # matching the case that renders both panels onto the page together.
    (pr_dir / "sarek_metro.svg").write_text(svg_text + "<!-- pr -->")

    output_dir = tmp_path / "out"
    assert build_diff(base_dir, pr_dir, output_dir)

    page = (output_dir / "index.html").read_text()
    referenced_ids = _URL_REF_RE.findall(page)
    assert referenced_ids
    defined_ids = _ID_RE.findall(page)
    for ref in referenced_ids:
        assert defined_ids.count(ref) == 1, (
            f"id {ref!r} referenced by url(#{ref}) collides in the generated page"
        )


def test_one_panels_stylesheet_cannot_select_the_other_panels_elements(tmp_path):
    """A rule in one panel's <style> block must not select the other's elements."""
    svg_text = _render_inactive_lines_svg()
    defective_svg_text = _strip_muted_rule(svg_text)

    base_path = tmp_path / "inactive_lines.svg"
    pr_path = tmp_path / "inactive_lines_pr.svg"
    base_path.write_text(svg_text)
    pr_path.write_text(defective_svg_text)

    base_inlined = _inline_svg(base_path, "inactive_lines-base")
    pr_inlined = _inline_svg(pr_path, "inactive_lines-pr")

    _assert_no_cross_panel_class_match(base_inlined, pr_inlined)
    _assert_no_cross_panel_class_match(pr_inlined, base_inlined)


def test_build_diff_output_isolates_panel_stylesheets(tmp_path):
    """The diff page must not let one panel's <style> select the other's elements."""
    svg_text = _render_inactive_lines_svg()
    defective_svg_text = _strip_muted_rule(svg_text)

    base_dir = tmp_path / "base"
    pr_dir = tmp_path / "pr"
    base_dir.mkdir()
    pr_dir.mkdir()
    (base_dir / "inactive_lines.svg").write_text(svg_text)
    (pr_dir / "inactive_lines.svg").write_text(defective_svg_text)

    output_dir = tmp_path / "out"
    assert build_diff(base_dir, pr_dir, output_dir)

    page = (output_dir / "index.html").read_text()
    base_start = page.index('class="side side-base"')
    pr_start = page.index('class="side side-pr"')
    side_base = page[base_start:pr_start]
    side_pr = page[pr_start:]

    _assert_no_cross_panel_class_match(side_base, side_pr)


def test_style_block_url_with_dotted_filename_survives_class_rewrite(tmp_path):
    """A dotted filename inside url(...) is not a class selector, so the
    <style> rewrite must leave it verbatim while namespacing the surrounding
    selectors and their matching class attributes together."""
    svg_text = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">\n'
        "<style>.nf-metro-bg { fill: url(sprite.png); }\n"
        ".nf-metro-title { fill: #111; }</style>\n"
        '<rect class="nf-metro-bg" />\n'
        '<text class="nf-metro-title">Hi</text>\n'
        "</svg>"
    )
    path = tmp_path / "synthetic.svg"
    path.write_text(svg_text)

    inlined = _inline_svg(path, "synthetic-pr")

    assert "url(sprite.png)" in inlined

    style_match = re.search(r"<style>(.*?)</style>", inlined, re.DOTALL)
    assert style_match
    style_body = style_match.group(1)
    assert ".nf-metro-bg--synthetic-pr {" in style_body
    assert ".nf-metro-title--synthetic-pr {" in style_body

    assert 'class="nf-metro-bg--synthetic-pr"' in inlined
    assert 'class="nf-metro-title--synthetic-pr"' in inlined


def test_build_diff_leaves_corpus_svgs_byte_unchanged(tmp_path):
    """Generating the diff page must not modify the on-disk SVGs the gate compares."""
    svg_text = _render_inactive_lines_svg()
    defective_svg_text = _strip_muted_rule(svg_text)

    base_dir = tmp_path / "base"
    pr_dir = tmp_path / "pr"
    base_dir.mkdir()
    pr_dir.mkdir()
    base_svg_path = base_dir / "inactive_lines.svg"
    pr_svg_path = pr_dir / "inactive_lines.svg"
    base_svg_path.write_text(svg_text)
    pr_svg_path.write_text(defective_svg_text)

    output_dir = tmp_path / "out"
    assert build_diff(base_dir, pr_dir, output_dir)

    assert base_svg_path.read_text() == svg_text
    assert pr_svg_path.read_text() == defective_svg_text


def test_watermark_version_only_difference_is_not_a_change(tmp_path):
    """Two renders that differ only in the watermark's version stamp are unchanged."""
    svg_text = _render_inactive_lines_svg()
    match = re.search(r"created with nf-metro v[^<]*", svg_text)
    assert match is not None, "fixture must carry the attribution watermark"
    bumped_svg_text = svg_text.replace(match.group(0), "created with nf-metro v99.0.0")
    assert bumped_svg_text != svg_text

    base_dir = tmp_path / "base"
    pr_dir = tmp_path / "pr"
    base_dir.mkdir()
    pr_dir.mkdir()
    (base_dir / "inactive_lines.svg").write_text(svg_text)
    (pr_dir / "inactive_lines.svg").write_text(bumped_svg_text)

    assert not build_diff(base_dir, pr_dir, tmp_path / "out")


def test_geometry_difference_is_still_a_change_when_versions_also_differ(tmp_path):
    svg_text = _render_inactive_lines_svg()
    defective_svg_text = _strip_muted_rule(svg_text).replace(
        "created with nf-metro v", "created with nf-metro v99.", 1
    )

    base_dir = tmp_path / "base"
    pr_dir = tmp_path / "pr"
    base_dir.mkdir()
    pr_dir.mkdir()
    (base_dir / "inactive_lines.svg").write_text(svg_text)
    (pr_dir / "inactive_lines.svg").write_text(defective_svg_text)

    assert build_diff(base_dir, pr_dir, tmp_path / "out")
