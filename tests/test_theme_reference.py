"""docs/theming.mdx documents every theme field exactly as the schema declares it."""

from __future__ import annotations

import re
from pathlib import Path

from nf_metro.render.style import authored_fields, chrome_bindings
from nf_metro.themes.reference import field_reference
from nf_metro.themes.stack import THEMEABLE_LAYOUT_OPTIONS

_DOC = Path(__file__).resolve().parents[1] / "docs" / "theming.mdx"
_START = "{/* theme-field-reference:start */}"
_END = "{/* theme-field-reference:end */}"


def _normalise(markdown: str) -> str:
    """Drop the padding and line wrapping that prettier and the sentence
    formatter add, keeping every word and cell."""
    text = re.sub(r"-{3,}", "---", markdown)
    return " ".join(text.split())


def _documented_reference() -> str:
    text = _DOC.read_text()
    return text[text.index(_START) + len(_START) : text.index(_END)]


def test_field_reference_matches_the_schema():
    """Regenerate with ``python -m nf_metro.themes.reference`` when this fails."""
    assert _normalise(_documented_reference()) == _normalise(field_reference())


def test_every_authored_field_is_documented():
    reference = _documented_reference()
    missing = [name for name in authored_fields() if f"`{name}`" not in reference]
    assert not missing


def test_every_chrome_property_and_layout_option_is_documented():
    text = _DOC.read_text()
    for binding in chrome_bindings():
        assert f"`{binding.css_var}`" in text, binding.css_var
    for name in THEMEABLE_LAYOUT_OPTIONS:
        assert f"`{name}`" in text, name


def test_every_field_has_a_doc_and_group():
    for name, field in authored_fields().items():
        assert field.metadata["doc"].strip(), name
        assert field.metadata["group"].strip(), name
