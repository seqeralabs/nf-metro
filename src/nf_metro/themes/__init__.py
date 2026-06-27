"""Theme definitions for metro maps."""

from nf_metro.render.style import Theme
from nf_metro.themes.light import LIGHT_THEME
from nf_metro.themes.nfcore import NFCORE_THEME
from nf_metro.themes.nfcore_light import NFCORE_LIGHT_THEME
from nf_metro.themes.seqera import SEQERA_THEME
from nf_metro.themes.seqera_dark import SEQERA_DARK_THEME

THEMES = {
    "nfcore": NFCORE_THEME,
    "light": LIGHT_THEME,
    "seqera": SEQERA_THEME,
    "nfcore-light": NFCORE_LIGHT_THEME,
    "seqera-dark": SEQERA_DARK_THEME,
}

# Maps brand name -> mode -> Theme. Used when both a brand and a mode are
# resolved independently (via ``%%metro mode:`` or ``--mode``). Brand choice
# has no bearing on which mode is used; mode is a completely separate axis.
THEME_FAMILIES: dict[str, dict[str, Theme]] = {
    "nfcore": {"dark": NFCORE_THEME, "light": NFCORE_LIGHT_THEME},
    "seqera": {"light": SEQERA_THEME, "dark": SEQERA_DARK_THEME},
}

__all__ = [
    "THEME_FAMILIES",
    "THEMES",
    "LIGHT_THEME",
    "NFCORE_LIGHT_THEME",
    "NFCORE_THEME",
    "SEQERA_DARK_THEME",
    "SEQERA_THEME",
]
