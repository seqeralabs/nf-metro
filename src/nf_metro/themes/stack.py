"""Theme files, theme stacks, and their resolution to a light and dark theme.

A theme file is YAML. Every key except ``name`` is optional::

    name: my-theme
    extends: london-tube      # names or files stacked before this one
    label_font_size: 13       # top-level fields apply to both modes
    light:
      label_color: "#1c3f94"  # colours only, for one mode
    dark:
      label_color: "#e8ecf8"
    layout:
      y_spacing: 45           # layout options, below directives and CLI flags

A stack is an ordered list of theme names and files. It always starts from the
base theme (``nfcore``), then applies each entry in turn; a later entry wins
for every field it sets, and light and dark merge independently.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nf_metro.errors import NfMetroError
from nf_metro.options import DARK, LIGHT, MODES
from nf_metro.render.style import Theme, authored_fields
from nf_metro.themes.schema import THEMEABLE_LAYOUT_OPTIONS, theme_schema

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

__all__ = [
    "DARK",
    "LIGHT",
    "MODES",
    "ResolvedTheme",
    "ThemeError",
    "ThemeLayer",
    "is_theme_file_entry",
    "layer_from_mapping",
    "load_theme_file",
    "merge_layers",
    "split_stack",
]

_THEME_FILE_SUFFIXES = (".yaml", ".yml")

# Colours a stack leaves empty take another field's value for the same mode.
_TAKES = {
    "marker_stroke": "station_stroke",
    "section_badge_fill": "station_stroke",
    "section_badge_text_color": "station_fill",
    "icon_fill": "station_fill",
    "icon_stroke": "station_stroke",
}


class ThemeError(NfMetroError, ValueError):
    """A theme stack entry or theme file is unknown, unreadable, or invalid."""


@dataclass(frozen=True, eq=False)
class ThemeLayer:
    """One theme file's contribution to a stack: the fields it sets for each
    mode, its layout options, and the entries it ``extends``."""

    name: str
    light: Mapping[str, Any]
    dark: Mapping[str, Any]
    layout: Mapping[str, Any]
    extends: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedTheme:
    """A resolved stack: its light and dark themes and its layout options."""

    name: str
    light: Theme
    dark: Theme
    layout: dict[str, Any]

    def for_mode(self, mode: str) -> Theme:
        """The theme to bake for *mode*."""
        return self.light if mode == LIGHT else self.dark


def split_stack(value: str) -> list[str]:
    """Split a ``style``/``--theme`` value into its non-empty entries."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def is_theme_file_entry(entry: str) -> bool:
    """Whether a stack entry names a YAML theme file rather than a built-in."""
    return entry.lower().endswith(_THEME_FILE_SUFFIXES)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    from jsonschema import Draft202012Validator

    return Draft202012Validator(theme_schema())


@lru_cache(maxsize=1)
def _number_types() -> dict[str, type]:
    """The number type of every numeric theme field and layout option."""
    casts = {"float": float, "int": int}
    types = {
        name: casts[str(f.type)]
        for name, f in authored_fields().items()
        if str(f.type) in casts
    }
    types |= {
        f"layout.{name}": casts[opt.kind]
        for name, opt in THEMEABLE_LAYOUT_OPTIONS.items()
        if opt.kind in casts
    }
    return types


def _typed(values: Mapping[str, Any], prefix: str, where: str) -> dict[str, Any]:
    """*values* with every number made the type it declares, refusing a
    non-finite one (which JSON Schema's bounds let through)."""
    types = _number_types()
    out = dict(values)
    for key, value in values.items():
        number = types.get(prefix + key)
        if number is None:
            continue
        if not math.isfinite(value):
            raise ThemeError(
                f"{where}: $.{prefix}{key}: {value!r} is not a finite number"
            )
        out[key] = number(value)
    return out


def layer_from_mapping(
    data: object, where: str, *, validate: bool = True
) -> ThemeLayer:
    """The layer of a parsed theme file.

    The file is checked against the theme schema unless *validate* is false
    (a packaged theme, checked by the test suite); an error names *where* and
    the JSON path of the offending value.
    """
    if validate:
        from jsonschema.exceptions import best_match

        error = best_match(_validator().iter_errors(data))
        if error is not None:
            message = (
                f"{error.instance!r} is not a colour"
                if error.validator == "pattern" and error.schema.get("x-colour")
                else error.message
            )
            raise ThemeError(f"{where}: {error.json_path}: {message}")
    assert isinstance(data, Mapping)
    extends_raw = data.get("extends") or []
    extends = (
        split_stack(extends_raw)
        if isinstance(extends_raw, str)
        else [e.strip() for e in extends_raw if e.strip()]
    )
    fields = authored_fields()
    shared = _typed({k: v for k, v in data.items() if k in fields}, "", where)
    return ThemeLayer(
        str(data["name"]).strip(),
        {**shared, **(data.get("light") or {})},
        {**shared, **(data.get("dark") or {})},
        _typed(data.get("layout") or {}, "layout.", where),
        tuple(extends),
    )


@lru_cache(maxsize=64)
def _parse(path: Path, data: bytes, validate: bool) -> ThemeLayer:
    import yaml

    where = f"theme file {path}"
    try:
        parsed = yaml.safe_load(data.decode())
    except UnicodeDecodeError:
        raise ThemeError(f"{where}: not UTF-8 text") from None
    except yaml.YAMLError as e:
        raise ThemeError(f"{where}: invalid YAML: {e}") from None
    return layer_from_mapping(parsed, where, validate=validate)


def load_theme_file(path: Path, *, validate: bool = True) -> ThemeLayer:
    """Read a theme file, reusing the result while its content holds."""
    try:
        data = path.read_bytes()
    except OSError:
        raise ThemeError(f"theme file not found: {path}") from None
    return _parse(path, data, validate)


def _complete(values: dict[str, Any]) -> dict[str, Any]:
    """One mode's merged fields with every empty derived colour filled in."""
    for field, source in _TAKES.items():
        values[field] = values.get(field) or values[source]
    background = values["background_fill"]
    opaque = background not in ("", "none", "transparent")
    values["knockout_fill"] = values.get("knockout_fill") or (
        background if opaque else "#ffffff"
    )
    return values


def merge_layers(layers: Sequence[ThemeLayer], *, name: str) -> ResolvedTheme:
    """Merge *layers* left to right into a :class:`ResolvedTheme`."""
    light: dict[str, Any] = {}
    dark: dict[str, Any] = {}
    layout: dict[str, Any] = {}
    for layer in layers:
        light.update(layer.light)
        dark.update(layer.dark)
        layout.update(layer.layout)
    light, dark = _complete(light), _complete(dark)
    mode_colors = tuple(
        (key, light.get(key, field.default), dark.get(key, field.default))
        for key, field in authored_fields().items()
        if field.metadata["color"]
    )
    return ResolvedTheme(
        name,
        Theme(mode=LIGHT, mode_colors=mode_colors, **light),
        Theme(mode=DARK, mode_colors=mode_colors, **dark),
        layout,
    )
