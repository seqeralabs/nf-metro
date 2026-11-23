"""Theme definitions for metro maps.

Brand and mode are orthogonal axes. ``THEMES`` is the flat by-name registry
(every brand and every concrete variant, for ``--theme``/``style:`` lookup).
``THEME_MODES`` groups brands into their ``{light, dark}`` pairs so a brand can
be resolved against an independently chosen mode, and so the renderer can emit
both palettes from a single render.

A ``style:``/``--theme`` value may also be a stack: comma-separated built-in
names and YAML theme files, merged left to right (see
:mod:`nf_metro.themes.stack`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nf_metro.render.style import Theme
from nf_metro.themes.light import LIGHT_THEME
from nf_metro.themes.nfcore import NFCORE_DARK_THEME, NFCORE_LIGHT_THEME
from nf_metro.themes.seqera import SEQERA_DARK_THEME, SEQERA_LIGHT_THEME
from nf_metro.themes.stack import (
    ThemeError,
    ThemeLayer,
    is_theme_file_entry,
    layer_from_family,
    load_theme_file,
    merge_layers,
    split_stack,
)

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

# The brand every stack is laid over, so a stack of partial themes sets every
# field.
_BASE_BRAND = "nfcore"

_BUILTIN_DIR = Path(__file__).parent

# Brand -> mode -> Theme. The renderer reads a resolved theme's ``brand`` here to
# recover both mode palettes for ``light-dark()`` emission; the resolver reads it
# to combine a brand with an independently chosen mode.
THEME_MODES: dict[str, dict[str, Theme]] = {
    "nfcore": {"dark": NFCORE_DARK_THEME, "light": NFCORE_LIGHT_THEME},
    "seqera": {"dark": SEQERA_DARK_THEME, "light": SEQERA_LIGHT_THEME},
}

# Built-in themes written as theme files, as the partial layers they stack as.
_BUILTIN_FILE_LAYERS: dict[str, ThemeLayer] = {}


def _builtin_layer(name: str) -> ThemeLayer:
    if name in _BUILTIN_FILE_LAYERS:
        return _BUILTIN_FILE_LAYERS[name]
    family = THEME_MODES[name]
    return layer_from_family(name, family["light"], family["dark"])


def _register_builtin_file(filename: str) -> None:
    layer, extends = load_theme_file(_BUILTIN_DIR / filename)
    layers = [_builtin_layer(_BASE_BRAND)]
    layers += [_builtin_layer(base) for base in extends]
    layers.append(layer)
    light, dark, _ = merge_layers(layers, name=layer.name, brand=layer.name)
    THEME_MODES[layer.name] = {"dark": dark, "light": light}
    _BUILTIN_FILE_LAYERS[layer.name] = layer


_register_builtin_file("london_tube.yaml")

# Flat by-name registry for direct ``--theme`` / ``style:`` selection. Bare brand
# names resolve to the brand at ``DEFAULT_MODE``; the suffixed names pin a mode.
THEMES: dict[str, Theme] = {"light": LIGHT_THEME}
for _brand, _family in THEME_MODES.items():
    THEMES[_brand] = _family[DEFAULT_MODE]
    THEMES[f"{_brand}-light"] = _family["light"]
    THEMES[f"{_brand}-dark"] = _family["dark"]


# Accepted single-entry ``%%metro style:`` values.
STYLE_NAMES = frozenset(THEMES) | frozenset(_STYLE_THEME_ALIASES)

# Families of resolved multi-entry stacks, so :func:`mode_pair` can recover
# both palettes of a stacked theme the same way it does a brand's.
_STACK_FAMILIES: dict[str, dict[str, Theme]] = {}


def _canonical_name(entry: str) -> str:
    name = entry.strip().lower()
    return _STYLE_THEME_ALIASES.get(name, name)


def check_stack(value: str) -> list[str]:
    """Return the entries of a stack, raising :class:`ThemeError` on an unknown name.

    Theme file entries are only checked for their suffix here; whether the
    file exists and is valid is checked when the stack resolves.
    """
    entries = split_stack(value)
    if not entries:
        raise ThemeError("empty theme stack")
    for entry in entries:
        if not is_theme_file_entry(entry) and _canonical_name(entry) not in STYLE_NAMES:
            raise ThemeError(
                f"unknown theme {entry!r}; expected one of "
                + ", ".join(sorted(STYLE_NAMES))
                + ", or a .yaml theme file"
            )
    return entries


def resolve_style(style: str) -> str:
    """Return the theme name a ``%%metro style:`` value selects.

    Resolves the alias map and the fallback :func:`resolve_theme` applies to a
    name no registered theme matches, so a caller can report the brand a map
    will actually render with rather than the raw directive value. A stack
    reports its entries, normalised and comma-joined.
    """
    entries = split_stack(style)
    if len(entries) > 1:
        return ", ".join(
            e if is_theme_file_entry(e) else _canonical_name(e) for e in entries
        )
    name = _canonical_name(style)
    return name if name in THEMES else "nfcore"


@dataclass(frozen=True)
class ResolvedStack:
    """A stack resolved to its light and dark themes and its layout options."""

    light: Theme
    dark: Theme
    layout: dict[str, Any]


def _entry_layers(
    entry: str, base_dir: Path, seen: tuple[Path, ...]
) -> list[ThemeLayer]:
    if not is_theme_file_entry(entry):
        name = _canonical_name(entry)
        if name in THEME_MODES:
            return [_builtin_layer(name)]
        if name in THEMES:
            theme = THEMES[name]
            return [layer_from_family(name, theme, theme)]
        raise ThemeError(f"unknown theme {entry!r}")
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if path in seen:
        chain = " -> ".join(str(p) for p in (*seen, path))
        raise ThemeError(f"theme files extend each other in a cycle: {chain}")
    layer, extends = load_theme_file(path)
    layers: list[ThemeLayer] = []
    for base in extends:
        layers += _entry_layers(base, path.parent, (*seen, path))
    return [*layers, layer]


def _stack_key(entries: list[str], base_dir: Path) -> str:
    parts = []
    for entry in entries:
        if is_theme_file_entry(entry):
            path = Path(entry).expanduser()
            parts.append(str(path if path.is_absolute() else (base_dir / path)))
        else:
            parts.append(_canonical_name(entry))
    return ",".join(parts)


def resolve_stack(value: str, base_dir: Path | str = "") -> ResolvedStack:
    """Merge the stack *value* over the base brand.

    Relative theme file entries resolve against *base_dir* (the working
    directory when empty).
    """
    entries = check_stack(value)
    root = Path(base_dir) if base_dir else Path.cwd()
    if len(entries) == 1 and not is_theme_file_entry(entries[0]):
        name = _canonical_name(entries[0])
        if name in THEME_MODES:
            family = THEME_MODES[name]
            layout = (
                _BUILTIN_FILE_LAYERS[name].layout
                if name in _BUILTIN_FILE_LAYERS
                else {}
            )
            return ResolvedStack(family["light"], family["dark"], dict(layout))
        theme = THEMES[name]
        return ResolvedStack(theme, theme, {})
    layers = [_builtin_layer(_BASE_BRAND)]
    for entry in entries:
        layers += _entry_layers(entry, root, ())
    brand = "stack:" + _stack_key(entries, root)
    light, dark, layout = merge_layers(layers, name=", ".join(entries), brand=brand)
    _STACK_FAMILIES[brand] = {"light": light, "dark": dark}
    return ResolvedStack(light, dark, layout)


def _stack_source(theme: str | None, graph: MetroGraph) -> tuple[str, str]:
    """The stack a render uses and the directory its relative files resolve against."""
    if theme is not None:
        return theme, ""
    return graph.style, graph.source_dir


def theme_layout_options(theme: str | None, graph: MetroGraph) -> dict[str, Any]:
    """Layout options the render's theme stack sets."""
    value, base_dir = _stack_source(theme, graph)
    return resolve_stack(value, base_dir).layout


def resolve_theme(
    theme: str | None, graph: MetroGraph, mode: str | None = None
) -> Theme:
    """Resolve a concrete theme from independent brand and mode axes.

    Brand comes from the explicit ``theme`` stack or the graph's style, and
    both go through the same alias map, so a name works identically whichever
    plane supplied it. Mode comes from the explicit argument, the graph
    directive, or ``DEFAULT_MODE``.
    """
    value, base_dir = _stack_source(theme, graph)
    resolved_mode = (mode or graph.mode).strip().lower() or DEFAULT_MODE
    entries = split_stack(value)
    if len(entries) <= 1 and not (entries and is_theme_file_entry(entries[0])):
        brand = resolve_style(entries[0] if entries else value)
        family = THEME_MODES.get(brand)
        if family and resolved_mode in family:
            return family[resolved_mode]
        return THEMES.get(brand, THEMES["nfcore"])
    stack = resolve_stack(value, base_dir)
    return stack.light if resolved_mode == "light" else stack.dark


def mode_pair(theme: Theme) -> tuple[Theme, Theme] | None:
    """Return ``(light_theme, dark_theme)`` for *theme*'s brand family.

    ``None`` when the theme has no registered light/dark family (e.g. the
    transparent ``light`` theme), so callers fall back to a single palette.
    """
    family = THEME_MODES.get(theme.brand) or _STACK_FAMILIES.get(theme.brand)
    if family is None or "light" not in family or "dark" not in family:
        return None
    return family["light"], family["dark"]


__all__ = [
    "THEMES",
    "STYLE_NAMES",
    "THEME_MODES",
    "DEFAULT_MODE",
    "ResolvedStack",
    "ThemeError",
    "check_stack",
    "resolve_stack",
    "resolve_style",
    "resolve_theme",
    "theme_layout_options",
    "mode_pair",
    "LIGHT_THEME",
    "NFCORE_LIGHT_THEME",
    "NFCORE_DARK_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
]
