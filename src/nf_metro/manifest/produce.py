"""Producer side: build a manifest and embed it (and its mirror) in an SVG."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ._common import (
    _DEFAULT_MATCH_TARGET,
    _MATCH_TYPE_FLAGS,
    MANIFEST_ELEMENT_ID,
    MANIFEST_SCHEMA_VERSION,
    _round1,
)


def build_manifest_data(
    *,
    title: str | None,
    width: float,
    height: float,
    nodes: Iterable[Mapping[str, Any]],
    groups: Iterable[Mapping[str, Any]] = (),
    regions: Iterable[Mapping[str, Any]] = (),
    match_target: str = _DEFAULT_MATCH_TARGET,
) -> dict[str, Any]:
    """Assemble a manifest dict from plain node data (no graph required).

    This is the producer half of the standard for any tool with its own
    diagram: hand it the canvas size and a node inventory and it returns the
    dict to embed with :func:`manifest_metadata_svg` / :func:`inject_manifest`.

    Args:
        title: A human label for the diagram (or ``None``).
        width, height: Final canvas dimensions; an overlay shares
            ``viewBox="0 0 width height"``.
        nodes: The addressable nodes. Each is a mapping with required ``id``,
            ``x``, ``y``, ``r`` and optional ``label`` (defaults to ``id``),
            ``groups`` (ids of any groups the node belongs to), ``region`` (one
            containing region id), and ``patterns`` (the regexes the node
            matches). Coordinates are rounded here, so pass raw values.
        groups: Optional multi-membership categories; each a mapping with
            ``id``, ``label``, ``color``.
        regions: Optional single-membership containers; each a mapping with
            ``id``, ``label``.
        match_target: Names the runtime string a consumer matches a node's
            ``patterns`` against, recorded in the manifest's ``match`` block.
            Defaults to ``"fqProcessName"`` (a Nextflow run's fully-qualified
            process name); a different runtime sets its own (e.g. ``"stepName"``).
            The match ``type``/``flags`` (case-insensitive regex) are fixed -
            they are the contract both halves rely on.

    Returns:
        The manifest dict, ready for :func:`manifest_json` /
        :func:`manifest_metadata_svg`.
    """
    return {
        "version": MANIFEST_SCHEMA_VERSION,
        "match": {"target": match_target, **_MATCH_TYPE_FLAGS},
        "title": title,
        "width": int(width),
        "height": int(height),
        "groups": [
            {"id": group["id"], "label": group["label"], "color": group["color"]}
            for group in groups
        ],
        "regions": [
            {"id": region["id"], "label": region["label"]} for region in regions
        ],
        "nodes": [_node_entry(node) for node in nodes],
    }


def _node_entry(node: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": node["id"],
        "label": node.get("label") or node["id"],
        "x": _round1(node["x"]),
        "y": _round1(node["y"]),
        "r": _round1(node["r"]),
        "groups": list(node.get("groups", [])),
        "patterns": list(node.get("patterns", [])),
    }
    if node.get("region"):
        entry["region"] = node["region"]
    return entry


def manifest_json(manifest: Mapping[str, Any]) -> str:
    """Serialize a manifest deterministically (sorted keys, compact)."""
    return json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def manifest_metadata_svg(manifest: Mapping[str, Any]) -> str:
    """Return the ``<metadata>`` element carrying the manifest as CDATA JSON.

    CDATA keeps the JSON pristine (no entity escaping of the many quote
    characters).  The only sequence CDATA cannot contain is ``]]>``; on the
    rare chance a regex includes it, split it with the standard idiom so the
    document stays well-formed.  :func:`read_manifest` reverses the split.
    """
    payload = manifest_json(manifest).replace("]]>", "]]]]><![CDATA[>")
    version = manifest.get("version", MANIFEST_SCHEMA_VERSION)
    return (
        f'<metadata id="{MANIFEST_ELEMENT_ID}" '
        f'data-schema-version="{version}">'
        f"<![CDATA[{payload}]]></metadata>"
    )


def overlay_svg(
    manifest: Mapping[str, Any], body: str = "", *, extra_attrs: str = ""
) -> str:
    """An overlay ``<svg>`` sized to the manifest, to stack over the base diagram.

    Returns a transparent SVG layer that shares the base's ``viewBox`` and
    dimensions, so markup in ``body`` (positioned with the manifest's node
    coordinates) lines up exactly when the two are stacked - the contract's
    "an overlay sharing that viewBox lines up exactly", encoded so a consumer
    can't mismatch the coordinate space.  ``extra_attrs`` is verbatim attributes
    for the root element (e.g. ``'class="overlay"'`` or
    ``'style="pointer-events:none"'``).
    """
    width, height = manifest["width"], manifest["height"]
    extra = f" {extra_attrs}" if extra_attrs else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"{extra}>{body}</svg>'
    )


_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*?>", re.IGNORECASE | re.DOTALL)


def inject_manifest(svg: str, manifest: Mapping[str, Any]) -> str:
    """Splice a manifest ``<metadata>`` element into an existing SVG string.

    Inserts immediately after the opening ``<svg ...>`` tag, so a producer that
    draws its diagram by any means can make the result self-describing in one
    call.  Raises :class:`ValueError` if no opening ``<svg>`` tag is found.
    """
    match = _SVG_OPEN_RE.search(svg)
    if match is None:
        raise ValueError("inject_manifest: no opening <svg> tag found")
    cut = match.end()
    return f"{svg[:cut]}{manifest_metadata_svg(manifest)}{svg[cut:]}"


def node_data_attrs(
    *,
    id: str,
    x: float,
    y: float,
    r: float,
    groups: Sequence[str] = (),
    region: str | None = None,
) -> dict[str, Any]:
    """The ``data-node-*`` attribute set for one node's element.

    The DOM-addressable mirror of a manifest node: ``data-node-id`` is the join
    key (equals the manifest ``id``) and the geometry attributes mirror the
    manifest's ``x``/``y``/``r`` (rounded to 1dp), so a consumer can position
    against either half interchangeably.  Returned as a plain dict so a producer
    can spread it onto whatever element it draws.
    """
    attrs: dict[str, Any] = {
        "data-node-id": id,
        "data-node-cx": _round1(x),
        "data-node-cy": _round1(y),
        "data-node-r": _round1(r),
        "data-node-groups": ",".join(groups),
    }
    if region:
        attrs["data-node-region"] = region
    return attrs
