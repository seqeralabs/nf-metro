"""The theme schema: what a theme file may say, and the documents generated from it."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from theme_helpers import ROOT, write

from nf_metro.themes import ThemeError, resolve_stack
from nf_metro.themes.schema import SCHEMA_FILE, theme_schema

sys.path.insert(0, str(ROOT / "scripts"))
from theme_docs import END, START, field_reference, render_schema  # noqa: E402

_THEMES = ROOT / "src" / "nf_metro" / "themes"
_BUILT_INS = sorted(_THEMES.glob("*.yaml"))
_PROBES = Path(__file__).resolve().parent / "fixtures" / "themes"


def _normalise(markdown: str) -> str:
    """Drop the padding and line wrapping that prettier and the sentence
    formatter add, keeping every word and cell."""
    text = re.sub(r"-{3,}", "---", markdown)
    return " ".join(text.split())


def test_shipped_schema_matches_the_dataclass():
    """Regenerate with ``python scripts/theme_docs.py --write`` when this fails."""
    assert (_THEMES / SCHEMA_FILE).read_text() == render_schema()


def test_field_reference_matches_the_schema():
    """Regenerate with ``python scripts/theme_docs.py --write`` when this fails."""
    text = (ROOT / "docs" / "theming.mdx").read_text()
    documented = text[text.index(START) + len(START) : text.index(END)]
    assert _normalise(documented) == _normalise(field_reference())


def test_shipped_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(theme_schema())


@pytest.mark.parametrize("path", _BUILT_INS, ids=lambda p: p.stem)
def test_built_in_themes_are_valid(path):
    """Packaged themes skip validation when loaded, so it happens here."""
    Draft202012Validator(theme_schema()).validate(yaml.safe_load(path.read_text()))


@pytest.mark.parametrize("path", _BUILT_INS, ids=lambda p: p.stem)
def test_built_in_themes_point_editors_at_the_schema(path):
    assert path.read_text().splitlines()[0] == (
        f"# yaml-language-server: $schema={SCHEMA_FILE}"
    )


@pytest.mark.parametrize(
    "body,message",
    [
        (
            "name: x\nstation_colour: red\n",
            "$: Additional properties are not allowed ('station_colour'",
        ),
        ("name: x\nlabel_color: '#12345'\n", "$.label_color: '#12345' is not a colour"),
        (
            "name: x\nline_width: thick\n",
            "$.line_width: 'thick' is not of type 'number'",
        ),
        (
            "name: x\nnumber_sections: 1\n",
            "$.number_sections: 1 is not of type 'boolean'",
        ),
        ("name: x\nstation_glyph: dot\n", "$.station_glyph: 'dot' is not one of"),
        ("name: x\nline_width: -1\n", "$.line_width: -1 is less than"),
        (
            "name: x\nlight:\n  station_radius: 14\n",
            "$.light: Additional properties are not allowed ('station_radius'",
        ),
        (
            "name: x\nfixed_mode: dark\n",
            "$: Additional properties are not allowed ('fixed_mode'",
        ),
        ("name: x\nline_width: .inf\n", "$.line_width: inf is not a finite number"),
        ("name: x\nlabel_font_size: .nan\n", "$.label_font_size: nan is not a finite"),
        (
            "name: x\nlayout:\n  x_spacing: .inf\n",
            "$.layout.x_spacing: inf is not a finite number",
        ),
        (
            "name: x\nlayout:\n  fold_threshold: 3\n",
            "$.layout: Additional properties are not allowed ('fold_threshold'",
        ),
        (
            "name: x\nlayout:\n  track_gap: wide\n",
            "$.layout.track_gap: 'wide' is not of type",
        ),
        ("name: x\ntitle_font_weight: heavy\n", "$.title_font_weight: 'heavy' is not"),
        ("name: x\ndashed_dasharray: '4 x'\n", "$.dashed_dasharray: '4 x' does not"),
        ("station_glyph: tick\n", "$: 'name' is a required property"),
        ("- name\n", "is not of type 'object'"),
    ],
)
def test_invalid_theme_files_name_the_problem(tmp_path, body, message):
    path = write(tmp_path, "bad.yaml", body)
    with pytest.raises(ThemeError, match=re.escape(f"theme file {path}")) as raised:
        resolve_stack(str(path))
    assert message in str(raised.value)


@pytest.mark.parametrize(
    "field",
    ["label_font_family", "dashed_dasharray", "title_font_weight"],
)
def test_markup_in_free_text_fields_is_rejected(field):
    with pytest.raises(ThemeError, match=re.escape(f"$.{field}:")):
        resolve_stack(str(_PROBES / f"inject_{field}.yaml"))
