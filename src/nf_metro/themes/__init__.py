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
from typing import TYPE_CHECKING

from nf_metro.render.style import Theme
from nf_metro.themes.stack import (
    DARK,
    LIGHT,
    MODES,
    ResolvedTheme,
    ThemeError,
    ThemeFile,
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
DEFAULT_MODE = DARK

_BUILTIN_DIR = Path(__file__).parent
_BUILTIN_FILES = {
    "nfcore": "nfcore.yaml",
    "seqera": "seqera.yaml",
    "london-tube": "london_tube.yaml",
    "transparent": "transparent.yaml",
}

# ``dark`` is ``MetroGraph.style``'s default, so a map with no style directive
# arrives as ``dark``. ``light`` names the transparent theme; both names are
# accepted wherever a theme name is.
_ALIASES = {"dark": "nfcore", "light": "transparent"}


def _load_builtin(name: str) -> ThemeLayer:
    loaded = load_theme_file(_BUILTIN_DIR / _BUILTIN_FILES[name])
    if loaded.extends:
        raise ThemeError(f"built-in theme {name!r} may not use extends")
    return loaded.layer


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


def _file_path(entry: str, base_dir: Path) -> Path:
    path = Path(entry).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()


def check_stack(
    value: str, base_dir: Path | str = "", *, must_exist: bool = False
) -> list[str]:
    """Return the entries of a stack, raising :class:`ThemeError` on a bad one.

    Every built-in name is checked. Theme file entries are checked only for
    their suffix unless *must_exist*, which also loads and validates each file
    (relative to *base_dir*, the working directory when empty). Entries are
    checked left to right, so the first bad one is the one reported.
    """
    entries = split_stack(value)
    if not entries:
        raise ThemeError("empty theme stack")
    root = Path(base_dir) if base_dir else Path.cwd()
    for entry in entries:
        if not is_theme_file_entry(entry):
            _parse_name(entry)
        elif must_exist:
            load_theme_file(_file_path(entry, root))
    return entries


def anchor_stack(value: str, base_dir: Path | str) -> str:
    """*value* with each relative theme file made absolute against *base_dir*."""
    root = Path(base_dir)
    return ", ".join(
        str(_file_path(entry, root)) if is_theme_file_entry(entry) else entry
        for entry in check_stack(value)
    )


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


def _expand(
    entry: str, base_dir: Path, seen: tuple[Path, ...]
) -> tuple[list[ThemeLayer | ThemeFile], str | None]:
    """An entry's layers in stack order (its ``extends`` first) and its pin.

    A theme file's ``extends`` entries resolve against that file's folder.
    """
    if not is_theme_file_entry(entry):
        name, pin = _parse_name(entry)
        return [_BUILTIN_LAYERS[name]], pin
    path = _file_path(entry, base_dir)
    if path in seen:
        chain = " -> ".join(str(p) for p in (*seen, path))
        raise ThemeError(f"theme files extend each other in a cycle: {chain}")
    loaded = load_theme_file(path)
    layers: list[ThemeLayer | ThemeFile] = []
    for base in loaded.extends:
        base_layers, _ = _expand(base, path.parent, (*seen, path))
        layers += base_layers
    return [*layers, loaded], None


def resolve_stack(value: str, base_dir: Path | str = "") -> ResolvedTheme:
    """Resolve a stack of theme names and files, laid over :data:`BASE_THEME`.

    Relative theme file entries resolve against *base_dir* (the working
    directory when empty). A mode-suffixed name pins the baked mode; the last
    pin in the stack wins. The result is shared by every resolution of the
    same stack whose files have the same content.
    """
    entries = check_stack(value)
    root = Path(base_dir) if base_dir else Path.cwd()
    parts: list[ThemeLayer | ThemeFile] = []
    names: list[str] = []
    pinned: str | None = None
    for entry in entries:
        entry_parts, pin = _expand(entry, root, ())
        parts += entry_parts
        pinned = pin or pinned
        names.append(entry if is_theme_file_entry(entry) else _parse_name(entry)[0])
    resolved = _merge_cached(tuple(parts), ", ".join(names))
    return dataclasses.replace(resolved, pinned_mode=pinned) if pinned else resolved


@lru_cache(maxsize=128)
def _merge_cached(
    parts: tuple[ThemeLayer | ThemeFile, ...], name: str
) -> ResolvedTheme:
    layers = [_BUILTIN_LAYERS[BASE_THEME]] + [
        part.layer if isinstance(part, ThemeFile) else part for part in parts
    ]
    return merge_layers(layers, name=name)


def resolve_graph_stack(theme: str | None, graph: MetroGraph) -> ResolvedTheme:
    """The stack *graph* renders with: the caller's *theme* if given, else the
    stack it was prepared with, else its ``%%metro style:``.

    A caller's theme files resolve against the working directory and the map's
    against the ``.mmd``'s folder.
    """
    if theme is not None:
        return resolve_stack(theme)
    if graph.theme_stack:
        return resolve_stack(graph.theme_stack, graph.theme_dir)
    return resolve_stack(graph.style, graph.source_dir)


def bind_graph_stack(
    graph: MetroGraph, theme: str | None, source_dir: str
) -> ResolvedTheme:
    """Record on *graph* the stack its render uses, and resolve it.

    The render then reads it back through :func:`resolve_theme` with no
    explicit theme, so a graph is resolved once per preparation.
    """
    if theme is not None:
        graph.theme_stack, graph.theme_dir = theme, ""
    else:
        graph.theme_stack, graph.theme_dir = graph.style, source_dir
    return resolve_stack(graph.theme_stack, graph.theme_dir)


def style_theme(graph: MetroGraph) -> Theme:
    """The theme of *graph*'s own ``%%metro style:``, in its baked mode.

    Layout passes that borrow render geometry measure with this, whatever
    theme the caller renders with.
    """
    return resolve_stack(graph.style, graph.source_dir).for_mode(
        resolve_mode(None, graph)
    )


def resolve_mode(mode: str | None, graph: MetroGraph) -> str:
    """The mode to bake: explicit, else the graph's ``%%metro mode:``, else default."""
    return (mode or graph.mode).strip().lower() or DEFAULT_MODE


def resolve_theme(
    theme: str | None, graph: MetroGraph, mode: str | None = None
) -> Theme:
    """The theme to render *graph* with.

    The stack is the explicit *theme* or the graph's style; the mode is the
    explicit *mode*, the graph's ``%%metro mode:``, or :data:`DEFAULT_MODE`.
    """
    return resolve_graph_stack(theme, graph).for_mode(resolve_mode(mode, graph))


#: Every accepted built-in name resolved at :data:`DEFAULT_MODE`, for callers
#: that need a concrete theme without a graph.
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
    "LIGHT",
    "DARK",
    "PAIRED_THEMES",
    "STYLE_NAMES",
    "THEMES",
    "ResolvedTheme",
    "ThemeError",
    "anchor_stack",
    "bind_graph_stack",
    "check_stack",
    "style_theme",
    "resolve_graph_stack",
    "resolve_mode",
    "resolve_stack",
    "resolve_style",
    "resolve_theme",
    "NFCORE_LIGHT_THEME",
    "NFCORE_DARK_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
    "TRANSPARENT_THEME",
]
