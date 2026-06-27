"""Theme definitions for metro maps.

Brand and mode are orthogonal axes. ``THEMES`` is the flat by-name registry
(every brand and every concrete variant, for ``--theme``/``style:`` lookup).
``THEME_MODES`` groups brands into their ``{light, dark}`` pairs so a brand can
be resolved against an independently chosen mode, and so the renderer can emit
both palettes from a single render.
"""

from nf_metro.render.style import Theme
from nf_metro.themes.light import LIGHT_THEME
from nf_metro.themes.nfcore import (
    NFCORE_DARK_THEME,
    NFCORE_LIGHT_THEME,
    NFCORE_THEME,
)
from nf_metro.themes.seqera import (
    SEQERA_DARK_THEME,
    SEQERA_LIGHT_THEME,
    SEQERA_THEME,
)

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
    "THEME_MODES",
    "DEFAULT_MODE",
    "mode_pair",
    "LIGHT_THEME",
    "NFCORE_THEME",
    "NFCORE_DARK_THEME",
    "NFCORE_LIGHT_THEME",
    "SEQERA_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
]
