"""Built-in themes and theme resolution.

Every built-in theme is a YAML file in this package, in the same format as a
user's theme file. A ``%%metro style:`` or ``--theme`` value is a stack of
theme names and files (a single name is a stack of one), resolved by
:func:`resolve_stack` to a :class:`ResolvedTheme` carrying the light theme, the
dark theme, and the stack's layout options. See :mod:`nf_metro.themes.stack`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nf_metro.render.style import Theme
from nf_metro.themes.stack import (
    DARK,
    LIGHT,
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
BASE_THEME = "default"

#: Mode baked when none is chosen (e.g. PNG export). SVG output carries both
#: palettes and adapts at view time, so this governs only a baked render.
DEFAULT_MODE = DARK

_BUILTIN_DIR = Path(__file__).parent
_BUILTIN_FILES = {
    "default": "default.yaml",
    "seqera": "seqera.yaml",
    "london-tube": "london_tube.yaml",
    "transparent": "transparent.yaml",
}

#: Every built-in theme name.
STYLE_NAMES = frozenset(_BUILTIN_FILES)


@lru_cache(maxsize=None)
def _builtin(name: str) -> ThemeLayer:
    return load_theme_file(_BUILTIN_DIR / _BUILTIN_FILES[name], validate=False)


#: Names earlier releases accepted, and what replaces each.
_RENAMED_STYLES = {
    "nfcore": "use 'default'",
    "nfcore-light": "use 'default' with '--mode light' or '%%metro mode: light'",
    "nfcore-dark": "use 'default' with '--mode dark' or '%%metro mode: dark'",
    "seqera-light": "use 'seqera' with '--mode light' or '%%metro mode: light'",
    "seqera-dark": "use 'seqera' with '--mode dark' or '%%metro mode: dark'",
    "dark": "it is a mode: use '--mode dark' or '%%metro mode: dark'",
    "light": "use 'transparent' for the transparent theme, or '--mode light' "
    "or '%%metro mode: light' for a light palette",
}


def renamed_style_hint(value: str) -> str:
    """What replaces each retired name in the stack *value*, or ``""``."""
    hints = [
        f"{entry.strip()!r} is no longer a theme name: "
        f"{_RENAMED_STYLES[entry.strip().lower()]}"
        for entry in value.split(",")
        if entry.strip().lower() in _RENAMED_STYLES
    ]
    return "; ".join(hints)


def _builtin_name(entry: str) -> str:
    name = entry.strip().lower()
    if name not in _BUILTIN_FILES:
        hint = renamed_style_hint(entry)
        raise ThemeError(
            f"unknown theme {entry!r}; expected one of "
            + ", ".join(sorted(STYLE_NAMES))
            + ", or a .yaml theme file"
            + (f" ({hint})" if hint else "")
        )
    return name


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
            _builtin_name(entry)
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


def _expand(entry: str, base_dir: Path, seen: tuple[Path, ...]) -> list[ThemeLayer]:
    """An entry's layers in stack order, its ``extends`` first.

    A theme file's ``extends`` entries resolve against that file's folder.
    """
    if not is_theme_file_entry(entry):
        return [_builtin(_builtin_name(entry))]
    path = _file_path(entry, base_dir)
    if path in seen:
        chain = " -> ".join(str(p) for p in (*seen, path))
        raise ThemeError(f"theme files extend each other in a cycle: {chain}")
    layer = load_theme_file(path)
    layers: list[ThemeLayer] = []
    for base in layer.extends:
        layers += _expand(base, path.parent, (*seen, path))
    return [*layers, layer]


def resolve_stack(value: str, base_dir: Path | str = "") -> ResolvedTheme:
    """Resolve a stack of theme names and files, laid over :data:`BASE_THEME`.

    Relative theme file entries resolve against *base_dir* (the working
    directory when empty). The result is shared by every resolution of the
    same stack whose files have the same content.
    """
    root = Path(base_dir) if base_dir else Path.cwd()
    layers: list[ThemeLayer] = []
    names: list[str] = []
    for entry in check_stack(value):
        layers += _expand(entry, root, ())
        names.append(entry if is_theme_file_entry(entry) else _builtin_name(entry))
    return _merge_cached(tuple(layers), ", ".join(names))


@lru_cache(maxsize=128)
def _merge_cached(layers: tuple[ThemeLayer, ...], name: str) -> ResolvedTheme:
    return merge_layers([_builtin(BASE_THEME), *layers], name=name)


def bound_stack(graph: MetroGraph, theme: str | None = None) -> ResolvedTheme:
    """The stack *graph* lays out and renders with, bound on the graph.

    A caller's *theme*, its files relative to the working directory, binds in
    place of any earlier stack. Without one the bound stack stays, and an
    unbound graph binds its ``%%metro style:``, its files relative to the
    map's folder.
    """
    if theme is not None:
        graph.resolved_theme = resolve_stack(theme)
    elif graph.resolved_theme is None:
        graph.resolved_theme = resolve_stack(graph.style, graph.source_dir)
    return graph.resolved_theme


def resolve_mode(mode: str | None, graph: MetroGraph) -> str:
    """The mode to bake: explicit, else the graph's ``%%metro mode:``, else default."""
    return (mode or graph.mode).strip().lower() or DEFAULT_MODE


def resolve_theme(graph: MetroGraph, mode: str | None = None) -> Theme:
    """The theme *graph*'s bound stack bakes in *mode* (see :func:`resolve_mode`)."""
    return bound_stack(graph).for_mode(resolve_mode(mode, graph))


# Concrete themes for callers without a graph, resolved on first use.
_NAMED_THEMES = {
    "DEFAULT_LIGHT_THEME": ("default", LIGHT),
    "DEFAULT_DARK_THEME": ("default", DARK),
    "SEQERA_LIGHT_THEME": ("seqera", LIGHT),
    "SEQERA_DARK_THEME": ("seqera", DARK),
    "TRANSPARENT_THEME": ("transparent", DEFAULT_MODE),
}


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """``THEMES``, every built-in at :data:`DEFAULT_MODE`, and the named themes."""
    if name == "THEMES":
        return {n: resolve_stack(n).for_mode(DEFAULT_MODE) for n in sorted(STYLE_NAMES)}
    if name in _NAMED_THEMES:
        stack, mode = _NAMED_THEMES[name]
        return resolve_stack(stack).for_mode(mode)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BASE_THEME",
    "DARK",
    "DEFAULT_MODE",
    "LIGHT",
    "MODES",
    "STYLE_NAMES",
    "renamed_style_hint",
    "THEMES",
    "ResolvedTheme",
    "ThemeError",
    "anchor_stack",
    "bound_stack",
    "check_stack",
    "resolve_mode",
    "resolve_stack",
    "resolve_theme",
    "DEFAULT_LIGHT_THEME",
    "DEFAULT_DARK_THEME",
    "SEQERA_LIGHT_THEME",
    "SEQERA_DARK_THEME",
    "TRANSPARENT_THEME",
]
