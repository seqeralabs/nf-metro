"""CLI <-> %%metro directive parity for layout/render options (issue #553).

Every tunable in the :data:`nf_metro.options.LAYOUT_OPTIONS` registry must be
settable by a directive and overridable by the matching CLI flag, with a
uniform precedence: CLI flag > directive > default. These tests pin the new
directives, the cascade, and a completeness guard that fails if an option ever
exists in only one plane.
"""

from __future__ import annotations

from collections import Counter

import pytest

from nf_metro.api import apply_layout_overrides, resolve_theme
from nf_metro.cli import render
from nf_metro.layout import compute_layout
from nf_metro.options import LAYOUT_OPTIONS
from nf_metro.parser.directives import _GLOBAL_DIRECTIVE_HANDLERS
from nf_metro.parser.mermaid import parse_metro_mermaid
from nf_metro.parser.model import MetroGraph
from nf_metro.themes import THEMES

_LINE = "%%metro line: a | A | #f00\n"
_FLAT = _LINE + "graph LR\n  n1[N1] -->|a| n2[N2]\n"


def _two_sections(extra: str = "") -> str:
    return (
        _LINE
        + extra
        + "graph LR\n"
        + "  subgraph s1 [S1]\n    n1[N1] -->|a| n2[N2]\n  end\n"
        + "  subgraph s2 [S2]\n    n3[N3] -->|a| n4[N4]\n  end\n"
        + "  n2 -->|a| n3\n"
    )


def test_every_option_is_in_both_planes():
    """Each registry option has a directive handler, a CLI flag, and a real field."""
    cli_params = {p.name for p in render.params}
    fields = set(vars(MetroGraph()))
    for opt in LAYOUT_OPTIONS:
        assert opt.name in _GLOBAL_DIRECTIVE_HANDLERS, f"{opt.name}: no directive"
        assert opt.name in cli_params, f"{opt.name}: no CLI flag"
        assert opt.target_attr in fields, (
            f"{opt.name}: no graph field {opt.target_attr}"
        )


def test_no_duplicate_or_shadowed_options():
    """The two planes share one name each: no option is defined twice or shadowed."""
    names = [opt.name for opt in LAYOUT_OPTIONS]
    attrs = [opt.target_attr for opt in LAYOUT_OPTIONS]
    assert len(names) == len(set(names)), "duplicate registry option name"
    assert len(attrs) == len(set(attrs)), "two options write the same graph field"

    # A bespoke handler must not shadow a generated one: the directive a
    # registry option resolves to has to be the registry-generated handler,
    # not a hand-written entry that silently diverges from the CLI.
    for opt in LAYOUT_OPTIONS:
        handler = _GLOBAL_DIRECTIVE_HANDLERS[opt.name]
        assert handler.__qualname__.startswith("_make_layout_option_handler"), (
            f"{opt.name}: directive shadowed by a bespoke handler"
        )

    # No registry flag collides with a hand-written click option on `render`.
    counts = Counter(p.name for p in render.params)
    assert all(c == 1 for c in counts.values()), "a CLI flag is declared twice"


@pytest.mark.parametrize(
    "directive, attr, expected",
    [
        ("x_spacing: 120", "x_spacing", 120.0),
        ("y_spacing: 80", "y_spacing", 80.0),
        ("section_x_gap: 90", "section_x_gap", 90.0),
        ("section_y_gap: 70", "section_y_gap", 70.0),
        ("diamond_style: symmetric", "diamond_style", "symmetric"),
        ("width: 1400", "width", 1400),
        ("height: 900", "height", 900),
        ("animate: true", "animate", True),
    ],
)
def test_new_directive_sets_field(directive, attr, expected):
    graph = parse_metro_mermaid(f"%%metro {directive}\ngraph LR\n")
    assert getattr(graph, attr) == expected


@pytest.mark.parametrize(
    "directive, attr, default",
    [
        ("x_spacing: huge", "x_spacing", None),
        ("x_spacing: -5", "x_spacing", None),
        ("section_x_gap: -1", "section_x_gap", None),
        ("diamond_style: round", "diamond_style", "straight"),
        ("width: tall", "width", None),
    ],
)
def test_new_directive_malformed_ignored(directive, attr, default):
    with pytest.warns(UserWarning):
        graph = parse_metro_mermaid(f"%%metro {directive}\ngraph LR\n")
    assert getattr(graph, attr) == default


def test_cli_override_beats_directive():
    graph = parse_metro_mermaid("%%metro x_spacing: 200\ngraph LR\n")
    assert graph.x_spacing == 200.0
    apply_layout_overrides(graph, {"x_spacing": 60.0})
    assert graph.x_spacing == 60.0
    apply_layout_overrides(graph, {"x_spacing": None})
    assert graph.x_spacing == 60.0


def test_cli_override_skips_parse_time_options():
    """fold_threshold reaches the graph through the parser, not a post-parse setattr."""
    graph = MetroGraph(fold_threshold=99)
    apply_layout_overrides(graph, {"fold_threshold": 5})
    assert graph.fold_threshold == 99


def test_x_spacing_directive_drives_layout():
    base = parse_metro_mermaid(_FLAT)
    compute_layout(base)
    wide = parse_metro_mermaid("%%metro x_spacing: 200\n" + _FLAT)
    compute_layout(wide)
    dx_base = base.stations["n2"].x - base.stations["n1"].x
    dx_wide = wide.stations["n2"].x - wide.stations["n1"].x
    assert dx_wide > dx_base


def test_section_x_gap_directive_drives_layout():
    base = parse_metro_mermaid(_two_sections())
    compute_layout(base)
    wide = parse_metro_mermaid(_two_sections("%%metro section_x_gap: 200\n"))
    compute_layout(wide)
    assert wide.sections["s2"].offset_x > base.sections["s2"].offset_x


def test_style_directive_selects_theme_and_cli_overrides():
    assert resolve_theme(None, MetroGraph(style="light")) is THEMES["light"]
    assert resolve_theme(None, MetroGraph(style="dark")) is THEMES["nfcore"]
    assert resolve_theme("nfcore", MetroGraph(style="light")) is THEMES["nfcore"]


def test_html_output_honours_graph_width_and_animate():
    """width/animate set by directive drive HTML identically to the explicit args."""
    from nf_metro.render.html import render_html

    via_directive = parse_metro_mermaid(
        "%%metro width: 1234\n%%metro animate: true\n" + _FLAT
    )
    compute_layout(via_directive)
    via_arg = parse_metro_mermaid(_FLAT)
    compute_layout(via_arg)
    assert render_html(via_directive, THEMES["nfcore"]) == render_html(
        via_arg, THEMES["nfcore"], width=1234, animate=True
    )
