"""Theme stacks: ordered lists of themes merged left to right.

A ``%%metro style:`` value or ``--theme`` argument is a comma-separated list
of entries. Each entry is a built-in theme name or the path to a YAML theme
file. Entries apply in order and a later entry wins per property, so
``nfcore, london-tube`` takes every property london-tube sets from london-tube
and everything else from nfcore. Light and dark merge independently.

A theme file has this shape (every key optional except ``name``)::

    name: my-theme
    extends: nfcore          # or a list; shorthand for stacking these first
    label_font_size: 13      # top-level fields apply to both modes
    light:
      label_color: "#1c3f94"
    dark:
      label_color: "#e8ecf8"
    layout:
      track_gap: 2           # layout options, below directives and CLI flags
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from nf_metro.errors import NfMetroError
from nf_metro.options import INVALID, LAYOUT_OPTIONS, LayoutOption, coerce
from nf_metro.render.style import Theme

__all__ = [
    "THEMEABLE_LAYOUT_OPTIONS",
    "ThemeError",
    "ThemeLayer",
    "is_theme_file_entry",
    "layer_from_family",
    "layer_from_mapping",
    "load_theme_file",
    "merge_layers",
    "split_stack",
]

_THEME_FILE_SUFFIXES = (".yaml", ".yml")

# Theme identity fields are derived from the stack, never authored.
_IDENTITY_FIELDS = frozenset({"name", "brand", "mode"})
_THEME_FIELDS: dict[str, dataclasses.Field[Any]] = {
    f.name: f for f in dataclasses.fields(Theme) if f.name not in _IDENTITY_FIELDS
}

_CHOICE_FIELDS: dict[str, tuple[str, ...]] = {
    "station_glyph": ("pill", "tick"),
}

# Layout options that shape a map's look. Options that change what is drawn
# or how a render behaves (size pinning, animation, guards, folding) stay with
# the map and the caller.
THEMEABLE_LAYOUT_OPTIONS: dict[str, LayoutOption] = {
    opt.name: opt
    for opt in LAYOUT_OPTIONS
    if opt.name
    in {
        "x_spacing",
        "y_spacing",
        "section_x_gap",
        "section_y_gap",
        "track_gap",
        "diamond_style",
        "line_order",
        "row_align",
        "center_ports",
        "compact_offsets",
        "label_angle",
        "font_scale",
        "stroke_scale",
        "logo_scale",
        "legend_min_height",
        "legend_logo_gap",
    }
}

_COLOR = re.compile(
    r"""^(
        |none|transparent|currentColor
        |\#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})
        |(?:rgb|rgba|hsl|hsla)\(\s*[-0-9.%,\s/]+\)
        |[a-zA-Z]+
    )$""",
    re.VERBOSE,
)


class ThemeError(NfMetroError, ValueError):
    """A theme stack entry or theme file is unknown, unreadable, or invalid."""


@dataclass(frozen=True)
class ThemeLayer:
    """One stack entry's contribution: per-mode Theme fields and layout options."""

    name: str
    light: Mapping[str, Any]
    dark: Mapping[str, Any]
    layout: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # Whether the entry fixes one palette for both modes (the transparent
    # ``light`` embed theme), so a render of it alone has no light/dark pair.
    single_palette: bool = False


def split_stack(value: str) -> list[str]:
    """Split a ``style``/``--theme`` value into its non-empty entries."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def is_theme_file_entry(entry: str) -> bool:
    """Whether a stack entry names a YAML theme file rather than a built-in."""
    return entry.lower().endswith(_THEME_FILE_SUFFIXES)


def layer_from_family(name: str, light: Theme, dark: Theme) -> ThemeLayer:
    """A built-in theme as a layer setting every field in both modes."""

    def fields(theme: Theme) -> dict[str, Any]:
        return {key: getattr(theme, key) for key in _THEME_FIELDS}

    return ThemeLayer(
        name=name,
        light=fields(light),
        dark=fields(dark),
        single_palette=light is dark,
    )


def _check_field(key: str, value: object, where: str) -> object:
    field = _THEME_FIELDS.get(key)
    if field is None:
        raise ThemeError(
            f"{where}: unknown theme field {key!r}; valid fields are "
            + ", ".join(sorted(_THEME_FIELDS))
        )
    kind = field.type if isinstance(field.type, str) else field.type.__name__
    if kind == "bool":
        if not isinstance(value, bool):
            raise ThemeError(f"{where}: {key} must be true or false, got {value!r}")
        return value
    if kind in ("float", "int"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ThemeError(f"{where}: {key} must be a number, got {value!r}")
        if value < 0:
            raise ThemeError(f"{where}: {key} must not be negative, got {value!r}")
        return int(value) if kind == "int" else float(value)
    if not isinstance(value, str):
        raise ThemeError(f"{where}: {key} must be a string, got {value!r}")
    choices = _CHOICE_FIELDS.get(key)
    if choices is not None and value not in choices:
        raise ThemeError(
            f"{where}: {key} must be one of {', '.join(choices)}, got {value!r}"
        )
    if _is_color_field(key) and not _COLOR.match(value.strip()):
        raise ThemeError(f"{where}: {key} is not a colour: {value!r}")
    return value


def _is_color_field(key: str) -> bool:
    if key.endswith(("_width", "_dasharray", "_radius", "_size")):
        return False
    return key.endswith(("_color", "_fill", "_stroke", "_background"))


def _check_fields(data: object, where: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ThemeError(f"{where}: expected a mapping of theme fields")
    return {str(k): _check_field(str(k), v, f"{where}") for k, v in data.items()}


def _check_layout(data: object, where: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ThemeError(f"{where}: layout must be a mapping of layout options")
    checked: dict[str, Any] = {}
    for key, raw in data.items():
        opt = THEMEABLE_LAYOUT_OPTIONS.get(str(key))
        if opt is None:
            raise ThemeError(
                f"{where}: {key!r} is not a layout option a theme can set; "
                "valid options are " + ", ".join(sorted(THEMEABLE_LAYOUT_OPTIONS))
            )
        text = str(raw).lower() if isinstance(raw, bool) else str(raw)
        value, expected = coerce(opt, text)
        if value is INVALID:
            raise ThemeError(f"{where}: layout {key} expects {expected}, got {raw!r}")
        checked[opt.name] = value
    return checked


_TOP_LEVEL_KEYS = frozenset({"name", "extends", "light", "dark", "layout"})


def layer_from_mapping(data: object, where: str) -> tuple[ThemeLayer, list[str]]:
    """Validate a parsed theme file; return its layer and ``extends`` entries."""
    if not isinstance(data, Mapping):
        raise ThemeError(f"{where}: a theme file must be a YAML mapping")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ThemeError(f"{where}: a theme file needs a non-empty 'name'")
    extends_raw = data.get("extends") or []
    if isinstance(extends_raw, str):
        extends = split_stack(extends_raw)
    elif isinstance(extends_raw, Sequence) and all(
        isinstance(e, str) for e in extends_raw
    ):
        extends = [e.strip() for e in extends_raw if e.strip()]
    else:
        raise ThemeError(f"{where}: 'extends' must be a name or a list of names")
    shared = _check_fields(
        {k: v for k, v in data.items() if k not in _TOP_LEVEL_KEYS}, where
    )
    light = {**shared, **_check_fields(data.get("light"), f"{where} (light)")}
    dark = {**shared, **_check_fields(data.get("dark"), f"{where} (dark)")}
    layout = _check_layout(data.get("layout"), f"{where} (layout)")
    return ThemeLayer(name=name.strip(), light=light, dark=dark, layout=layout), extends


@lru_cache(maxsize=64)
def _load_cached(path: str, mtime_ns: int) -> tuple[ThemeLayer, tuple[str, ...]]:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise ThemeError(f"theme file {path}: invalid YAML: {e}") from None
    layer, extends = layer_from_mapping(data, f"theme file {path}")
    return layer, tuple(extends)


def load_theme_file(path: Path) -> tuple[ThemeLayer, list[str]]:
    """Read and validate a theme file; return its layer and ``extends`` entries."""
    try:
        stat = path.stat()
    except OSError:
        raise ThemeError(f"theme file not found: {path}") from None
    layer, extends = _load_cached(str(path), stat.st_mtime_ns)
    return layer, list(extends)


def merge_layers(
    layers: Sequence[ThemeLayer], *, name: str, brand: str
) -> tuple[Theme, Theme, dict[str, Any]]:
    """Merge *layers* left to right into a light theme, a dark theme, and layout."""
    light: dict[str, Any] = {}
    dark: dict[str, Any] = {}
    layout: dict[str, Any] = {}
    for layer in layers:
        light.update(layer.light)
        dark.update(layer.dark)
        layout.update(layer.layout)
    return (
        Theme(name=f"{name}-light", brand=brand, mode="light", **light),
        Theme(name=f"{name}-dark", brand=brand, mode="dark", **dark),
        layout,
    )
