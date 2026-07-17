"""Access to the JSON Schema describing a live state snapshot."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

# This file's own version, tracked alongside nf_metro.manifest.MANIFEST_SCHEMA_VERSION.
# No version field is embedded in the snapshot itself (see state_schema.json); a
# breaking change to this schema bumps this constant and nf-metro's major version
# together.
STATE_SCHEMA_VERSION = "1.0"


def state_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) for a live state snapshot.

    The machine-readable form of the vocabulary served by ``GET /state`` and
    each Server-Sent Event on ``/stream``: validate any producer's snapshot
    against it (in Python via ``jsonschema``, or in any language with a
    standard validator). Shipped as ``state_schema.json`` beside this module
    so it travels with the package.
    """
    text = files(__package__).joinpath("state_schema.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema
