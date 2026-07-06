"""Regression test: duplicate SVG ids across inlined render-diff panels.

The render-diff page (``scripts/build_render_diff.py``) inlines two copies of
the same pipeline's SVG side by side (base + PR). When both copies share the
same source (e.g. the mask markup is byte-identical), their ``id``s collide.
The base/PR/side-by-side toggle buttons switch panels via ``display:none``,
and a browser resolving ``url(#id)`` picks the first same-id element in
document order - if that first element sits inside a now-hidden subtree, the
reference stops working and the mask is dropped entirely, so both the light
and dark logo variants render unmasked on top of each other.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from nf_metro.api import render_string

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_render_diff import _inline_svg  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

_ID_RE = re.compile(r'\bid="([^"]+)"')
_URL_REF_RE = re.compile(r"url\(#([^)]+)\)")


def _render_adaptive_logo_svg() -> str:
    text = (EXAMPLES / "sarek_metro.mmd").read_text()
    return render_string(text, self_color_scheme=False)


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
