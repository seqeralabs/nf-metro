"""The shipped theme schema is the one the Theme dataclass generates."""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from nf_metro.themes.schema import SCHEMA_FILE, render_schema, shipped_theme_schema

_THEMES = Path(__file__).resolve().parents[1] / "src" / "nf_metro" / "themes"


def test_shipped_schema_matches_the_dataclass():
    """Regenerate with ``python -m nf_metro.themes.schema --write`` when this fails."""
    assert (_THEMES / SCHEMA_FILE).read_text() == render_schema()


def test_shipped_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(shipped_theme_schema())


@pytest.mark.parametrize("path", sorted(_THEMES.glob("*.yaml")), ids=lambda p: p.stem)
def test_built_in_themes_point_editors_at_the_schema(path):
    assert path.read_text().splitlines()[0] == (
        f"# yaml-language-server: $schema={SCHEMA_FILE}"
    )
