"""The manifest tooling used on its own, with no MetroGraph or renderer.

These tests exercise :mod:`nf_metro.manifest` as the would-be standalone
package: build a manifest from plain node data, embed it in a hand-written SVG,
and read/match it back - the path a *different* diagram tool follows to emit a
conforming, self-describing SVG.  They also guard the property that makes future
extraction simple: the package imports nothing from ``nf_metro`` but itself.
"""

from __future__ import annotations

import ast
import pathlib

import jsonschema
import pytest

from nf_metro.manifest import (
    MANIFEST_ELEMENT_ID,
    MANIFEST_SCHEMA_VERSION,
    build_manifest_data,
    inject_manifest,
    manifest_schema,
    match_node_ids,
    matching_node_ids,
    node_data_attrs,
    overlay_svg,
    read_manifest,
)


def test_build_manifest_data_from_plain_nodes_roundtrips() -> None:
    manifest = build_manifest_data(
        title="My Tool",
        width=400,
        height=300,
        nodes=[
            {
                "id": "a",
                "x": 10.04,
                "y": 20.06,
                "r": 5.0,
                "groups": ["main"],
                "patterns": ["ALIGN.*"],
            },
            {"id": "b", "label": "Bee", "x": 30, "y": 40, "r": 5, "region": "grp"},
        ],
        groups=[{"id": "main", "label": "Main", "color": "#abcdef"}],
        regions=[{"id": "grp", "label": "Group"}],
    )

    assert manifest["version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["match"] == {
        "target": "fqProcessName",
        "type": "regex",
        "flags": "i",
    }
    assert manifest["width"] == 400 and manifest["height"] == 300

    a, b = manifest["nodes"]
    assert (a["x"], a["y"]) == (10.0, 20.1)
    assert a["label"] == "a" and b["label"] == "Bee"
    assert b["patterns"] == [] and b["groups"] == []
    assert b["region"] == "grp" and "region" not in a

    svg = inject_manifest('<svg viewBox="0 0 400 300"></svg>', manifest)
    assert read_manifest(svg) == manifest


def test_match_target_defaults_to_nextflow_and_is_overridable() -> None:
    default = build_manifest_data(title=None, width=1, height=1, nodes=[])
    assert default["match"] == {
        "target": "fqProcessName",
        "type": "regex",
        "flags": "i",
    }
    # A non-Nextflow producer names its own runtime identifier; type/flags fixed.
    custom = build_manifest_data(
        title=None, width=1, height=1, nodes=[], match_target="stepName"
    )
    assert custom["match"] == {"target": "stepName", "type": "regex", "flags": "i"}


def test_inject_manifest_places_metadata_after_opening_svg_tag() -> None:
    manifest = build_manifest_data(title=None, width=10, height=10, nodes=[])
    svg = inject_manifest('<svg width="10" height="10"><rect/></svg>', manifest)
    open_end = svg.index(">") + 1
    assert svg[open_end:].startswith(f'<metadata id="{MANIFEST_ELEMENT_ID}"')


def test_inject_manifest_without_svg_tag_raises() -> None:
    with pytest.raises(ValueError):
        inject_manifest(
            "<html></html>",
            build_manifest_data(title=None, width=1, height=1, nodes=[]),
        )


def test_node_data_attrs_shape_and_rounding() -> None:
    attrs = node_data_attrs(
        id="x", x=1.04, y=2.06, r=5.0, groups=["a", "b"], region="s"
    )
    assert attrs == {
        "data-node-id": "x",
        "data-node-cx": 1.0,
        "data-node-cy": 2.1,
        "data-node-r": 5.0,
        "data-node-groups": "a,b",
        "data-node-region": "s",
    }
    # No region -> the attribute is omitted entirely.
    assert "data-node-region" not in node_data_attrs(id="x", x=0, y=0, r=1)


def test_overlay_svg_shares_the_manifest_viewbox() -> None:
    manifest = build_manifest_data(title=None, width=360, height=92, nodes=[])
    layer = overlay_svg(manifest, "<circle/>", extra_attrs='class="overlay"')
    assert 'viewBox="0 0 360 92"' in layer
    assert 'width="360"' in layer and 'height="92"' in layer
    assert 'class="overlay"' in layer
    assert layer.endswith("<circle/></svg>")


def test_matching_node_ids_is_case_insensitive_and_ordered() -> None:
    patterns = {"align": ["ALIGN.*"], "qc": ["fastqc", "multiqc"], "none": []}
    assert matching_node_ids("nfcore:rnaseq:alignhisat2", patterns) == ["align"]
    assert matching_node_ids("FastQC", patterns) == ["qc"]
    # A target may match more than one node; order follows the mapping.
    multi = {"a": ["shared"], "b": ["SHARED"]}
    assert matching_node_ids("the_shared_one", multi) == ["a", "b"]


def test_consumer_path_on_a_hand_built_svg() -> None:
    """A non-nf-metro producer's SVG is fully drivable from the file alone."""
    manifest = build_manifest_data(
        title="Hand built",
        width=100,
        height=100,
        nodes=[{"id": "trim", "x": 50, "y": 50, "r": 4, "patterns": ["TRIM.*"]}],
    )
    attrs = node_data_attrs(id="trim", x=50, y=50, r=4)
    attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    svg = inject_manifest(
        f'<svg viewBox="0 0 100 100"><g {attr_str}>'
        f'<circle cx="50" cy="50" r="4"/></g></svg>',
        manifest,
    )

    recovered = read_manifest(svg)
    assert recovered is not None
    assert match_node_ids(recovered, "NFCORE:RNASEQ:TRIMGALORE") == ["trim"]
    assert 'data-node-id="trim"' in svg


def test_json_schema_is_valid_and_built_manifest_conforms() -> None:
    schema = manifest_schema()
    jsonschema.Draft202012Validator.check_schema(schema)

    manifest = build_manifest_data(
        title="t",
        width=120,
        height=80,
        nodes=[{"id": "a", "x": 1, "y": 2, "r": 3, "patterns": ["X.*"]}],
        groups=[{"id": "g", "label": "G", "color": "#fff"}],
        regions=[{"id": "r", "label": "R"}],
    )
    jsonschema.validate(manifest, schema)


def test_json_schema_rejects_a_node_missing_geometry() -> None:
    schema = manifest_schema()
    bad = build_manifest_data(title=None, width=10, height=10, nodes=[])
    bad["nodes"] = [{"id": "a"}]  # no x/y/r
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_manifest_package_has_no_nf_metro_dependencies() -> None:
    """The standalone package must import nothing from nf_metro but itself.

    This is the invariant that keeps a future extraction into its own
    distribution a straight directory move.
    """
    pkg = pathlib.Path(__file__).resolve().parents[1] / "src" / "nf_metro" / "manifest"
    offenders: list[str] = []
    for path in pkg.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            else:
                continue
            if module == "nf_metro" or module.startswith("nf_metro."):
                offenders.append(f"{path.name}: {module}")
    assert not offenders, f"nf_metro imports in standalone package: {offenders}"
