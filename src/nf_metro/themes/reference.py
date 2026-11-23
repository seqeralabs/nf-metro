"""The theme field reference, generated from the :class:`Theme` schema.

``docs/theming.mdx`` embeds this reference between the
``theme-field-reference`` markers, and a test fails when the two differ.
Regenerate it with ``python -m nf_metro.themes.reference``.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from nf_metro.render.style import authored_fields
from nf_metro.themes import BASE_THEME


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
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    print(field_reference(), end="")
