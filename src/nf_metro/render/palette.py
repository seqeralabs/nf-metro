"""Line colours from a theme's palette, extended past its end when a map has
more lines than the palette has colours.

An extra colour is the in-gamut candidate farthest, in OKLab, from every
colour already in use (the palette, earlier extras, the background and the
inactive grey) among those reaching 3:1 against the light background. In dark
mode each extra keeps its hue, lightened where needed to 3:1 against the
dark background, so a line keeps its identity across modes.
"""

from __future__ import annotations

import colorsys
import math
from functools import lru_cache
from typing import TYPE_CHECKING

from PIL import ImageColor

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroLine
    from nf_metro.render.style import Theme

__all__ = [
    "color_pair",
    "contrast_ratio",
    "extended_palette",
    "line_color",
    "line_color_pair",
    "luminance",
]

_MIN_CONTRAST = 3.0
_LIGHT_FALLBACK = "#ffffff"
_DARK_FALLBACK = "#000000"


def _rgb(color: str) -> tuple[float, float, float] | None:
    try:
        r, g, b = ImageColor.getrgb(color)[:3]
    except ValueError:
        return None
    return r / 255, g / 255, b / 255


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(c * 255):02X}" for c in rgb)


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def luminance(color: str) -> float:
    """WCAG relative luminance of *color*; 0 for a value that is not a colour."""
    rgb = _rgb(color)
    return 0.0 if rgb is None else _luminance(rgb)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between colours *a* and *b*."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _oklab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_linear(c) for c in rgb)
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _from_oklch(lightness: float, chroma: float, hue: float) -> tuple[float, ...]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(
        12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in linear
    )


@lru_cache(maxsize=1)
def _candidates() -> tuple[tuple[float, float, float], ...]:
    """In-gamut colours across the OKLCh hue circle at line-weight lightness
    and chroma, in a fixed order so picks are deterministic."""
    out = []
    for lightness in (0.45, 0.52, 0.59, 0.66, 0.73):
        for chroma in (0.10, 0.14, 0.18):
            for hue in range(0, 360, 10):
                rgb = _from_oklch(lightness, chroma, hue)
                if all(-1e-6 <= c <= 1 + 1e-6 for c in rgb):
                    clamped = tuple(min(1.0, max(0.0, c)) for c in rgb)
                    out.append((clamped[0], clamped[1], clamped[2]))
    return tuple(out)


# Extras are computed this many at a time; picks are prefix-stable, so one
# block serves every line index inside it.
_EXTRA_BLOCK = 16


@lru_cache(maxsize=64)
def _extras(
    palette: tuple[str, ...], background: str, avoid: tuple[str, ...], count: int
) -> tuple[str, ...]:
    bg = _rgb(background)
    if bg is None:
        return ()
    used = [_oklab(rgb) for c in (*palette, background, *avoid) if (rgb := _rgb(c))]
    pool = [c for c in _candidates() if _contrast(c, bg) >= _MIN_CONTRAST]
    labs = [_oklab(c) for c in pool]
    nearest = [min(math.dist(lab, u) for u in used) for lab in labs]
    picks: list[str] = []
    while len(picks) < count and pool:
        best = max(range(len(pool)), key=nearest.__getitem__)
        chosen = labs[best]
        picks.append(_hex(pool[best]))
        del pool[best], labs[best], nearest[best]
        nearest = [min(d, math.dist(lab, chosen)) for d, lab in zip(nearest, labs)]
    return tuple(picks)


def extended_palette(
    palette: tuple[str, ...], background: str, avoid: tuple[str, ...], count: int
) -> tuple[str, ...]:
    """*palette* extended to *count* colours, each extra reaching 3:1 against
    *background* and as far as possible from every colour in use and *avoid*."""
    need = count - len(palette)
    if need <= 0:
        return palette
    block = -(-need // _EXTRA_BLOCK) * _EXTRA_BLOCK
    return palette + _extras(palette, background, avoid, block)[:need]


def _lighten_to(color: str, background: str) -> str:
    """*color* lightened, keeping its hue, until it reaches 3:1 against
    *background*."""
    rgb, bg = _rgb(color), _rgb(background)
    if rgb is None or bg is None or _contrast(rgb, bg) >= _MIN_CONTRAST:
        return color
    hue, lightness, saturation = colorsys.rgb_to_hls(*rgb)
    while lightness < 1.0:
        lightness = min(1.0, lightness + 0.01)
        candidate = colorsys.hls_to_rgb(hue, lightness, saturation)
        if _contrast(candidate, bg) >= _MIN_CONTRAST:
            return _hex(candidate)
    return _hex(colorsys.hls_to_rgb(hue, lightness, saturation))


def color_pair(theme: Theme, name: str) -> tuple[str, str]:
    """``(light, dark)`` values of colour field *name*."""
    for field, light, dark in theme.mode_colors:
        if field == name:
            return light, dark
    value: str = getattr(theme, name)
    return value, value


def _opaque(color: str, fallback: str) -> str:
    return color if _rgb(color) is not None else fallback


def line_color_pair(theme: Theme, line: MetroLine) -> tuple[str, str]:
    """``(light, dark)`` colour of *line* under *theme*: its palette colour by
    declaration order, an extension colour past the palette's end, or its own
    colour when the palette for that mode is empty."""
    light, dark = theme.mode_line_colors
    index = line.palette_index
    base = light or dark
    if not base:
        return line.color, line.color
    bg_light, bg_dark = color_pair(theme, "background_fill")
    bg_light = _opaque(bg_light, _LIGHT_FALLBACK)
    bg_dark = _opaque(bg_dark, _DARK_FALLBACK)
    avoid = color_pair(theme, "muted_line_color")

    def pick(palette: tuple[str, ...], dark_mode: bool) -> str:
        if not palette:
            return line.color
        if index < len(palette):
            return palette[index]
        extra = extended_palette(base, bg_light, avoid, index + 1)[index]
        return _lighten_to(extra, bg_dark) if dark_mode else extra

    return pick(light, False), pick(dark, True)


def line_color(theme: Theme, line: MetroLine) -> str:
    """*line*'s colour in the mode *theme* is baked for."""
    light, dark = line_color_pair(theme, line)
    return light if theme.mode == "light" else dark
