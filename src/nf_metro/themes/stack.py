"""Theme files, theme stacks, and their resolution to a light and dark theme.

A theme file is YAML. Every key except ``name`` is optional::

    name: my-theme
    extends: london-tube      # names or files stacked before this one
    fixed_mode: dark          # one palette, rendered in this mode; none clears
    label_font_size: 13       # top-level fields apply to both modes
    light:
      label_color: "#1c3f94"
    dark:
      label_color: "#e8ecf8"
    layout:
      y_spacing: 45           # layout options, below directives and CLI flags

A stack is an ordered list of theme names and files. It always starts from the
base theme (``nfcore``), then applies each entry in turn; a later entry wins
for every field it sets, and light and dark merge independently. ``fixed_mode``
merges the same way: a later entry that sets it, to a mode or to ``none``,
replaces an earlier one.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from nf_metro.errors import NfMetroError
from nf_metro.options import coerce
from nf_metro.render.style import Theme, authored_fields
from nf_metro.themes.schema import THEMEABLE_LAYOUT_OPTIONS, theme_schema

__all__ = [
    "DARK",
    "LIGHT",
    "MODES",
    "UNSET",
    "THEMEABLE_LAYOUT_OPTIONS",
    "ResolvedTheme",
    "ThemeError",
    "ThemeFile",
    "ThemeLayer",
    "is_theme_file_entry",
    "layer_from_mapping",
    "load_theme_file",
    "merge_layers",
    "split_stack",
]

LIGHT = "light"
DARK = "dark"
MODES = (LIGHT, DARK)

#: A layer's ``fixed_mode`` when its file does not mention the key, as opposed
#: to ``None`` for an explicit ``fixed_mode: none``.
UNSET = "unset"
_THEME_FILE_SUFFIXES = (".yaml", ".yml")


class ThemeError(NfMetroError, ValueError):
    """A theme stack entry or theme file is unknown, unreadable, or invalid."""


@dataclass(frozen=True, eq=False)
class ThemeLayer:
    """One stack entry's contribution: the fields it sets per mode, and more.

    ``fixed_mode`` is :data:`UNSET` when the entry leaves the stack's fixed
    mode alone, ``None`` when it clears it, or the mode it fixes.
    """

    name: str
    light: Mapping[str, Any]
    dark: Mapping[str, Any]
    layout: Mapping[str, Any] = dataclasses.field(
        default_factory=lambda: MappingProxyType({})
    )
    fixed_mode: str | None = UNSET


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
        return self.light if effective == LIGHT else self.dark

    def __deepcopy__(self, memo: dict[int, object]) -> ResolvedTheme:
        # Immutable and shared by every resolution of its stack, so a copied
        # graph keeps the same object.
        return self


def split_stack(value: str) -> list[str]:
    """Split a ``style``/``--theme`` value into its non-empty entries."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def is_theme_file_entry(entry: str) -> bool:
    """Whether a stack entry names a YAML theme file rather than a built-in."""
    return entry.lower().endswith(_THEME_FILE_SUFFIXES)


_VALIDATOR = Draft202012Validator(theme_schema())
_TOP_LEVEL_KEYS = frozenset(
    {"name", "extends", "fixed_mode", "light", "dark", "layout"}
)
_NUMBER_TYPES = {"float": float, "int": int}


def _typed(fields: Mapping[str, Any]) -> dict[str, Any]:
    """*fields* with each number made the type its theme field declares."""
    kinds = {name: str(f.type) for name, f in authored_fields().items()}
    return {
        key: _NUMBER_TYPES[kinds[key]](value) if kinds[key] in _NUMBER_TYPES else value
        for key, value in fields.items()
    }


def layer_from_mapping(data: object, where: str) -> tuple[ThemeLayer, list[str]]:
    """Validate a parsed theme file; return its layer and ``extends`` entries.

    The file is checked against the theme schema; an error names *where* and
    the JSON path of the offending value.
    """
    error = best_match(_VALIDATOR.iter_errors(data))
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
    fixed_mode: str | None = UNSET
    if "fixed_mode" in data:
        fixed_mode = (
            None if data["fixed_mode"] in (None, "none") else data["fixed_mode"]
        )
    shared = _typed({k: v for k, v in data.items() if k not in _TOP_LEVEL_KEYS})
    light = {**shared, **_typed(data.get("light") or {})}
    dark = {**shared, **_typed(data.get("dark") or {})}
    layout = {
        key: coerce(THEMEABLE_LAYOUT_OPTIONS[key], str(value))[0]
        for key, value in (data.get("layout") or {}).items()
    }
    layer = ThemeLayer(
        str(data["name"]).strip(),
        MappingProxyType(light),
        MappingProxyType(dark),
        MappingProxyType(layout),
        fixed_mode,
    )
    return layer, extends


@dataclass(frozen=True)
class ThemeFile:
    """A theme file's validated layer, keyed by its path and content."""

    path: Path
    digest: str
    layer: ThemeLayer
    extends: tuple[str, ...]


@lru_cache(maxsize=64)
def _parse(path: Path, digest: str, data: bytes) -> ThemeFile:
    where = f"theme file {path}"
    try:
        parsed = yaml.safe_load(data.decode())
    except UnicodeDecodeError:
        raise ThemeError(f"{where}: not UTF-8 text") from None
    except yaml.YAMLError as e:
        raise ThemeError(f"{where}: invalid YAML: {e}") from None
    layer, extends = layer_from_mapping(parsed, where)
    return ThemeFile(path, digest, layer, tuple(extends))


def load_theme_file(path: Path) -> ThemeFile:
    """Read and validate a theme file, reusing the result while its content holds."""
    try:
        data = path.read_bytes()
    except OSError:
        raise ThemeError(f"theme file not found: {path}") from None
    return _parse(path, hashlib.sha256(data).hexdigest(), data)


def merge_layers(
    layers: Sequence[ThemeLayer], *, name: str, pinned_mode: str | None = None
) -> ResolvedTheme:
    """Merge *layers* left to right into a :class:`ResolvedTheme`."""
    fields: dict[str, dict[str, Any]] = {LIGHT: {}, DARK: {}}
    layout: dict[str, Any] = {}
    fixed_mode: str | None = None
    for layer in layers:
        fields[LIGHT].update(layer.light)
        fields[DARK].update(layer.dark)
        layout.update(layer.layout)
        if layer.fixed_mode != UNSET:
            fixed_mode = layer.fixed_mode
    frozen_layout = MappingProxyType(layout)
    try:
        if fixed_mode is not None:
            theme = Theme(mode=fixed_mode, **fields[fixed_mode])
            return ResolvedTheme(
                name, theme, theme, frozen_layout, fixed_mode, pinned_mode
            )
        light = Theme(mode=LIGHT, **fields[LIGHT])
        dark = Theme(mode=DARK, **fields[DARK])
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
        frozen_layout,
        None,
        pinned_mode,
    )
