"""Built-in themes and theme resolution.

Every built-in theme is a YAML file in this package, loaded and validated the
same way as a user's theme file. A ``%%metro style:`` or ``--theme`` value is a
stack of theme names and files (a single name is a stack of one), resolved by
:func:`resolve_stack` to a :class:`ResolvedTheme` carrying the light theme, the
dark theme, and the stack's layout options. See :mod:`nf_metro.themes.stack`.
"""

from __future__ import annotations

import dataclasses
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nf_metro.render.style import Theme
from nf_metro.themes.stack import (
    MODES,
    ResolvedTheme,
    ThemeError,
    ThemeLayer,
    is_theme_file_entry,
    load_theme_file,
    merge_layers,
    split_stack,
)

if TYPE_CHECKING:
    from nf_metro.parser.model import MetroGraph

#: The theme every stack starts from, and the look of a map with no style.
BASE_THEME = "nfcore"

#: Mode baked when none is chosen (e.g. PNG export). SVG output carries both
#: palettes and adapts at view time, so this governs only a baked render.
DEFAULT_MODE = "dark"

_BUILTIN_DIR = Path(__file__).parent
_BUILTIN_FILES = {
    "nfcore": "nfcore.yaml",
    "seqera": "seqera.yaml",
    "london-tube": "london_tube.yaml",
    "transparent": "transparent.yaml",
}

# ``dark`` is ``MetroGraph.style``'s default and a legacy style value; ``light``
# is the old name of the transparent theme, which collides with the mode.
_ALIASES = {"dark": "nfcore", "light": "transparent"}


def _load_builtin(name: str) -> ThemeLayer:
    layer, extends = load_theme_file(_BUILTIN_DIR / _BUILTIN_FILES[name])
    if extends:
        raise ThemeError(f"built-in theme {name!r} may not use extends")
    return layer


_BUILTIN_LAYERS: dict[str, ThemeLayer] = {
    name: _load_builtin(name) for name in _BUILTIN_FILES
}

#: Built-in themes with both palettes, which also accept a ``-light`` or
#: ``-dark`` suffix to pin the baked mode.
PAIRED_THEMES = tuple(
    name for name, layer in _BUILTIN_LAYERS.items() if layer.fixed_mode is None
)

#: Every accepted built-in name: themes, mode-pinned variants, and aliases.
STYLE_NAMES = frozenset(
    [*_BUILTIN_LAYERS, *_ALIASES]
    + [f"{name}-{mode}" for name in PAIRED_THEMES for mode in MODES]
)


def _parse_name(entry: str) -> tuple[str, str | None]:
    """A built-in entry's canonical theme name and the mode it pins, if any."""
    name = entry.strip().lower()
    name = _ALIASES.get(name, name)
    if name in _BUILTIN_LAYERS:
        return name, None
    for mode in MODES:
        base = name.removesuffix(f"-{mode}")
        if base != name and base in PAIRED_THEMES:
            return base, mode
    raise ThemeError(
        f"unknown theme {entry!r}; expected one of "
        + ", ".join(sorted(STYLE_NAMES))
        + ", or a .yaml theme file"
    )


def check_stack(value: str) -> list[str]:
    """Return the entries of a stack, raising :class:`ThemeError` on an unknown name.

    Theme file entries are only checked for their suffix here; whether the
    file exists and is valid is checked when the stack resolves.
    """
    entries = split_stack(value)
    if not entries:
        raise ThemeError("empty theme stack")
    for entry in entries:
        if not is_theme_file_entry(entry):
            _parse_name(entry)
    return entries


def resolve_style(style: str) -> str:
    """The canonical spelling of a ``style``/``--theme`` value.

    Aliases resolve to the theme they name and entries are comma-joined, so a
    caller can report the theme a map renders with.
    """
    entries = check_stack(style)
    return ", ".join(
        entry
        if is_theme_file_entry(entry)
        else "-".join(filter(None, _parse_name(entry)))
        for entry in entries
    )


def _entry_layers(
    entry: str, base_dir: Path, seen: tuple[Path, ...]
) -> tuple[list[ThemeLayer], str | None]:
    if not is_theme_file_entry(entry):
        name, pin = _parse_name(entry)
        return [_BUILTIN_LAYERS[name]], pin
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
        base_layers, _ = _entry_layers(base, path.parent, (*seen, path))
        layers += base_layers
    return [*layers, layer], None


def resolve_stack(value: str, base_dir: Path | str = "") -> ResolvedTheme:
    """Resolve a stack of theme names and files, laid over :data:`BASE_THEME`.

    Relative theme file entries resolve against *base_dir* (the working
    directory when empty). A mode-suffixed name pins the baked mode; the last
    pin in the stack wins. A stack of built-in names resolves to the same
    object every time.
    """
    entries = check_stack(value)
    if not any(is_theme_file_entry(entry) for entry in entries):
        return _resolve_builtin_stack(resolve_style(value))
    return _resolve(entries, Path(base_dir) if base_dir else Path.cwd())


def _resolve_builtin_stack(canonical: str) -> ResolvedTheme:
    names: list[str] = []
    pinned: str | None = None
    for entry in split_stack(canonical):
        name, pin = _parse_name(entry)
        names.append(name)
        pinned = pin or pinned
    resolved = _resolve_builtin_names(", ".join(names))
    return dataclasses.replace(resolved, pinned_mode=pinned) if pinned else resolved


@lru_cache(maxsize=128)
def _resolve_builtin_names(names: str) -> ResolvedTheme:
    return _resolve(split_stack(names), Path.cwd())


def _resolve(entries: list[str], root: Path) -> ResolvedTheme:
    layers = [_BUILTIN_LAYERS[BASE_THEME]]
    pinned: str | None = None
    for entry in entries:
        entry_layers, pin = _entry_layers(entry, root, ())
        layers += entry_layers
        pinned = pin or pinned
    return merge_layers(layers, name=", ".join(entries), pinned_mode=pinned)


def _stack_source(theme: str | None, graph: MetroGraph) -> tuple[str, str]:
    """The stack a render uses and the directory its relative files resolve against."""
    if theme is not None:
        return theme, ""
    return graph.style, graph.source_dir


def resolve_graph_stack(theme: str | None, graph: MetroGraph) -> ResolvedTheme:
    """The caller's stack (``--theme``) if given, else the map's ``%%metro style:``."""
    return resolve_stack(*_stack_source(theme, graph))


def theme_layout_options(theme: str | None, graph: MetroGraph) -> dict[str, Any]:
    """Layout options the render's theme stack sets."""
    return dict(resolve_graph_stack(theme, graph).layout)


def resolve_theme(
    theme: str | None, graph: MetroGraph, mode: str | None = None
) -> Theme:
    """The theme to render *graph* with.

    The stack is the explicit *theme* or the graph's style; the mode is the
    explicit *mode*, the graph's ``%%metro mode:``, or :data:`DEFAULT_MODE`.
    """
    resolved_mode = (mode or graph.mode).strip().lower() or DEFAULT_MODE
    return resolve_graph_stack(theme, graph).for_mode(resolved_mode)


#: Every accepted built-in name resolved at :data:`DEFAULT_MODE`, for callers
#: that need a concrete theme without a graph (e.g. layout's icon obstacles).
THEMES: dict[str, Theme] = {
    name: resolve_stack(name).for_mode(DEFAULT_MODE) for name in sorted(STYLE_NAMES)
}

NFCORE_LIGHT_THEME = THEMES["nfcore-light"]
NFCORE_DARK_THEME = THEMES["nfcore-dark"]
SEQERA_LIGHT_THEME = THEMES["seqera-light"]
SEQERA_DARK_THEME = THEMES["seqera-dark"]
TRANSPARENT_THEME = THEMES["transparent"]


__all__ = [
    "BASE_THEME",
    "DEFAULT_MODE",
    "PAIRED_THEMES",
    "STYLE_NAMES",
    "THEMES",
    "ResolvedTheme",
    "ThemeError",
    "check_stack",
    "resolve_graph_stack",
    "resolve_stack",
    "resolve_style",
    "resolve_theme",
    "theme_layout_options",
    "NFCORE_LIGHT_THEME",
    "NFCORE_DARK_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
    "TRANSPARENT_THEME",
]
