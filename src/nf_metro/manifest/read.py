"""Consumer side: read the manifest back out of an SVG and match names to nodes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ._common import MANIFEST_ELEMENT_ID

_METADATA_RE = re.compile(
    rf'<metadata id="{re.escape(MANIFEST_ELEMENT_ID)}"[^>]*>(.*?)</metadata>',
    re.DOTALL,
)
_CDATA_RE = re.compile(r"\s*<!\[CDATA\[(.*)\]\]>\s*\Z", re.DOTALL)


def read_manifest(svg: str) -> dict[str, Any] | None:
    """Parse the embedded manifest back out of a rendered SVG string.

    The canonical reader for the contract: returns the manifest dict, or
    ``None`` if the SVG carries no manifest.  Parser-independent (a plain regex
    extract) so a consumer needn't load an XML library.
    """
    match = _METADATA_RE.search(svg)
    if match is None:
        return None
    inner = match.group(1)
    cdata = _CDATA_RE.match(inner)
    text = cdata.group(1) if cdata else inner
    text = text.replace("]]]]><![CDATA[>", "]]>")
    result: dict[str, Any] = json.loads(text)
    return result


def matching_node_ids(
    target: str, patterns_by_id: Mapping[str, Sequence[str]]
) -> list[str]:
    """Ids whose patterns match ``target`` (case-insensitive), in input order.

    The reference matcher for the standard's ``match`` semantics, over a plain
    ``id -> [pattern, ...]`` mapping.  :func:`match_node_ids` is the
    manifest-shaped convenience over it.
    """
    return [
        node_id
        for node_id, patterns in patterns_by_id.items()
        if any(re.search(pattern, target, re.IGNORECASE) for pattern in patterns)
    ]


def match_node_ids(manifest: Mapping[str, Any], target: str) -> list[str]:
    """Node ids in ``manifest`` whose ``patterns`` match ``target``.

    Mirrors the documented matching semantics so a consumer (in any language)
    can reproduce it from the embedded manifest alone.
    """
    patterns_by_id = {
        node["id"]: node.get("patterns", []) for node in manifest.get("nodes", [])
    }
    return matching_node_ids(target, patterns_by_id)
