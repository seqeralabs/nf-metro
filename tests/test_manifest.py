"""Tests for the embedded SVG manifest contract.

The rendered SVG is a durable, machine-readable contract: a JSON manifest in a
``<metadata>`` element plus ``data-metro-*`` attributes on each station ``<g>``.
These tests assert the manifest round-trips, that its coordinates/processes
match the graph it was built from, that the two halves agree on the station
``id`` join key, and that the documented matching/coordinate semantics hold.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest
from conftest import compute_corpus_layout, content_corpus, parse_and_layout

from nf_metro.live.mapping import stations_for_process
from nf_metro.render.manifest import (
    MANIFEST_SCHEMA_VERSION,
    match_station_ids,
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
    assert {ln["id"] for ln in manifest["lines"]} == {"qc", "align"}
    assert {s["id"] for s in manifest["sections"]} == {"prep", "main"}


def test_svg_without_manifest_returns_none() -> None:
    assert read_manifest("<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None


def test_no_manifest_opt_out_emits_drawn_map_only() -> None:
    """``embed_manifest=False`` drops every data decoration from the SVG.

    The drawn map carries no ``<metadata>`` manifest, no ``data-metro-*``
    attributes, and no station-group wrapper, so it is the lean output the
    gallery's render diff compares against the base branch.
    """
    graph = parse_and_layout(MAPPED_TEXT)
    graph.embed_manifest = False
    svg = render_svg(graph, NFCORE_THEME)

    ET.fromstring(svg)  # well-formed XML
    assert read_manifest(svg) is None
    assert "nf-metro-manifest" not in svg
    assert "data-metro-" not in svg
    assert "nf-metro-station-group" not in svg


def test_no_manifest_drops_only_data_not_glyphs() -> None:
    """The manifest-off SVG draws the same glyphs, minus the identity data.

    Both outputs draw one ``<rect>`` per station; the manifest-off SVG is
    shorter only because it omits the non-visual ``<metadata>`` block and
    ``data-metro-*`` attributes.
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


def test_stations_coords_and_processes_match_graph() -> None:
    graph = parse_and_layout(MAPPED_TEXT)
    svg = render_svg(graph, NFCORE_THEME)
    manifest = read_manifest(svg)

    by_id = {s["id"]: s for s in manifest["stations"]}
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
        assert entry["lines"] == graph.station_lines(sid)
        assert entry["processes"] == graph.process_mapping.get(sid, [])


def test_unmapped_station_has_empty_processes(mapped_svg: str) -> None:
    manifest = read_manifest(mapped_svg)
    fastqc = next(s for s in manifest["stations"] if s["id"] == "fastqc")
    assert fastqc["processes"] == []


def test_id_is_join_key_to_dom(mapped_svg: str) -> None:
    """Every manifest station id is emitted as data-metro-station on a <g>."""
    manifest = read_manifest(mapped_svg)
    for station in manifest["stations"]:
        assert f'data-metro-station="{station["id"]}"' in mapped_svg


def test_data_attrs_mirror_manifest_geometry(mapped_svg: str) -> None:
    """The <g> data-metro-cx/cy/r equal the manifest x/y/r for each station."""
    manifest = read_manifest(mapped_svg)
    root = ET.fromstring(mapped_svg)
    groups = {
        g.get("data-metro-station"): g
        for g in root.iter("{http://www.w3.org/2000/svg}g")
        if g.get("data-metro-station")
    }
    assert groups  # sanity: the namespaced iter found the station groups
    for station in manifest["stations"]:
        g = groups[station["id"]]
        assert float(g.get("data-metro-cx")) == station["x"]
        assert float(g.get("data-metro-cy")) == station["y"]
        assert float(g.get("data-metro-r")) == station["r"]
        assert g.get("data-metro-lines") == ",".join(station["lines"])


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
    """match_station_ids reproduces stations_for_process exactly."""
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
        assert sorted(match_station_ids(manifest, name)) == sorted(
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
    a = next(s for s in parsed["stations"] if s["id"] == "a")
    assert a["processes"] == ["FOO]]>BAR"]


@pytest.mark.parametrize("fixture_id,path,is_nextflow", content_corpus())
def test_corpus_manifest_roundtrips(fixture_id, path, is_nextflow) -> None:
    """Every gallery/topology fixture renders an SVG whose manifest reads back
    and whose station ids all appear as data-metro-station handles."""
    graph = compute_corpus_layout(path, is_nextflow)
    svg = render_svg(graph, NFCORE_THEME)
    manifest = read_manifest(svg)
    assert manifest is not None
    for station in manifest["stations"]:
        assert f'data-metro-station="{station["id"]}"' in svg
        assert station["processes"] == graph.process_mapping.get(station["id"], [])
