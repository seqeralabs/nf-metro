"""Theme files, theme stacks, and their resolution to a light and dark theme.

A theme file is YAML. Every key except ``name`` is optional::

    name: my-theme
    extends: london-tube      # names or files stacked before this one
    fixed_mode: dark          # one palette, rendered in this mode only
    label_font_size: 13       # top-level fields apply to both modes
    light:
      label_color: "#1c3f94"
    dark:
      label_color: "#e8ecf8"
    layout:
      y_spacing: 45           # layout options, below directives and CLI flags

A stack is an ordered list of theme names and files. It always starts from the
base theme (``nfcore``), then applies each entry in turn; a later entry wins
for every field it sets, and light and dark merge independently.
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
from nf_metro.render.style import Theme, authored_fields

__all__ = [
    "MODES",
    "THEMEABLE_LAYOUT_OPTIONS",
    "ResolvedTheme",
    "ThemeError",
    "ThemeLayer",
    "is_theme_file_entry",
    "layer_from_mapping",
    "load_theme_file",
    "merge_layers",
    "split_stack",
]

MODES = ("light", "dark")
_THEME_FILE_SUFFIXES = (".yaml", ".yml")

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

_TOP_LEVEL_KEYS = frozenset(
    {"name", "extends", "fixed_mode", "light", "dark", "layout"}
)


class ThemeError(NfMetroError, ValueError):
    """A theme stack entry or theme file is unknown, unreadable, or invalid."""


@dataclass(frozen=True)
class ThemeLayer:
    """One stack entry's contribution: the fields it sets per mode, and more."""

    name: str
    light: Mapping[str, Any]
    dark: Mapping[str, Any]
    layout: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    fixed_mode: str | None = None


@dataclass(frozen=True)
class ResolvedTheme:
    """A resolved stack: both themes, its layout options, and its mode rules.

    ``fixed_mode`` comes from a single-palette theme, which renders in that
    mode whatever mode is asked for and carries no light/dark pair.
    ``pinned_mode`` comes from a mode-suffixed name (``nfcore-light``): the
    baked mode is fixed but both palettes are kept.
    """

    name: str
    light: Theme
    dark: Theme
    layout: Mapping[str, Any]
    fixed_mode: str | None = None
    pinned_mode: str | None = None

    def for_mode(self, mode: str) -> Theme:
        """The theme to bake for a requested *mode*."""
        effective = self.fixed_mode or self.pinned_mode or mode
        return self.light if effective == "light" else self.dark


def split_stack(value: str) -> list[str]:
    """Split a ``style``/``--theme`` value into its non-empty entries."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def is_theme_file_entry(entry: str) -> bool:
    """Whether a stack entry names a YAML theme file rather than a built-in."""
    return entry.lower().endswith(_THEME_FILE_SUFFIXES)


def _field_kind(field: dataclasses.Field[Any]) -> str:
    return field.type if isinstance(field.type, str) else field.type.__name__


def _check_field(key: str, value: object, where: str) -> object:
    field = authored_fields().get(key)
    if field is None:
        raise ThemeError(
            f"{where}: unknown theme field {key!r}; valid fields are "
            + ", ".join(sorted(authored_fields()))
        )
    kind = _field_kind(field)
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
    choices = field.metadata["choices"]
    if choices and value not in choices:
        raise ThemeError(
            f"{where}: {key} must be one of {', '.join(choices)}, got {value!r}"
        )
    if field.metadata["color"] and not _COLOR.match(value.strip()):
        raise ThemeError(f"{where}: {key} is not a colour: {value!r}")
    return value


def _check_fields(data: object, where: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ThemeError(f"{where}: expected a mapping of theme fields")
    return {str(k): _check_field(str(k), v, where) for k, v in data.items()}


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
    fixed_mode = data.get("fixed_mode")
    if fixed_mode is not None and fixed_mode not in MODES:
        raise ThemeError(
            f"{where}: fixed_mode must be light or dark, got {fixed_mode!r}"
        )
    shared = _check_fields(
        {k: v for k, v in data.items() if k not in _TOP_LEVEL_KEYS}, where
    )
    light = {**shared, **_check_fields(data.get("light"), f"{where} (light)")}
    dark = {**shared, **_check_fields(data.get("dark"), f"{where} (dark)")}
    layout = _check_layout(data.get("layout"), f"{where} (layout)")
    layer = ThemeLayer(name.strip(), light, dark, layout, fixed_mode)
    return layer, extends


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
    layers: Sequence[ThemeLayer], *, name: str, pinned_mode: str | None = None
) -> ResolvedTheme:
    """Merge *layers* left to right into a :class:`ResolvedTheme`."""
    fields: dict[str, dict[str, Any]] = {"light": {}, "dark": {}}
    layout: dict[str, Any] = {}
    fixed_mode: str | None = None
    for layer in layers:
        fields["light"].update(layer.light)
        fields["dark"].update(layer.dark)
        layout.update(layer.layout)
        fixed_mode = layer.fixed_mode or fixed_mode
    try:
        if fixed_mode is not None:
            theme = Theme(name=name, mode=fixed_mode, **fields[fixed_mode])
            return ResolvedTheme(name, theme, theme, layout, fixed_mode, pinned_mode)
        light = Theme(name=f"{name}-light", mode="light", **fields["light"])
        dark = Theme(name=f"{name}-dark", mode="dark", **fields["dark"])
    except TypeError as e:
        raise ThemeError(f"theme {name!r} leaves required fields unset: {e}") from None
    mode_colors = tuple(
        (key, getattr(light, key), getattr(dark, key))
        for key, field in authored_fields().items()
        if field.metadata["color"]
    )
    return ResolvedTheme(
        name,
        dataclasses.replace(light, mode_colors=mode_colors),
        dataclasses.replace(dark, mode_colors=mode_colors),
        layout,
        None,
        pinned_mode,
    )
