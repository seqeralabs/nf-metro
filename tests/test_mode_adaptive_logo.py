"""Tests for mode-adaptive logo path selection."""

import io
from pathlib import Path

import pytest
from PIL import Image as PILImage

from nf_metro.parser.model import MetroGraph
from nf_metro.render.ns import adaptive_logo_mask_ids as _adaptive_logo_mask_ids
from nf_metro.render.svg import (
    _effective_logo_path,
    _has_adaptive_logos,
    _is_adaptive_mode,
    _resolve_logo,
)


def _graph_with_logo(
    *,
    logo_path: str = "",
    logo_path_light: str = "",
    logo_path_dark: str = "",
    style: str = "dark",
) -> MetroGraph:
    g = MetroGraph()
    g.logo_path = logo_path
    g.logo_path_light = logo_path_light
    g.logo_path_dark = logo_path_dark
    g.style = style
    return g


def test_single_path_returned_regardless_of_style():
    g = _graph_with_logo(logo_path="logo.png", style="dark")
    assert _effective_logo_path(g) == "logo.png"

    g.style = "light"
    assert _effective_logo_path(g) == "logo.png"


def test_dark_variant_chosen_for_dark_style():
    g = _graph_with_logo(
        logo_path_light="light.png", logo_path_dark="dark.png", style="dark"
    )
    assert _effective_logo_path(g) == "dark.png"


def test_light_variant_chosen_for_light_style():
    g = _graph_with_logo(
        logo_path_light="light.png", logo_path_dark="dark.png", style="light"
    )
    assert _effective_logo_path(g) == "light.png"


def test_light_variant_chosen_case_insensitive():
    g = _graph_with_logo(
        logo_path_light="light.png", logo_path_dark="dark.png", style="Light"
    )
    assert _effective_logo_path(g) == "light.png"


def test_fallback_to_logo_path_when_no_variants():
    g = _graph_with_logo(logo_path="fallback.png", style="light")
    assert _effective_logo_path(g) == "fallback.png"


def test_fallback_to_logo_path_when_light_variant_missing():
    g = _graph_with_logo(
        logo_path="fallback.png", logo_path_dark="dark.png", style="light"
    )
    assert _effective_logo_path(g) == "fallback.png"


def test_fallback_to_logo_path_when_dark_variant_missing():
    g = _graph_with_logo(
        logo_path="fallback.png", logo_path_light="light.png", style="dark"
    )
    assert _effective_logo_path(g) == "fallback.png"


def test_empty_when_no_logo_set():
    g = _graph_with_logo(style="dark")
    assert _effective_logo_path(g) == ""


def test_adaptive_logo_mask_ids_stable():
    dark_id, light_id = _adaptive_logo_mask_ids("examples/logo_dark.png")
    dark_id2, light_id2 = _adaptive_logo_mask_ids("examples/logo_dark.png")
    assert dark_id == dark_id2
    assert light_id == light_id2


def test_adaptive_logo_mask_ids_different_paths():
    dark_a, light_a = _adaptive_logo_mask_ids("examples/logo_a_dark.png")
    dark_b, light_b = _adaptive_logo_mask_ids("examples/logo_b_dark.png")
    assert dark_a != dark_b
    assert light_a != light_b


def test_adaptive_logo_mask_ids_dark_light_differ():
    dark_id, light_id = _adaptive_logo_mask_ids("examples/logo_dark.png")
    assert dark_id != light_id


def test_has_adaptive_logos_false_when_files_missing():
    g = _graph_with_logo(
        logo_path_light="nonexistent_light.png", logo_path_dark="nonexistent_dark.png"
    )
    assert not _has_adaptive_logos(g)


def test_has_adaptive_logos_false_when_variants_empty():
    g = _graph_with_logo(logo_path="single.png")
    assert not _has_adaptive_logos(g)


def test_is_adaptive_mode_true_when_both_set():
    g = _graph_with_logo(logo_path_light="light.png", logo_path_dark="dark.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_true_when_light_only():
    g = _graph_with_logo(logo_path_light="light.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_true_when_dark_only():
    g = _graph_with_logo(logo_path_dark="dark.png")
    assert _is_adaptive_mode(g)


def test_is_adaptive_mode_false_for_single_path():
    g = _graph_with_logo(logo_path="logo.png")
    assert not _is_adaptive_mode(g)


def test_is_adaptive_mode_false_when_no_logo():
    g = _graph_with_logo()
    assert not _is_adaptive_mode(g)


def _write_png(path: Path) -> None:
    img = PILImage.new("RGB", (100, 50), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


def test_resolve_logo_errors_on_missing_single_path():
    """A set logo_path that doesn't exist on disk should raise ValueError."""
    g = MetroGraph()
    g.logo_path = "does/not/exist.png"
    with pytest.raises(ValueError, match="does/not/exist.png"):
        _resolve_logo(g, adaptive=False)


def test_resolve_logo_no_error_when_no_path_set():
    """Empty logo_path raises no error."""
    g = MetroGraph()
    show, _, _, _ = _resolve_logo(g, adaptive=False)
    assert not show


def test_resolve_logo_resolves_relative_to_source_dir(tmp_path):
    """A logo_path relative to source_dir resolves correctly."""
    logo_file = tmp_path / "mylogo.png"
    _write_png(logo_file)

    g = MetroGraph()
    g.logo_path = "mylogo.png"
    g.source_dir = str(tmp_path)

    show, w, h, effective = _resolve_logo(g, adaptive=False)
    assert show
    assert effective == str(logo_file)
    assert w > 0


def test_resolve_logo_errors_when_source_dir_set_but_still_missing(tmp_path):
    """A logo_path that doesn't exist relative to source_dir raises ValueError."""
    g = MetroGraph()
    g.logo_path = "nonexistent.png"
    g.source_dir = str(tmp_path)
    with pytest.raises(ValueError, match="nonexistent.png"):
        _resolve_logo(g, adaptive=False)


def test_resolve_adaptive_logo_errors_on_missing_paths():
    """In adaptive mode, missing both variants should raise ValueError."""
    g = MetroGraph()
    g.logo_path_light = "missing_light.png"
    g.logo_path_dark = "missing_dark.png"
    with pytest.raises(ValueError, match="missing_light.png|missing_dark.png"):
        _resolve_logo(g, adaptive=True)


def test_resolve_adaptive_logo_resolves_relative_to_source_dir(tmp_path):
    """Adaptive logo paths resolve relative to source_dir."""
    light_file = tmp_path / "light.png"
    dark_file = tmp_path / "dark.png"
    _write_png(light_file)
    _write_png(dark_file)

    g = MetroGraph()
    g.logo_path_light = "light.png"
    g.logo_path_dark = "dark.png"
    g.source_dir = str(tmp_path)

    show, w, h, _ = _resolve_logo(g, adaptive=True)
    assert show
    assert w > 0
