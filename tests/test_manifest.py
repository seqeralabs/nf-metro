"""Tests for the embedded SVG manifest contract.

The rendered SVG is a durable, machine-readable contract: a JSON manifest in a
``<metadata>`` element plus ``data-node-*`` attributes on each node ``<g>``.
These tests assert the manifest round-trips, that its coordinates/patterns match
the graph it was built from, that the two halves agree on the node ``id`` join
key, and that the documented matching/coordinate semantics hold. nf-metro feeds
the standalone builder via the adapter, so a metro station becomes a manifest
node, a line a group, and a section a region.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import jsonschema
import pytest
from conftest import compute_corpus_layout, content_corpus, parse_and_layout

from nf_metro.live.mapping import stations_for_process
from nf_metro.render.manifest import (
    MANIFEST_SCHEMA_VERSION,
    manifest_schema,
    match_node_ids,
    read_manifest,
)
from nf_metro.render.svg import render_svg
from nf_metro.themes import NFCORE_THEME

# A small map with sections, multiple lines, and a process mapping so every
# manifest field is exercised.
MAPPED_TEXT = """\
%%metro title: Demo Pipeline
%%metro line: qc    | QC        | #f0a000
%%metro line: align | Alignment | #3a86ff

graph LR
    subgraph prep [Preparation]
        %%metro process: input | SAMPLESHEET
        input[Samplesheet]
        %%metro process: trim | TRIMGALORE
        trim[Trim]
        input -->|qc,align| trim
    end
    subgraph main [Analysis]
        %%metro process: star | STAR_ALIGN
        star[Align]
        fastqc[FastQC]
        trim -->|align| star
        trim -->|qc| fastqc
    end
"""


@pytest.fixture
def mapped_svg() -> str:
    graph = parse_and_layout(MAPPED_TEXT)
    return render_svg(graph, NFCORE_THEME)


def test_manifest_embedded_and_roundtrips(mapped_svg: str) -> None:
    manifest = read_manifest(mapped_svg)
    assert manifest is not None
    assert manifest["version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["title"] == "Demo Pipeline"
    assert {g["id"] for g in manifest["groups"]} == {"qc", "align"}
    assert {r["id"] for r in manifest["regions"]} == {"prep", "main"}


def test_svg_without_manifest_returns_none() -> None:
    assert read_manifest("<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None


def test_no_manifest_opt_out_emits_drawn_map_only() -> None:
    """``embed_manifest=False`` drops every data decoration from the SVG.

    The drawn map carries no ``<metadata>`` manifest, no ``data-node-*``
    attributes, and no station-group wrapper, so it is the lean output the
    gallery's render diff compares against the base branch.
    """
    graph = parse_and_layout(MAPPED_TEXT)
    graph.embed_manifest = False
    svg = render_svg(graph, NFCORE_THEME)

    ET.fromstring(svg)  # well-formed XML
    assert read_manifest(svg) is None
    assert "diagram-manifest" not in svg
    assert "data-node-" not in svg
    assert "nf-metro-station-group" not in svg


def test_no_manifest_drops_only_data_not_glyphs() -> None:
    """The manifest-off SVG draws the same glyphs, minus the identity data.

    Both outputs draw one ``<rect>`` per station; the manifest-off SVG is
    shorter only because it omits the non-visual ``<metadata>`` block and
    ``data-node-*`` attributes.
    """
    graph = parse_and_layout(MAPPED_TEXT)
    on = render_svg(graph, NFCORE_THEME)
    graph.embed_manifest = False
    off = render_svg(graph, NFCORE_THEME)

    rects_on = on.count("<rect")
    rects_off = off.count("<rect")
    assert rects_off == rects_on
    assert len(off) < len(on)


def test_match_block_documents_semantics(mapped_svg: str) -> None:
    """The match block makes the (non-Python) matching contract explicit."""
    manifest = read_manifest(mapped_svg)
    assert manifest["match"] == {
        "target": "fqProcessName",
        "type": "regex",
        "flags": "i",
    }


def test_nodes_coords_and_patterns_match_graph() -> None:
    graph = parse_and_layout(MAPPED_TEXT)
    svg = render_svg(graph, NFCORE_THEME)
    manifest = read_manifest(svg)

    by_id = {n["id"]: n for n in manifest["nodes"]}
    # Ports are excluded; every real (non-port, non-hidden) station is present.
    expected = {
        sid for sid, st in graph.stations.items() if not st.is_port and not st.is_hidden
    }
    assert set(by_id) == expected

    for sid, entry in by_id.items():
        st = graph.stations[sid]
        assert entry["x"] == round(st.x, 1)
        assert entry["y"] == round(st.y, 1)
        assert entry["r"] == round(NFCORE_THEME.station_radius, 1)
        assert entry["groups"] == graph.station_lines(sid)
        assert entry["patterns"] == graph.process_mapping.get(sid, [])


def test_unmapped_node_has_empty_patterns(mapped_svg: str) -> None:
    manifest = read_manifest(mapped_svg)
    fastqc = next(n for n in manifest["nodes"] if n["id"] == "fastqc")
    assert fastqc["patterns"] == []


def test_id_is_join_key_to_dom(mapped_svg: str) -> None:
    """Every manifest node id is emitted as data-node-id on a <g>."""
    manifest = read_manifest(mapped_svg)
    for node in manifest["nodes"]:
        assert f'data-node-id="{node["id"]}"' in mapped_svg


def test_data_attrs_mirror_manifest_geometry(mapped_svg: str) -> None:
    """The <g> data-node-* values equal the manifest entry for each node.

    The manifest and the per-node attributes derive the same geometry, groups,
    and region independently, so this pins them together: a divergence in either
    derivation fails here rather than shipping inconsistent halves.
    """
    manifest = read_manifest(mapped_svg)
    root = ET.fromstring(mapped_svg)
    groups = {
        g.get("data-node-id"): g
        for g in root.iter("{http://www.w3.org/2000/svg}g")
        if g.get("data-node-id")
    }
    assert groups  # sanity: the namespaced iter found the node groups
    for node in manifest["nodes"]:
        g = groups[node["id"]]
        assert float(g.get("data-node-cx")) == node["x"]
        assert float(g.get("data-node-cy")) == node["y"]
        assert float(g.get("data-node-r")) == node["r"]
        assert g.get("data-node-groups") == ",".join(node["groups"])
        assert g.get("data-node-region") == node.get("region")


def test_manifest_arrays_follow_graph_declaration_order() -> None:
    """nodes, groups, and regions keep the graph's declaration order.

    Manifest equality alone would catch a render-to-render reorder but not
    pin the order to anything meaningful; anchoring it to the graph makes an
    ordering regression point straight at the offending derivation.
    """
    graph = parse_and_layout(MAPPED_TEXT)
    manifest = read_manifest(render_svg(graph, NFCORE_THEME))

    expected_nodes = [
        sid for sid, st in graph.stations.items() if not st.is_port and not st.is_hidden
    ]
    expected_regions = [
        sid for sid, sec in graph.sections.items() if not sec.is_implicit
    ]
    assert [n["id"] for n in manifest["nodes"]] == expected_nodes
    assert [g["id"] for g in manifest["groups"]] == list(graph.lines)
    assert [r["id"] for r in manifest["regions"]] == expected_regions


def test_coordinate_space_is_viewbox(mapped_svg: str) -> None:
    """x/y/r are absolute user units in viewBox '0 0 width height', no transform."""
    manifest = read_manifest(mapped_svg)
    vb = re.search(r'<svg[^>]*viewBox="([^"]+)"', mapped_svg).group(1)
    assert vb == f"0 0 {manifest['width']} {manifest['height']}"
    # No outer transform wrapping the content: the first child after <defs>
    # carries raw coordinates, so manifest coords are post-transform absolute.
    root = ET.fromstring(mapped_svg)
    for child in root:
        assert "transform" not in child.attrib


def test_matcher_mirrors_live_server() -> None:
    """match_node_ids reproduces stations_for_process exactly."""
    graph = parse_and_layout(MAPPED_TEXT)
    manifest = read_manifest(render_svg(graph, NFCORE_THEME))
    process_names = [
        "NFCORE:PIPE:SAMPLESHEET",
        "TRIMGALORE",
        "star_align",  # case-insensitive
        "NFCORE:PIPE:STAR_ALIGN",
        "UNMAPPED_PROCESS",
    ]
    for name in process_names:
        assert sorted(match_node_ids(manifest, name)) == sorted(
            stations_for_process(name, graph.process_mapping)
        )


def test_manifest_is_deterministic() -> None:
    graph = parse_and_layout(MAPPED_TEXT)
    a = render_svg(graph, NFCORE_THEME)
    b = render_svg(graph, NFCORE_THEME)
    assert read_manifest(a) == read_manifest(b)


def test_cdata_survives_bracket_sequence() -> None:
    """A pattern containing ']]>' stays well-formed and round-trips."""
    graph = parse_and_layout(
        "%%metro line: x | X | #fff\n"
        "graph LR\n"
        "    %%metro process: a | FOO]]>BAR\n"
        "    a[A]\n"
        "    b[B]\n"
        "    a -->|x| b\n"
    )
    svg = render_svg(graph, NFCORE_THEME)
    ET.fromstring(svg)  # must be well-formed XML
    parsed = read_manifest(svg)
    a = next(n for n in parsed["nodes"] if n["id"] == "a")
    assert a["patterns"] == ["FOO]]>BAR"]


@pytest.mark.parametrize("fixture_id,path,is_nextflow", content_corpus())
def test_corpus_manifest_roundtrips(fixture_id, path, is_nextflow) -> None:
    """Every gallery/topology fixture renders an SVG whose manifest reads back
    and whose node ids all appear as data-node-id handles."""
    graph = compute_corpus_layout(path, is_nextflow)
    svg = render_svg(graph, NFCORE_THEME)
    manifest = read_manifest(svg)
    assert manifest is not None
    jsonschema.validate(manifest, manifest_schema())
    for node in manifest["nodes"]:
        assert f'data-node-id="{node["id"]}"' in svg
        assert node["patterns"] == graph.process_mapping.get(node["id"], [])
