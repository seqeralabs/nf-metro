"""SVG rendering for metro maps."""

from nf_metro.render.manifest import (
    MANIFEST_ELEMENT_ID,
    MANIFEST_SCHEMA_VERSION,
    build_manifest,
    build_manifest_data,
    inject_manifest,
    manifest_json,
    manifest_metadata_svg,
    manifest_schema,
    match_node_ids,
    matching_node_ids,
    node_data_attrs,
    overlay_svg,
    read_manifest,
)
from nf_metro.render.svg import render_svg
from nf_metro.render.validate import RenderFinding, validate_render

__all__ = [
    "RenderFinding",
    "validate_render",
    "MANIFEST_ELEMENT_ID",
    "MANIFEST_SCHEMA_VERSION",
    "build_manifest",
    "build_manifest_data",
    "inject_manifest",
    "manifest_json",
    "manifest_metadata_svg",
    "manifest_schema",
    "match_node_ids",
    "matching_node_ids",
    "node_data_attrs",
    "overlay_svg",
    "read_manifest",
    "render_svg",
]
