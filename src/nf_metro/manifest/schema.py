"""Access to the JSON Schema describing a manifest dict."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def manifest_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) for a manifest.

    The machine-readable form of the format: validate any producer's output
    against it (in Python via ``jsonschema``, or in any language with a standard
    validator). Shipped as ``schema.json`` beside this module so it travels with
    the package.
    """
    text = files(__package__).joinpath("schema.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema
