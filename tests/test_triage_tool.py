"""Guards for the layout-invariant triage tool (``nf-metro-layout-triage``).

The tool's finder registry keys must track the live invariant names in
``test_layout_invariants.py``: a key naming a removed/renamed test is dead
code that can never visualise the invariant that actually reds CI. The
rendered-label box must also land on the drawn ``<text>`` glyph rather than
the engine's logical label coordinates.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_REVIEW = (
    _REPO_ROOT / ".claude" / "skills" / "nf-metro-layout-triage" / "build_review.py"
)
_INVARIANTS_SRC = _REPO_ROOT / "tests" / "test_layout_invariants.py"


def _load_build_review():
    if not _BUILD_REVIEW.is_file():
        pytest.skip(f"triage tool not present at {_BUILD_REVIEW}")
    spec = importlib.util.spec_from_file_location("triage_build_review", _BUILD_REVIEW)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finder_registry_keys_are_live_invariants():
    br = _load_build_review()
    suite_src = _INVARIANTS_SRC.read_text()
    stale = [key for key in br.INVARIANT_FINDERS if f"def {key}(" not in suite_src]
    assert not stale, (
        "triage finder registry names tests that no longer exist in "
        f"test_layout_invariants.py: {stale}"
    )


def test_rendered_label_box_uses_drawn_glyph_coords():
    br = _load_build_review()
    svg = (
        '<svg><text x="932.5" y="135.0" font-size="13.0" text-anchor="middle" '
        'dominant-baseline="hanging" data-station-id="star" '
        'class="nf-metro-station-label">STAR</text></svg>'
    )
    box = br._rendered_label_box(svg, "star")
    assert box is not None
    left, top, width, height = box
    # middle anchor centres the glyph on x; hanging baseline tops it at y.
    assert left + width / 2 == pytest.approx(932.5, abs=0.01)
    assert top == pytest.approx(135.0, abs=0.01)
    assert height == pytest.approx(13.0, abs=0.01)
    assert br._rendered_label_box(svg, "nonexistent") is None


def test_label_violator_overlay_marks_glyph_and_expected_marker():
    br = _load_build_review()
    svg = (
        '<svg><text x="960.0" y="135.0" font-size="13.0" text-anchor="middle" '
        'dominant-baseline="hanging" data-station-id="star" '
        'class="nf-metro-station-label">STAR</text></svg>'
    )
    violator = {
        "kind": "label",
        "station_id": "star",
        "expected_x": 932.5,
        "label_x": 960.0,
    }
    annotated, count = br.annotate_svg(svg, [violator])
    assert count == 1
    assert "<rect" in annotated
    # The expected-marker tick is drawn at the station marker X, not label.x.
    assert 'x1="932.5"' in annotated
