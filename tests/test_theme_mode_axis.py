"""Tests for the independent theme (brand) and mode (light/dark) axes."""

from __future__ import annotations

import warnings

import pytest

from nf_metro.api import resolve_theme
from nf_metro.parser import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.themes import (
    NFCORE_LIGHT_THEME,
    SEQERA_DARK_THEME,
    THEME_FAMILIES,
    THEMES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_MMD = """\
%%metro line: l1 | Line 1 | #ff0000
graph LR
    subgraph s1 [S1]
        %%metro entry: left | l1
        %%metro exit: right | l1
        a[A]
        a -->|l1| b[B]
    end
"""


def _graph(**overrides: str) -> MetroGraph:
    g = MetroGraph()
    for k, v in overrides.items():
        setattr(g, k, v)
    return g


# ---------------------------------------------------------------------------
# Style-only resolution (no explicit mode)
# ---------------------------------------------------------------------------


def test_default_resolves_to_nfcore_dark() -> None:
    assert resolve_theme(None, _graph()).name == "nfcore"


def test_style_dark_alias_resolves_to_nfcore() -> None:
    assert resolve_theme(None, _graph(style="dark")).name == "nfcore"


def test_style_nfcore_resolves_to_nfcore_dark() -> None:
    assert resolve_theme(None, _graph(style="nfcore")).name == "nfcore"


def test_style_seqera_resolves_to_seqera_light() -> None:
    assert resolve_theme(None, _graph(style="seqera")).name == "seqera"


def test_style_light_resolves_to_transparent_light() -> None:
    assert resolve_theme(None, _graph(style="light")).name == "light"


def test_explicit_theme_arg_overrides_directive() -> None:
    g = _graph(style="seqera")
    assert resolve_theme("nfcore", g).name == "nfcore"


# ---------------------------------------------------------------------------
# Mode is independent of brand: mixing them works as expected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "brand, mode, expected",
    [
        ("nfcore", "dark", "nfcore"),
        ("nfcore", "light", "nfcore-light"),
        ("seqera", "light", "seqera"),
        ("seqera", "dark", "seqera-dark"),
    ],
)
def test_brand_mode_combinations(brand: str, mode: str, expected: str) -> None:
    """Brand and mode are resolved independently for every (brand, mode) pair."""
    g = _graph(style=brand)
    assert resolve_theme(None, g, mode=mode).name == expected


def test_explicit_theme_arg_with_mode() -> None:
    g = _graph()
    assert resolve_theme("nfcore", g, mode="light").name == "nfcore-light"
    assert resolve_theme("seqera", g, mode="dark").name == "seqera-dark"


def test_mode_directive_on_graph() -> None:
    g = _graph(style="nfcore", mode="light")
    assert resolve_theme(None, g).name == "nfcore-light"


def test_cli_mode_beats_directive_mode() -> None:
    g = _graph(style="nfcore", mode="light")
    assert resolve_theme(None, g, mode="dark").name == "nfcore"


def test_nfcore_brand_does_not_force_dark() -> None:
    """Choosing the nfcore brand should not lock in dark mode."""
    g = _graph(style="nfcore", mode="light")
    theme = resolve_theme(None, g)
    assert theme.name == "nfcore-light"
    assert "#f5f5f5" in theme.background_color  # solid, not dark


def test_seqera_brand_does_not_force_light() -> None:
    """Choosing the seqera brand should not lock in light mode."""
    g = _graph(style="seqera", mode="dark")
    theme = resolve_theme(None, g)
    assert theme.name == "seqera-dark"
    assert "#1a1625" in theme.background_color  # dark, not light


# ---------------------------------------------------------------------------
# New theme objects exist and have sensible properties
# ---------------------------------------------------------------------------


def test_nfcore_light_has_solid_background() -> None:
    assert NFCORE_LIGHT_THEME.background_color not in ("none", "", "transparent")
    assert NFCORE_LIGHT_THEME.background_color.startswith("#")


def test_seqera_dark_has_dark_background() -> None:
    assert SEQERA_DARK_THEME.background_color.startswith("#")
    assert SEQERA_DARK_THEME.background_color != THEMES["seqera"].background_color


def test_theme_families_cover_both_modes() -> None:
    for brand, family in THEME_FAMILIES.items():
        assert "light" in family, f"{brand} missing light variant"
        assert "dark" in family, f"{brand} missing dark variant"
        assert family["light"].name != family["dark"].name


def test_new_themes_in_themes_dict() -> None:
    assert "nfcore-light" in THEMES
    assert "seqera-dark" in THEMES


# ---------------------------------------------------------------------------
# %%metro mode: directive
# ---------------------------------------------------------------------------


def test_mode_directive_parsed() -> None:
    mmd = "%%metro mode: light\n" + _MINIMAL_MMD
    g = parse_metro_mermaid(mmd)
    assert g.mode == "light"


def test_mode_directive_dark() -> None:
    mmd = "%%metro mode: dark\n" + _MINIMAL_MMD
    g = parse_metro_mermaid(mmd)
    assert g.mode == "dark"


def test_mode_directive_invalid_warns_and_ignores() -> None:
    mmd = "%%metro mode: vivid\n" + _MINIMAL_MMD
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        g = parse_metro_mermaid(mmd)
    assert g.mode == ""
    assert any("mode" in str(w.message).lower() for w in caught)


def test_mode_directive_end_to_end() -> None:
    """%%metro mode: light + style: nfcore resolves to nfcore-light."""
    mmd = "%%metro style: nfcore\n%%metro mode: light\n" + _MINIMAL_MMD
    g = parse_metro_mermaid(mmd)
    assert resolve_theme(None, g).name == "nfcore-light"
