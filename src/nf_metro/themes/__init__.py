"""Theme definitions for metro maps.

Brand and mode are orthogonal axes. ``THEMES`` is the flat by-name registry
(every brand and every concrete variant, for ``--theme``/``style:`` lookup).
``THEME_MODES`` groups brands into their ``{light, dark}`` pairs so a brand can
be resolved against an independently chosen mode, and so the renderer can emit
both palettes from a single render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nf_metro.render.style import Theme
from nf_metro.themes.light import LIGHT_THEME
from nf_metro.themes.nfcore import NFCORE_DARK_THEME, NFCORE_LIGHT_THEME
from nf_metro.themes.seqera import SEQERA_DARK_THEME, SEQERA_LIGHT_THEME

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

# ``dark`` names a mode, not a brand, but it is both an accepted
# ``%%metro style:`` value and ``MetroGraph.style``'s default, so every map
# without a style directive arrives here; map it onto the nfcore brand.
_STYLE_THEME_ALIASES = {"dark": "nfcore"}

# Mode used when a single concrete palette must be baked and none was chosen
# (e.g. PNG raster). Applies equally to every brand - no brand is intrinsically
# light or dark. SVG output carries both palettes and adapts at view time, so
# this only governs raster/standalone fallback.
DEFAULT_MODE = "dark"

# Brand -> mode -> Theme. The renderer reads a resolved theme's ``brand`` here to
# recover both mode palettes for ``light-dark()`` emission; the resolver reads it
# to combine a brand with an independently chosen mode.
THEME_MODES: dict[str, dict[str, Theme]] = {
    "nfcore": {"dark": NFCORE_DARK_THEME, "light": NFCORE_LIGHT_THEME},
    "seqera": {"dark": SEQERA_DARK_THEME, "light": SEQERA_LIGHT_THEME},
}

# Flat by-name registry for direct ``--theme`` / ``style:`` selection. Bare brand
# names resolve to the brand at ``DEFAULT_MODE``; the suffixed names pin a mode.
THEMES = {
    "nfcore": THEME_MODES["nfcore"][DEFAULT_MODE],
    "nfcore-light": NFCORE_LIGHT_THEME,
    "nfcore-dark": NFCORE_DARK_THEME,
    "seqera": THEME_MODES["seqera"][DEFAULT_MODE],
    "seqera-light": SEQERA_LIGHT_THEME,
    "seqera-dark": SEQERA_DARK_THEME,
    "light": LIGHT_THEME,
}


# Accepted ``%%metro style:`` values.
STYLE_NAMES = frozenset(THEMES) | frozenset(_STYLE_THEME_ALIASES)


def resolve_style(style: str) -> str:
    """Return the theme name a ``%%metro style:`` value selects.

    Resolves the alias map and the fallback :func:`resolve_theme` applies to a
    name no registered theme matches, so a caller can report the brand a map
    will actually render with rather than the raw directive value.
    """
    name = style.strip().lower()
    name = _STYLE_THEME_ALIASES.get(name, name)
    return name if name in THEMES else "nfcore"


def resolve_theme(
    theme: str | None, graph: MetroGraph, mode: str | None = None
) -> Theme:
    """Resolve a concrete theme from independent brand and mode axes.

    Brand comes from the explicit ``theme`` name or the graph's style, and
    both go through the same alias map, so a name works identically whichever
    plane supplied it. Mode comes from the explicit argument, the graph
    directive, or ``DEFAULT_MODE``.
    """
    brand = resolve_style(theme if theme is not None else graph.style)

    resolved_mode = (mode or graph.mode).strip().lower() or DEFAULT_MODE
    family = THEME_MODES.get(brand)
    if family and resolved_mode in family:
        return family[resolved_mode]

    return THEMES.get(brand, THEMES["nfcore"])


def mode_pair(theme: Theme) -> tuple[Theme, Theme] | None:
    """Return ``(light_theme, dark_theme)`` for *theme*'s brand family.

    ``None`` when the theme has no registered light/dark family (e.g. the
    transparent ``light`` theme), so callers fall back to a single palette.
    """
    family = THEME_MODES.get(theme.brand)
    if family is None or "light" not in family or "dark" not in family:
        return None
    return family["light"], family["dark"]


__all__ = [
    "THEMES",
    "STYLE_NAMES",
    "THEME_MODES",
    "DEFAULT_MODE",
    "resolve_style",
    "resolve_theme",
    "mode_pair",
    "LIGHT_THEME",
    "NFCORE_LIGHT_THEME",
    "NFCORE_DARK_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
]
