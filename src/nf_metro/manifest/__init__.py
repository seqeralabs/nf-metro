"""The embedded-manifest standard: a self-describing, addressable SVG.

This package is deliberately **dependency-free** (Python standard library only,
no other ``nf_metro`` imports) so it can be lifted into its own distribution
as-is.  It owns the format, the reader, the matcher, and the producer helpers;
nf-metro is one *producer* on top of it (see
:mod:`nf_metro.render.manifest` for the :class:`~nf_metro.parser.model.MetroGraph`
adapter, which maps metro stations/lines/sections onto the neutral
nodes/groups/regions vocabulary below).

The implementation is split across :mod:`._common` (shared constants),
:mod:`.produce` (build and embed), and :mod:`.read` (read back and match); this
module is the public surface that re-exports them.

A rendered SVG becomes a durable contract that a downstream tool can drive -
position overlays, restyle nodes, look up which patterns a node matches - from
the **committed file alone**, with no re-render and no in-memory graph.  The
data is carried two redundant, sanitization-safe ways (no ``<script>``, so it
survives inline-SVG sanitizers):

1. A JSON manifest in a ``<metadata id="diagram-manifest">`` element
   (:func:`build_manifest_data`, :func:`manifest_metadata_svg`).
2. ``data-node-*`` attributes on each node's wrapping ``<g>``
   (:func:`node_data_attrs`).

The two halves join on a node's ``id``: it equals ``data-node-id="<id>"`` on the
element, so a consumer can go manifest->element and element->manifest without
guessing.

Vocabulary
----------
The wire format is tool-neutral: ``nodes`` are the addressable points;
``groups`` are optional multi-membership categories (each with a colour);
``regions`` are optional single-membership containers.  A producer with no such
grouping leaves ``groups``/``regions`` empty and uses ``nodes`` alone.

Coordinate space
----------------
``x``/``y``/``r`` are absolute SVG user units inside the ``viewBox`` declared by
the manifest's ``width``/``height`` (a producer must emit
``viewBox="0 0 width height"`` with no outer transform), so an overlay sharing
that viewBox lines up exactly.  Coordinates are rounded to one decimal place.

Matching
--------
``node.patterns`` are regular expressions matched **case-insensitively** against
a runtime target string (the ``match`` block names the target so a consumer
reproduces the rule; for a Nextflow producer it is the fully-qualified process
name).  Keep patterns within a portable regex subset common to Python ``re`` and
JavaScript ``RegExp`` -- plain character classes, anchors, ``.``/``*``/``+``/
``?``, bounded quantifiers ``{m,n}``, alternation, groups -- so two
implementations cannot diverge.  Avoid Python-only constructs (named groups
``(?P<>)``, inline flags ``(?i)``, possessive quantifiers, ``\\Z``).  A target
may legitimately match more than one node; resolving that is a consumer-side
policy decision, not a schema error.

Forward compatibility
----------------------
Consumers MUST ignore unknown fields so the schema can grow within a major
``version``.  The format is stable as of nf-metro 1.0 and covered by semantic
versioning: incompatible schema changes bump ``version`` and the nf-metro major
version together.  The current schema version is ``"1.0"``.
"""

from __future__ import annotations

from ._common import MANIFEST_ELEMENT_ID, MANIFEST_SCHEMA_VERSION
from .produce import (
    build_manifest_data,
    inject_manifest,
    manifest_json,
    manifest_metadata_svg,
    node_data_attrs,
    overlay_svg,
)
from .read import match_node_ids, matching_node_ids, read_manifest
from .schema import manifest_schema

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_ELEMENT_ID",
    "build_manifest_data",
    "manifest_json",
    "manifest_metadata_svg",
    "inject_manifest",
    "overlay_svg",
    "node_data_attrs",
    "read_manifest",
    "match_node_ids",
    "matching_node_ids",
    "manifest_schema",
]
