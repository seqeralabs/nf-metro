"""The JSON Schema of a theme file, generated from the :class:`Theme` dataclass.

The dataclass is the typed source of truth; :func:`theme_schema` derives the
schema (draft 2020-12) from its field types, defaults and metadata, and from the
layout options a theme may set. ``theme.schema.json`` beside this module is that
schema as shipped, for editors and other tools; a test fails when it drifts.
Rewrite it with ``python -m nf_metro.themes.schema --write``.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from nf_metro.options import LAYOUT_OPTIONS, LayoutOption
from nf_metro.render.style import authored_fields

__all__ = [
    "SCHEMA_FILE",
    "THEMEABLE_LAYOUT_OPTIONS",
    "theme_schema",
    "shipped_theme_schema",
]

SCHEMA_FILE = "theme.schema.json"
SCHEMA_URL = (
    "https://raw.githubusercontent.com/seqeralabs/nf-metro/main/"
    f"src/nf_metro/themes/{SCHEMA_FILE}"
)

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
        "section_x_padding",
        "section_y_padding",
        "track_gap",
        "diamond_style",
        "line_order",
        "row_align",
        "center_ports",
        "compact_offsets",
        "label_angle",
        "label_offset",
        "diagonal_run",
        "directional",
        "font_scale",
        "stroke_scale",
        "logo_scale",
        "legend_min_height",
        "legend_logo_gap",
    }
}

# A CSS colour as a theme writes one: empty, a keyword, hex, or an rgb()/hsl()
# function.
COLOR_PATTERN = (
    r"^\s*(|none|transparent|currentColor"
    r"|#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
    r"|(?:rgb|rgba|hsl|hsla)\(\s*[-0-9.%,\s/]+\)"
    r"|[a-zA-Z]+)\s*$"
)

_JSON_TYPES = {"str": "string", "float": "number", "int": "integer", "bool": "boolean"}


def _field_schema(field: dataclasses.Field[Any]) -> dict[str, Any]:
    kind = field.type if isinstance(field.type, str) else field.type.__name__
    meta = field.metadata
    schema: dict[str, Any] = {"description": meta["doc"], "x-group": meta["group"]}
    if meta["choices"]:
        schema["enum"] = list(meta["choices"])
    else:
        schema["type"] = _JSON_TYPES[kind]
    if meta["color"]:
        schema["pattern"] = COLOR_PATTERN
        schema["x-colour"] = True
    if kind in ("float", "int"):
        schema["minimum"] = 0
    if meta.get("maximum") is not None:
        schema["maximum"] = meta["maximum"]
    if field.default is not dataclasses.MISSING:
        schema["default"] = field.default
    return schema


def _layout_schema(opt: LayoutOption) -> dict[str, Any]:
    schema: dict[str, Any] = {"description": opt.help}
    if opt.kind == "choice":
        schema["enum"] = list(opt.choices)
    else:
        schema["type"] = {
            "bool": "boolean",
            "int": "integer",
            "float": "number",
            "str": "string",
        }[opt.kind]
    if opt.sign == "nonneg":
        schema["minimum"] = 0
    elif opt.sign == "positive":
        schema["exclusiveMinimum"] = 0
    if opt.max_val is not None:
        schema["maximum"] = opt.max_val
    return schema


def theme_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a theme file."""
    fields = {name: _field_schema(f) for name, f in authored_fields().items()}
    refs = {name: {"$ref": f"#/$defs/fields/{name}"} for name in fields}
    palette = {"type": "object", "properties": refs, "additionalProperties": False}
    layout = {
        "type": "object",
        "properties": {
            name: _layout_schema(opt) for name, opt in THEMEABLE_LAYOUT_OPTIONS.items()
        },
        "additionalProperties": False,
    }
    stack_entry = {"type": "string", "minLength": 1}
    structure: dict[str, Any] = {
        "name": {"type": "string", "pattern": r"\S", "description": "Theme name."},
        "extends": {
            "description": "Themes stacked before this one: a name, a theme "
            "file relative to this file, or a list of them.",
            "oneOf": [stack_entry, {"type": "array", "items": stack_entry}],
        },
        "fixed_mode": {
            "description": "Render one palette, in this mode, whatever mode is "
            "asked for; `none` clears a fixed mode set earlier in the stack.",
            "enum": ["light", "dark", "none", None],
        },
        "light": {**palette, "description": "Fields for the light palette only."},
        "dark": {**palette, "description": "Fields for the dark palette only."},
        "layout": {
            **layout,
            "description": "Layout options, below `%%metro` directives and "
            "command-line flags.",
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_URL,
        "title": "nf-metro theme",
        "description": "A theme file: top-level fields apply to both palettes, "
        "`light:` and `dark:` to one each.",
        "type": "object",
        "required": ["name"],
        "properties": {
            **structure,
            **refs,
        },
        "additionalProperties": False,
        "$defs": {"fields": fields},
    }


def render_schema() -> str:
    """The shipped schema file's text."""
    return json.dumps(theme_schema(), indent=2) + "\n"


@lru_cache(maxsize=1)
def shipped_theme_schema() -> dict[str, Any]:
    """The schema as shipped beside this module."""
    text = files(__package__).joinpath(SCHEMA_FILE).read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


if __name__ == "__main__":
    if "--write" in sys.argv:
        (Path(__file__).parent / SCHEMA_FILE).write_text(render_schema())
    else:
        print(render_schema(), end="")
