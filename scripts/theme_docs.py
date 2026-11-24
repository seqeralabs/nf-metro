"""Generated theme documents: the shipped JSON Schema and the field reference.

``src/nf_metro/themes/theme.schema.json`` is :func:`theme_schema` as shipped,
for editors and other tools, and ``docs/theming.mdx`` embeds the field
reference between the ``theme-field-reference`` markers. Tests fail when
either drifts; rewrite both with ``python scripts/theme_docs.py --write``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nf_metro.render.style import CHROME
from nf_metro.themes import BASE_THEME
from nf_metro.themes.schema import SCHEMA_FILE, THEMEABLE_LAYOUT_OPTIONS, theme_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "src" / "nf_metro" / "themes" / SCHEMA_FILE
DOC_PATH = ROOT / "docs" / "theming.mdx"

START = "{/* theme-field-reference:start */}"
END = "{/* theme-field-reference:end */}"

_KINDS = {
    "boolean": "`true` \\| `false`",
    "number": "number",
    "integer": "whole number",
    "string": "text",
}


def _kind(schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return " \\| ".join(f"`{c}`" for c in schema["enum"])
    if schema.get("x-colour"):
        return "colour"
    return _KINDS[schema["type"]]


def _default(schema: dict[str, Any]) -> str:
    if "default" not in schema:
        return f"from `{BASE_THEME}`"
    value = schema["default"]
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value == "":
        return "empty"
    return f"`{value}`"


def field_reference() -> str:
    """Markdown tables of every theme field, grouped as in the schema."""
    groups: dict[str, list[str]] = {}
    for name, schema in theme_schema()["$defs"]["fields"].items():
        row = (
            f"| `{name}` | {_kind(schema)} | {_default(schema)} | "
            f"{schema['description']} |"
        )
        groups.setdefault(schema["x-group"], []).append(row)
    parts = []
    for group, rows in groups.items():
        parts.append(
            f"### {group}\n\n| Field | Value | Default | Effect |\n"
            "| --- | --- | --- | --- |\n" + "\n".join(rows)
        )
    chrome = "\n".join(f"| `{name}` | `{c.css_var}` |" for name, c in CHROME.items())
    parts.append(
        "### Chrome colors and host overrides\n\n"
        "These colors also emit a CSS custom property, so a page that inlines "
        "the SVG can recolor them (see [`var()` for host overrides]"
        "(#var-for-host-overrides-optional)). An empty value emits none.\n\n"
        "| Field | Custom property |\n| --- | --- |\n" + chrome
    )
    options = ", ".join(f"`{name}`" for name in THEMEABLE_LAYOUT_OPTIONS)
    parts.append(
        "### Layout options\n\n"
        "A `layout:` block takes these options, with the same values as their "
        f"`%%metro` directives: {options}. A theme's value applies only where "
        "the map has no directive for that option, and a command-line flag "
        "beats both."
    )
    return "\n\n".join(parts) + "\n"


def render_schema() -> str:
    """The shipped schema file's text."""
    return json.dumps(theme_schema(), indent=2) + "\n"


def write() -> None:
    """Rewrite the schema file and the reference between the doc's markers."""
    SCHEMA_PATH.write_text(render_schema())
    text = DOC_PATH.read_text()
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    DOC_PATH.write_text(f"{head}{START}\n\n{field_reference()}\n{END}{tail}")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        print(field_reference(), end="")
