"""A user-set ``fold_threshold`` either renders or fails with an authoring
error, never an internal routing self-check (#1088).

A ``--fold-threshold`` / ``%%metro fold_threshold`` (equivalently
``max_station_columns``) below a map's natural width compresses the section
grid.  On sufficiently compacted geometry the router cannot separate parallel
bundles, size concentric corners, or seat a section header clear of a route.
When the requested threshold compressed the section grid relative to its
unbounded layout, the render chokepoint reframes such an abort as
:class:`FoldThresholdError` (a ``ValueError`` the CLI surfaces cleanly), naming
the directive and recommending a wider threshold, rather than letting an
internal ``CurveInvariantError`` / ``SectionHeaderClashError`` reach the user.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from nf_metro.api import prepare_graph
from nf_metro.layout import FoldThresholdError
from nf_metro.layout.routing.invariants import CurveInvariantError
from nf_metro.render import render_svg
from nf_metro.render.section_header import SectionHeaderClashError
from nf_metro.themes import THEMES

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _find(name: str) -> Path:
    hits = glob.glob(str(EXAMPLES / "**" / f"{name}.mmd"), recursive=True)
    assert hits, f"fixture {name}.mmd not found under {EXAMPLES}"
    return Path(hits[0])


def _render_at(name: str, fold: int | None) -> None:
    # Mirror the CLI render path: parse (records fold compression), apply layout
    # options, compute coordinates, then render.  The abort is geometry-driven,
    # so the full layout must run before render for it to surface.
    opts = {"fold_threshold": fold} if fold is not None else {}
    graph = prepare_graph(_find(name).read_text(), layout_options=opts)
    render_svg(graph, THEMES["nfcore"])


# (fixture, aggressive fold) - each renders at its default width but aborts on
# the internal invariant once the fold compresses its section grid.  A spread
# of failure shapes: collinear V/H overlay, bundle-order flip, non-concentric
# corner, and the section-header clash.
COMPRESSED_ABORTS = [
    ("section_diamond", 1),
    ("fold_fan_across", 2),
    ("off_track_input_above_consumer", 1),
    ("upward_bypass", 1),
    ("shared_sink_parallel", 1),
    ("mixed_bundle_column", 1),
    ("epitopeprediction", 3),
]


@pytest.mark.parametrize("name, fold", COMPRESSED_ABORTS)
def test_aggressive_fold_raises_authoring_error_not_internal(
    name: str, fold: int
) -> None:
    with pytest.raises(FoldThresholdError) as exc:
        _render_at(name, fold)
    # The authoring error must name the offending directive and point at the fix.
    msg = str(exc.value).lower()
    assert "fold" in msg
    # It is an authoring error (ValueError), not an internal engine self-check.
    assert isinstance(exc.value, ValueError)
    assert not isinstance(exc.value, (CurveInvariantError, SectionHeaderClashError))


@pytest.mark.parametrize("name, fold", COMPRESSED_ABORTS)
def test_same_fixtures_render_at_default_fold(name: str, fold: int) -> None:
    # Sanity: the abort is purely fold-induced - each renders at its default
    # width with no fold set.
    _render_at(name, None)


def test_uncompressed_layout_does_not_reframe() -> None:
    # A fold at or above the natural width does not compress the grid, so an
    # abort there (there is none on this clean fixture) would surface as the
    # internal error, never reframed.  Rendering must simply succeed.
    _render_at("variant_calling", 99)
    _render_at("rnaseq_auto", 99)
