"""Tests for mode-adaptive logo path selection."""

from nf_metro.parser.model import MetroGraph
from nf_metro.render.ns import adaptive_logo_mask_ids as _adaptive_logo_mask_ids
from nf_metro.render.svg import (
    _effective_logo_path,
    _has_adaptive_logos,
    _is_adaptive_mode,
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
