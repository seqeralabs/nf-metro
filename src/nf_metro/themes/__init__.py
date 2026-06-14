"""Theme definitions for metro maps."""

from nf_metro.themes.light import LIGHT_THEME
from nf_metro.themes.nfcore import NFCORE_THEME
from nf_metro.themes.seqera import SEQERA_THEME

THEMES = {
    "nfcore": NFCORE_THEME,
    "light": LIGHT_THEME,
    "seqera": SEQERA_THEME,
}

__all__ = ["THEMES", "NFCORE_THEME", "LIGHT_THEME", "SEQERA_THEME"]
