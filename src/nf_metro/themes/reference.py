"""The theme field reference, generated from the :class:`Theme` schema.

``docs/theming.mdx`` embeds this reference between the
``theme-field-reference`` markers, and a test fails when the two differ.
Rewrite it in place with ``python -m nf_metro.themes.reference --write``.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

from nf_metro.render.style import authored_fields, chrome_bindings
from nf_metro.themes import BASE_THEME
from nf_metro.themes.stack import THEMEABLE_LAYOUT_OPTIONS

START = "{/* theme-field-reference:start */}"
END = "{/* theme-field-reference:end */}"


def _kind(field: dataclasses.Field[Any]) -> str:
    if field.metadata["choices"]:
        return " \\| ".join(f"`{c}`" for c in field.metadata["choices"])
    if field.metadata["color"]:
        return "colour"
    kind = field.type if isinstance(field.type, str) else field.type.__name__
    return {"bool": "`true` \\| `false`", "float": "number", "int": "whole number"}.get(
        kind, "text"
    )


def _default(field: dataclasses.Field[Any]) -> str:
    if field.default is dataclasses.MISSING:
        return f"from `{BASE_THEME}`"
    value = field.default
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value == "":
        return "empty"
    return f"`{value}`"


def field_reference() -> str:
    """Markdown tables of every authorable theme field, grouped as in the schema."""
    groups: dict[str, list[str]] = {}
    for name, field in authored_fields().items():
        doc = field.metadata["doc"]
        row = f"| `{name}` | {_kind(field)} | {_default(field)} | {doc} |"
        groups.setdefault(field.metadata["group"], []).append(row)
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
