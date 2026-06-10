"""Constants and helpers shared by the producer and reader halves."""

from __future__ import annotations

# Bump the major part for a breaking change; minor for additive fields (which
# consumers tolerate via the "ignore unknown fields" rule).
MANIFEST_SCHEMA_VERSION = "1.0"

# The id of the <metadata> element carrying the manifest; the stable anchor a
# consumer searches for in the SVG.
MANIFEST_ELEMENT_ID = "diagram-manifest"

# The fixed half of the match contract (a non-Python consumer reproduces it):
# node.patterns are case-insensitive regexes. The target they match against is
# producer-supplied (see build_manifest_data's match_target).
_MATCH_TYPE_FLAGS = {"type": "regex", "flags": "i"}
_DEFAULT_MATCH_TARGET = "fqProcessName"


def _round1(value: float) -> float:
    """Round a coordinate to one decimal place, matching the overlay geometry."""
    return round(float(value), 1)
