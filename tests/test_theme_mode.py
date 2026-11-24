"""Brand and mode are orthogonal axes.

Brand (nfcore, seqera) picks identity; mode (light, dark) picks the chrome
palette. No brand is intrinsically light or dark - an unspecified mode falls to
a single global default for every brand alike.
"""

import pytest

from nf_metro.api import resolve_theme
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.render.style import color_pair, is_adaptive
from nf_metro.themes import (
    DEFAULT_MODE,
    NFCORE_DARK_THEME,
    NFCORE_LIGHT_THEME,
    SEQERA_DARK_THEME,
    SEQERA_LIGHT_THEME,
    STYLE_NAMES,
    THEMES,
    resolve_stack,
)

_MMD = "%%metro line: main | Main | #ff0000\ngraph LR\n    a -->|main| b\n"


def _graph(style="nfcore", mode=""):
    g = parse_metro_mermaid(_MMD)
    g.style = style
    g.mode = mode
    return g


# ---------------------------------------------------------------------------
# Brand carries no mode: an unspecified mode resolves to the global default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["nfcore", "seqera", "transparent"])
def test_unspecified_mode_uses_global_default_for_every_brand(style):
    resolved = resolve_theme(_graph(style=style))
    assert resolved.mode == DEFAULT_MODE


def test_brands_share_one_default_mode_not_a_per_brand_one():
    """nfcore and seqera resolve to the same mode when none is given."""
    nfcore = resolve_theme(_graph(style="nfcore"))
    seqera = resolve_theme(_graph(style="seqera"))
    assert nfcore.mode == seqera.mode == DEFAULT_MODE


# ---------------------------------------------------------------------------
# Mode is chosen independently of brand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "style,mode,expected",
    [
        ("nfcore", "light", NFCORE_LIGHT_THEME),
        ("nfcore", "dark", NFCORE_DARK_THEME),
        ("seqera", "light", SEQERA_LIGHT_THEME),
        ("seqera", "dark", SEQERA_DARK_THEME),
    ],
)
def test_brand_and_mode_combine_independently(style, mode, expected):
    assert resolve_theme(_graph(style=style), mode=mode) is expected


def test_explicit_mode_arg_overrides_directive():
    assert resolve_theme(_graph(mode="dark"), mode="light") is NFCORE_LIGHT_THEME


def test_directive_mode_used_when_no_arg():
    assert resolve_theme(_graph("seqera", mode="light")) is SEQERA_LIGHT_THEME


# ---------------------------------------------------------------------------
# %%metro mode: directive
# ---------------------------------------------------------------------------


def test_mode_directive_parsed():
    g = parse_metro_mermaid(f"%%metro mode: light\n{_MMD}")
    assert g.mode == "light"


def test_invalid_mode_directive_ignored():
    g = parse_metro_mermaid(f"%%metro mode: sepia\n{_MMD}")
    assert g.mode == ""


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_every_theme_resolves_to_one_theme_per_mode():
    for name in STYLE_NAMES:
        resolved = resolve_stack(name)
        assert (resolved.light.mode, resolved.dark.mode) == ("light", "dark")
        assert resolved.for_mode("light") is resolved.light
        assert resolved.for_mode("dark") is resolved.dark


def test_paired_theme_carries_both_palettes():
    assert is_adaptive(NFCORE_DARK_THEME)
    assert color_pair(NFCORE_DARK_THEME, "background_fill") == (
        NFCORE_LIGHT_THEME.background_fill,
        NFCORE_DARK_THEME.background_fill,
    )


def test_a_theme_with_one_palette_does_not_adapt():
    transparent = THEMES["transparent"]
    assert not is_adaptive(transparent)
    assert color_pair(transparent, "label_color") == (
        transparent.label_color,
        transparent.label_color,
    )


def test_bare_brand_names_resolve_to_default_mode():
    assert THEMES["nfcore"].mode == DEFAULT_MODE
    assert THEMES["seqera"].mode == DEFAULT_MODE
