"""The theme field reference, generated from the theme file's JSON Schema.

``docs/theming.mdx`` embeds this reference between the
``theme-field-reference`` markers, and a test fails when the two differ.
Rewrite it in place with ``python -m nf_metro.themes.reference --write``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from nf_metro.render.style import chrome_bindings
from nf_metro.themes import BASE_THEME
from nf_metro.themes.schema import THEMEABLE_LAYOUT_OPTIONS, theme_schema

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
    chrome = "\n".join(f"| `{b.field}` | `{b.css_var}` |" for b in chrome_bindings())
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


def write(doc: Path) -> None:
    """Replace the reference between the markers in *doc*."""
    text = doc.read_text()
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    doc.write_text(f"{head}{START}\n\n{field_reference()}\n{END}{tail}")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write(Path(__file__).resolve().parents[3] / "docs" / "theming.mdx")
    else:
        print(field_reference(), end="")
