"""Tests for the CLI entry points."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.layout import FoldThresholdError

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RNASEQ_MMD = EXAMPLES_DIR / "rnaseq_sections.mmd"
# Copied into a temp directory by the tests below, so it must name no asset
# that resolves relative to the map file.
STANDALONE_MMD = EXAMPLES_DIR / "variant_calling.mmd"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
INVALID_DIR = FIXTURES_DIR / "invalid"

# Fixtures that parse and pass graph-semantic validation but trip a layout
# invariant once the engine runs.  They exercise the boundary between the bare
# `validate` (semantic only) and `validate --with-layout`.
SEMANTIC_VALID_LAYOUT_BROKEN = [
    "mixed_entry_opposing.mmd",
    "mixed_entry_perpendicular.mmd",
    "backward_feed_rl.mmd",
    "merge_trunk_rightward_source.mmd",
]

DEFERRED_ROUTE_GUARD_FAILURES = [
    (
        "hash_seed_determinism/seed_15.mmd",
        "bundle 's8__exit_left_8'->'s10__entry_right_19' corner (623.0,616.0)",
    ),
    (
        "hash_seed_determinism/seed_77.mmd",
        "undeclared gap channel: line 'l0' (__junction_37->__merge_11) runs "
        "up at x=2158.0",
    ),
]


def test_render_produces_svg(tmp_path):
    """render command produces an SVG file."""
    out = tmp_path / "output.svg"
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    content = out.read_text()
    assert "<svg" in content


def test_render_default_output(tmp_path):
    """render command uses input stem + .svg when no -o given."""
    mmd = tmp_path / "test.mmd"
    mmd.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(mmd)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "test.svg").exists()


def test_validate_success():
    """validate command succeeds on valid input."""
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(RNASEQ_MMD)])
    assert result.exit_code == 0
    assert "Valid:" in result.output


def test_validate_bad_file(tmp_path):
    """Prose the parser finds no station in is an error, not a valid map."""
    bad = tmp_path / "bad.mmd"
    bad.write_text("not a valid mermaid file")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(bad)])
    assert result.exit_code != 0
    assert "defines no stations" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("fixture", SEMANTIC_VALID_LAYOUT_BROKEN)
def test_validate_default_skips_layout(fixture):
    """Bare `validate` is graph-semantic only: a layout-broken but
    semantically valid file passes without running the layout engine."""
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(INVALID_DIR / fixture)])
    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


@pytest.mark.parametrize("fixture", SEMANTIC_VALID_LAYOUT_BROKEN)
def test_validate_with_layout_catches_layout_invariant(fixture):
    """`--with-layout` runs the layout engine and reports a layout failure as
    a clean validation error (not a traceback)."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["validate", "--with-layout", str(INVALID_DIR / fixture)]
    )
    assert result.exit_code != 0
    assert "Validation errors:" in result.output
    assert "Traceback" not in result.output


def test_validate_with_layout_passes_clean_file():
    """`--with-layout` succeeds on a file that lays out cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--with-layout", str(RNASEQ_MMD)])
    assert result.exit_code == 0, result.output
    assert "Valid:" in result.output


@pytest.mark.parametrize("fixture,guard_detail", DEFERRED_ROUTE_GUARD_FAILURES)
def test_validate_with_layout_reports_deferred_route_guard_cleanly(
    fixture: str, guard_detail: str
) -> None:
    result = CliRunner().invoke(
        cli, ["validate", "--with-layout", str(FIXTURES_DIR / fixture)]
    )

    assert result.exit_code == 1
    assert "Validation errors:" in result.output
    assert "Traceback" not in result.output
    assert guard_detail in result.output


def test_validate_with_layout_reports_fold_threshold_cleanly(monkeypatch) -> None:
    def reject(*_args, **_kwargs) -> None:
        raise FoldThresholdError("fold threshold is too small")

    monkeypatch.setattr("nf_metro.cli.compute_layout", reject)

    result = CliRunner().invoke(cli, ["validate", "--with-layout", str(RNASEQ_MMD)])

    assert result.exit_code == 1
    assert "Validation errors:" in result.output
    assert "fold threshold is too small" in result.output
    assert "Traceback" not in result.output


def _td_graph(tmp_path: Path) -> Path:
    """A semantically valid diagram whose non-LR primary direction warns."""
    mmd = tmp_path / "td.mmd"
    mmd.write_text("%%metro line: x | X | #ff0000\ngraph TD\n    a[A] -->|x| b[B]\n")
    return mmd


def test_validate_surfaces_warnings_without_failing(tmp_path):
    """A warning is reported but does not fail the default (non-strict) run."""
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(_td_graph(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "graph LR" in result.output
    assert "Valid:" in result.output


def test_validate_strict_escalates_warning_to_error(tmp_path):
    """`--strict` turns a warning into a non-zero exit."""
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--strict", str(_td_graph(tmp_path))])
    assert result.exit_code != 0
    assert "graph LR" in result.output


def test_info_output():
    """info command prints graph metadata."""
    runner = CliRunner()
    result = runner.invoke(cli, ["info", str(RNASEQ_MMD)])
    assert result.exit_code == 0
    assert "Title:" in result.output
    assert "Stations:" in result.output
    assert "Lines:" in result.output
    assert "Sections:" in result.output


def test_info_default_matches_formatter():
    """Default text output equals the non-verbose formatter, byte-for-byte."""
    from nf_metro.introspect import build_info, format_info_text
    from nf_metro.parser import parse_metro_mermaid

    runner = CliRunner()
    result = runner.invoke(cli, ["info", str(RNASEQ_MMD)])
    assert result.exit_code == 0
    graph = parse_metro_mermaid(RNASEQ_MMD.read_text())
    expected = format_info_text(build_info(graph), verbose=False)
    assert result.output == expected + "\n"
    # The verbose-only sections must not leak into the default output.
    assert "Section dependency graph:" not in result.output


def test_info_json_is_valid_and_structured():
    """--json emits parseable JSON carrying the full introspection structure."""
    import json

    runner = CliRunner()
    result = runner.invoke(cli, ["info", str(RNASEQ_MMD), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["title"] == "nf-core/rnaseq"
    assert {"counts", "lines", "sections", "ports", "junctions", "section_dag"} <= set(
        data
    )
    assert data["section_dag"]["edges"]


def test_info_verbose_adds_introspection():
    """--verbose appends the richer sections to the stable summary."""
    runner = CliRunner()
    result = runner.invoke(cli, ["info", str(RNASEQ_MMD), "--verbose"])
    assert result.exit_code == 0
    assert "Section dependency graph:" in result.output
    assert "Per-line routes:" in result.output
    assert "Ports (synthetic):" in result.output


def test_info_captures_parse_warnings(tmp_path):
    """A non-LR primary graph direction surfaces as a captured warning."""
    import json

    mmd = tmp_path / "tb.mmd"
    mmd.write_text(
        "%%metro title: TB warn\n"
        "%%metro line: a | A | #ff0000\n"
        "graph TB\n"
        "    x[X] -->|a| y[Y]\n"
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["info", str(mmd), "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any("LR" in w for w in data["warnings"])


def test_version():
    """--version flag prints version string."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


def test_render_with_theme(tmp_path):
    """render command accepts --theme flag."""
    out = tmp_path / "output.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(RNASEQ_MMD), "-o", str(out), "--theme", "light"]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_render_svg_ends_with_newline(tmp_path):
    """SVG output ends with a trailing newline (nf-core end-of-file-fixer)."""
    out = tmp_path / "output.svg"
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out)])
    assert result.exit_code == 0, result.output
    content = out.read_text()
    assert content.endswith("\n"), "SVG output must end with a trailing newline"


def test_render_section_gap_options(tmp_path):
    """render command accepts --section-x-gap and --section-y-gap flags."""
    out = tmp_path / "output.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "render",
            str(RNASEQ_MMD),
            "-o",
            str(out),
            "--section-x-gap",
            "80",
            "--section-y-gap",
            "60",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_render_nonexistent_file():
    """render command fails gracefully on missing input."""
    runner = CliRunner()
    result = runner.invoke(cli, ["render", "/nonexistent/file.mmd"])
    assert result.exit_code != 0


def test_render_unexpected_exception_becomes_click_exception(tmp_path, monkeypatch):
    """An exception type outside the pipeline's known errors becomes a clean error."""

    def _boom(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.delenv("NF_METRO_DEBUG", raising=False)
    monkeypatch.setattr("nf_metro.cli.render_graph_result", _boom)
    src = tmp_path / "a.mmd"
    src.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(src), "-o", str(tmp_path / "out.svg")])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "unexpected error" in result.output


def test_render_unexpected_exception_reraises_under_debug_env(tmp_path, monkeypatch):
    """NF_METRO_DEBUG=1 re-raises the original exception instead of wrapping it."""

    def _boom(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setenv("NF_METRO_DEBUG", "1")
    monkeypatch.setattr("nf_metro.cli.render_graph_result", _boom)
    src = tmp_path / "a.mmd"
    src.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(src), "-o", str(tmp_path / "out.svg")])
    assert isinstance(result.exception, KeyError)


def test_render_permissive_flag_reports_downgraded_guards(tmp_path, monkeypatch):
    """``--permissive`` renders through a guard warning instead of aborting,
    and reports what was downgraded on stderr."""
    import warnings

    from nf_metro.api import render_graph_result as real_render_graph
    from nf_metro.parser.model import PermissiveGuardWarning

    def _render_graph_with_warning(*args, **kwargs):
        warnings.warn(
            "synthetic guard trip for CLI test",
            category=PermissiveGuardWarning,
            stacklevel=2,
        )
        return real_render_graph(*args, **kwargs)

    monkeypatch.setattr("nf_metro.cli.render_graph_result", _render_graph_with_warning)
    src = tmp_path / "a.mmd"
    src.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", "--permissive", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out.svg").exists()
    assert "--permissive: 1 guard(s) downgraded" in result.output
    assert "synthetic guard trip for CLI test" in result.output


def test_render_multiple_files(tmp_path):
    """render accepts more than one INPUT_FILE, each to its own sibling output."""
    a = tmp_path / "a.mmd"
    b = tmp_path / "b.mmd"
    a.write_text(STANDALONE_MMD.read_text())
    b.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "a.svg").exists()
    assert (tmp_path / "b.svg").exists()
    assert "[1/2] OK" in result.output
    assert "[2/2] OK" in result.output


def test_render_multiple_files_rejects_output_flag(tmp_path):
    """-o/--output is only valid with a single INPUT_FILE."""
    a = tmp_path / "a.mmd"
    b = tmp_path / "b.mmd"
    a.write_text(STANDALONE_MMD.read_text())
    b.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(a), str(b), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code != 0
    assert "single INPUT_FILE" in result.output


def test_render_multiple_files_partial_failure(tmp_path):
    """One failing file doesn't block the rest, but the overall exit is non-zero."""
    bad = tmp_path / "bad.mmd"
    good = tmp_path / "good.mmd"
    bad.write_text(
        "%%metro title: Bad\n"
        "%%metro logo: nonexistent-logo-file.png\n"
        "%%metro line: x | X | #0570b0\n"
        "graph LR\n"
        "    a[Foo] -->|x| b[Bar]\n"
    )
    good.write_text(STANDALONE_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(bad), str(good)])
    assert result.exit_code != 0
    assert "[1/2] FAIL" in result.output
    assert "[2/2] OK" in result.output
    assert not (tmp_path / "bad.svg").exists()
    assert (tmp_path / "good.svg").exists()


def test_render_center_ports_cli_flag_accepted(tmp_path):
    """--center-ports / --no-center-ports flags both render successfully."""
    out = tmp_path / "out.svg"
    runner = CliRunner()
    for flag in ("--center-ports", "--no-center-ports"):
        result = runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out), flag])
        assert result.exit_code == 0, f"{flag}: {result.output}"
        assert out.exists()


def test_render_center_ports_cli_overrides_directive(tmp_path, monkeypatch):
    """CLI --no-center-ports overrides a mmd %%metro center_ports: true directive."""
    from nf_metro.parser.mermaid import parse_metro_mermaid

    captured: dict = {}

    import nf_metro.api as api_mod

    original_compute_layout = api_mod.compute_layout

    def spy_compute_layout(graph, **kw):
        captured["center_ports"] = graph.center_ports
        return original_compute_layout(graph, **kw)

    monkeypatch.setattr(api_mod, "compute_layout", spy_compute_layout)

    mmd_text = "%%metro center_ports: true\n" + STANDALONE_MMD.read_text()
    mmd = tmp_path / "with_directive.mmd"
    mmd.write_text(mmd_text)
    out = tmp_path / "out.svg"
    runner = CliRunner()

    # Directive alone -> True
    result = runner.invoke(cli, ["render", str(mmd), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert captured["center_ports"] is True

    # CLI --no-center-ports overrides directive
    result = runner.invoke(
        cli, ["render", str(mmd), "-o", str(out), "--no-center-ports"]
    )
    assert result.exit_code == 0, result.output
    assert captured["center_ports"] is False

    # Sanity check: parser alone preserves the directive
    parsed = parse_metro_mermaid(mmd_text)
    assert parsed.center_ports is True


def test_validate_svg_success(tmp_path):
    """validate-svg passes on a freshly rendered (manifest-on) SVG."""
    out = tmp_path / "map.svg"
    runner = CliRunner()
    rendered = runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out)])
    assert rendered.exit_code == 0, rendered.output
    result = runner.invoke(cli, ["validate-svg", str(out)])
    assert result.exit_code == 0, result.output
    assert "Valid" in result.output


def test_validate_svg_no_manifest(tmp_path):
    """validate-svg fails when the SVG carries no manifest (--no-manifest)."""
    out = tmp_path / "map.svg"
    runner = CliRunner()
    runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out), "--no-manifest"])
    result = runner.invoke(cli, ["validate-svg", str(out)])
    assert result.exit_code == 1


def test_render_responsive_flag_omits_fixed_dimensions(tmp_path):
    """--responsive omits fixed width/height from root <svg> element."""
    import xml.etree.ElementTree as ET

    out = tmp_path / "responsive.svg"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["render", str(RNASEQ_MMD), "-o", str(out), "--responsive"]
    )
    assert result.exit_code == 0, result.output
    root = ET.fromstring(out.read_text())
    assert root.get("width") is None
    assert root.get("height") is None
    assert root.get("viewBox") is not None
    assert root.get("preserveAspectRatio") == "xMinYMin meet"


def test_validate_svg_rejects_nonconforming(tmp_path):
    """validate-svg fails when the embedded manifest violates the schema."""
    import re

    out = tmp_path / "map.svg"
    runner = CliRunner()
    runner.invoke(cli, ["render", str(RNASEQ_MMD), "-o", str(out)])
    out.write_text(re.sub(r'"r":[0-9.]+,', "", out.read_text(), count=1))
    result = runner.invoke(cli, ["validate-svg", str(out)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# render-many tests
# ---------------------------------------------------------------------------

SIMPLE_MMD = Path(__file__).resolve().parent / "fixtures" / "da_pipeline.mmd"


def _write_manifest(tmp_path: Path, jobs: list[dict]) -> Path:
    import json

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(jobs))
    return manifest


def test_render_many_single_job(tmp_path):
    """render-many with one job produces a valid SVG."""
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out)}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "<svg" in out.read_text()


def test_render_many_multiple_jobs(tmp_path):
    """render-many processes multiple jobs in one invocation."""
    outs = [tmp_path / f"out{i}.svg" for i in range(3)]
    jobs = [{"input": str(RNASEQ_MMD), "output": str(out)} for out in outs]
    manifest = _write_manifest(tmp_path, jobs)
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    for out in outs:
        assert out.exists()
        assert "<svg" in out.read_text()


def test_render_many_creates_output_dirs(tmp_path):
    """render-many creates missing parent directories for output paths."""
    out = tmp_path / "nested" / "dir" / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out)}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_render_many_empty_manifest(tmp_path):
    """render-many with an empty manifest exits zero and does nothing."""
    manifest = _write_manifest(tmp_path, [])
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output


def test_render_many_output_matches_single_render(tmp_path):
    """render-many SVG output is byte-identical to nf-metro render with same options."""
    single_out = tmp_path / "single.svg"
    batch_out = tmp_path / "batch.svg"

    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "render",
            str(RNASEQ_MMD),
            "-o",
            str(single_out),
            "--no-self-color-scheme",
            "--no-manifest",
        ],
    )

    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(batch_out),
                "no_self_color_scheme": True,
                "layout_options": {"manifest": False},
            }
        ],
    )
    runner.invoke(cli, ["render-many", str(manifest)])

    assert single_out.read_text() == batch_out.read_text()


def test_render_many_partial_failure_continues(tmp_path):
    """render-many continues past a bad job and reports the failure count."""
    good_out = tmp_path / "good.svg"
    manifest = _write_manifest(
        tmp_path,
        [
            {"input": "/nonexistent/missing.mmd", "output": str(tmp_path / "bad.svg")},
            {"input": str(RNASEQ_MMD), "output": str(good_out)},
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert good_out.exists(), "successful job must still produce output"
    assert "1/2" in result.output


def test_render_many_rejects_non_positive_scale(tmp_path):
    """A manifest job's scale gets the same bound as --scale, not a raw resvg error."""
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(tmp_path / "out.png"),
                "format": "png",
                "scale": 0,
            }
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "not in the range" in result.output


def test_render_many_rejects_non_positive_png_width(tmp_path):
    """A manifest job's png_width of 0 is rejected, not silently treated as unset."""
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(tmp_path / "out.png"),
                "format": "png",
                "png_width": 0,
            }
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "not in the range" in result.output


@pytest.mark.parametrize("key", ["scale", "png_width"])
def test_render_many_rejects_boolean_numeric_values(tmp_path, key):
    """A JSON true/false for scale/png_width is refused, not coerced to 1/0."""
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(tmp_path / "out.png"),
                "format": "png",
                key: True,
            }
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "is not a number" in result.output


def test_render_many_rejects_unknown_format(tmp_path):
    """An unrecognised format is refused, not silently rendered as SVG."""
    out = tmp_path / "out.jpeg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out), "format": "jpeg"}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "not one of" in result.output
    assert not out.exists()


def test_render_many_bad_manifest_json(tmp_path):
    """render-many fails cleanly on unparseable manifest JSON."""
    manifest = tmp_path / "bad.json"
    manifest.write_text("{not valid json")
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "cannot read manifest" in result.output


def test_render_many_manifest_not_array(tmp_path):
    """render-many fails cleanly when the manifest is not a JSON array."""
    import json

    manifest = tmp_path / "obj.json"
    manifest.write_text(json.dumps({"input": "foo", "output": "bar"}))
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code != 0
    assert "JSON array" in result.output


def test_render_many_layout_options_dict(tmp_path):
    """layout_options dict passes arbitrary layout overrides to the engine."""
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(out),
                "layout_options": {"manifest": False},
            }
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    assert "<metadata" not in out.read_text()


def test_render_many_theme_option(tmp_path):
    """theme key selects an alternate brand theme."""
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out), "theme": "light"}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_render_many_html_format(tmp_path):
    """format: html produces a self-contained HTML page."""
    out = tmp_path / "out.html"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out), "format": "html"}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    content = out.read_text()
    assert "<!DOCTYPE html" in content or "<html" in content
    assert "<svg" in content


def test_render_many_svg_class_prefix(tmp_path):
    """svg_class_prefix is applied to SVG presentation class names."""
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "input": str(RNASEQ_MMD),
                "output": str(out),
                "svg_class_prefix": "myapp",
            }
        ],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    assert "myapp-nf-metro-" in out.read_text()


def test_render_many_trailing_newline(tmp_path):
    """render-many output ends with a trailing newline."""
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(RNASEQ_MMD), "output": str(out)}],
    )
    CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert out.read_text().endswith("\n")


_INACTIVE_MMD = (
    "%%metro line: a | Line A | #ff0000 | solid | inactive\n"
    "%%metro line: b | Line B | #0000ff\n"
    "graph LR\n"
    "    x[X] -->|a| y[Y]\n"
    "    y -->|b| z[Z]\n"
)


def test_render_inactive_lines_flag_mutes(tmp_path):
    """--inactive-lines greys the named line's stroke."""
    src = tmp_path / "map.mmd"
    src.write_text(
        "%%metro line: a | Line A | #ff0000\n"
        "%%metro line: b | Line B | #0000ff\n"
        "graph LR\n"
        "    x[X] -->|a| y[Y]\n"
        "    y -->|b| z[Z]\n"
    )
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(out), "--inactive-lines", "a"]
    )
    assert result.exit_code == 0, result.output
    svg = out.read_text()
    assert 'stroke="#ff0000"' not in svg
    assert 'stroke="#888888"' in svg


def test_render_unknown_inactive_line_errors(tmp_path):
    """An unknown --inactive-lines id fails with a clear message."""
    src = tmp_path / "map.mmd"
    src.write_text(
        "%%metro line: a | Line A | #ff0000\ngraph LR\n    x[X] -->|a| y[Y]\n"
    )
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(out), "--inactive-lines", "nope"]
    )
    assert result.exit_code != 0
    assert "nope" in result.output


def test_render_declared_inactive_used_without_flag(tmp_path):
    """A line declared inactive in the .mmd greys with no CLI flag."""
    src = tmp_path / "map.mmd"
    src.write_text(_INACTIVE_MMD)
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(cli, ["render", str(src), "-o", str(out)])
    assert result.exit_code == 0, result.output
    svg = out.read_text()
    assert 'stroke="#ff0000"' not in svg
    assert 'stroke="#888888"' in svg


def test_render_empty_flag_forces_all_active(tmp_path):
    """--inactive-lines '' overrides a declared-inactive line back to full colour."""
    src = tmp_path / "map.mmd"
    src.write_text(_INACTIVE_MMD)
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(out), "--inactive-lines", ""]
    )
    assert result.exit_code == 0, result.output
    svg = out.read_text()
    assert 'stroke="#ff0000"' in svg
    assert 'stroke="#0000ff"' in svg


def test_render_many_inactive_lines_list(tmp_path):
    """render-many accepts an inactive_lines job key as a JSON list."""
    src = tmp_path / "map.mmd"
    src.write_text(_INACTIVE_MMD)
    out = tmp_path / "out.svg"
    manifest = _write_manifest(
        tmp_path,
        [{"input": str(src), "output": str(out), "inactive_lines": ["b"]}],
    )
    result = CliRunner().invoke(cli, ["render-many", str(manifest)])
    assert result.exit_code == 0, result.output
    svg = out.read_text()
    assert 'stroke="#ff0000"' in svg  # a restored to full colour
    assert 'stroke="#0000ff"' not in svg  # b muted


def _simple_map(tmp_path: Path, *, prelude: str = "") -> Path:
    """Write a renderable one-section, one-line map, *prelude* first."""
    mmd = tmp_path / "map.mmd"
    mmd.write_text(
        prelude + "%%metro line: a | A | #ff0000\n"
        "graph LR\n"
        "    subgraph s1 [One]\n"
        "        n1[N1] -->|a| n2[N2]\n"
        "    end\n"
    )
    return mmd


_UNKNOWN_DIRECTIVE = "%%metro titel: Typo\n"
_UNKNOWN_DIRECTIVE_WARNING = "%%metro titel: unknown directive; ignoring"


def test_render_empty_source_names_file_and_cause(tmp_path):
    """An empty source is refused with an actionable message, not an internal one."""
    src = tmp_path / "empty.mmd"
    src.write_text("")
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code != 0
    assert str(src) in result.output
    assert "defines no stations" in result.output
    assert "max()" not in result.output


def test_render_source_without_recognised_stations_is_refused(tmp_path):
    """Prose the grammar finds no station in is refused as station-less."""
    src = tmp_path / "prose.mmd"
    src.write_text("this is not mermaid at all\n")
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code != 0
    assert "defines no stations" in result.output


def test_validate_rejects_a_station_less_map(tmp_path):
    """`validate` and `render` accept the same maps: neither takes an empty one."""
    src = tmp_path / "empty.mmd"
    src.write_text("")

    validated = CliRunner().invoke(cli, ["validate", str(src)])
    assert validated.exit_code != 0, validated.output
    assert "Validation errors:" in validated.output
    assert "defines no stations" in validated.output

    rendered = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert rendered.exit_code != 0


@pytest.mark.parametrize(
    "source, expected",
    [
        ("", "EmptyGraphError"),
        (None, "ValueError"),
    ],
    ids=["station-less", "render-rejection"],
)
def test_render_error_reraises_under_debug_env(tmp_path, monkeypatch, source, expected):
    """NF_METRO_DEBUG=1 surfaces the typed error itself, not a one-line message."""

    def _reject(*args, **kwargs):
        raise ValueError("synthetic render rejection")

    monkeypatch.setenv("NF_METRO_DEBUG", "1")
    src = tmp_path / "a.mmd"
    if source is None:
        monkeypatch.setattr("nf_metro.cli.render_graph_result", _reject)
        src.write_text(RNASEQ_MMD.read_text())
    else:
        src.write_text(source)
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert type(result.exception).__name__ == expected


def test_render_rejection_is_one_line_without_debug_env(tmp_path, monkeypatch):
    """A recognised rejection prints as one message naming the source file."""

    def _reject(*args, **kwargs):
        raise ValueError("synthetic render rejection")

    monkeypatch.delenv("NF_METRO_DEBUG", raising=False)
    monkeypatch.setattr("nf_metro.cli.render_graph_result", _reject)
    src = tmp_path / "a.mmd"
    src.write_text(STANDALONE_MMD.read_text())
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code != 0
    assert f"Error: {src}: synthetic render rejection" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--font-scale", "0"),
        ("--font-scale", "-2"),
        ("--stroke-scale", "0"),
        ("--logo-scale", "0"),
        ("--x-spacing", "0"),
        ("--x-spacing", "-500"),
        ("--y-spacing", "0"),
        ("--section-x-gap", "-10"),
        ("--section-y-gap", "-10"),
        ("--legend-min-height", "-5"),
        ("--legend-logo-gap", "-1"),
        ("--track-gap", "99"),
        ("--fold-threshold", "0"),
        ("--width", "0"),
        ("--height", "-3"),
    ],
)
def test_render_rejects_out_of_range_numeric_option(tmp_path, flag, value):
    """A numeric flag outside its declared bounds fails before any render work."""
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli, ["render", str(RNASEQ_MMD), "-o", str(out), flag, value]
    )
    assert result.exit_code == 2, result.output
    assert f"Invalid value for '{flag}'" in result.output
    assert not out.exists()


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--font-scale", "1.5"),
        ("--stroke-scale", "2"),
        ("--track-gap", "0"),
        ("--section-x-gap", "0"),
        ("--label-angle", "-45"),
        ("--width", "1400"),
    ],
)
def test_render_accepts_in_range_numeric_option(tmp_path, flag, value):
    """One in-range value per bound style reaches the render."""
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli, ["render", str(_simple_map(tmp_path)), "-o", str(out), flag, value]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_numeric_flag_help_keeps_its_plain_metavar():
    """A bounded flag documents its bound without renaming its value type."""
    result = CliRunner().invoke(cli, ["render", "--help"])
    assert result.exit_code == 0
    help_text = " ".join(result.output.split())
    assert "--x-spacing FLOAT" in help_text
    assert "--width INTEGER" in help_text
    assert "FLOAT RANGE" not in help_text
    assert "INTEGER RANGE" not in help_text


def test_render_presents_parse_warnings_as_a_clean_block(tmp_path):
    """A parse warning reaches the user as a labelled block, not raw Python."""
    src = _simple_map(tmp_path, prelude=_UNKNOWN_DIRECTIVE)
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code == 0, result.output
    assert f"Warnings:\n  - {_UNKNOWN_DIRECTIVE_WARNING}" in result.output
    assert "UserWarning" not in result.output
    assert "directives.py" not in result.output


def test_render_presents_layout_warnings_as_a_clean_block(tmp_path, monkeypatch):
    """A warning raised while the layout runs gets the same presentation."""
    import warnings as warnings_module

    from nf_metro.api import prepare_graph as real_prepare_graph

    def _prepare_with_warning(*args, **kwargs):
        graph = real_prepare_graph(*args, **kwargs)
        warnings_module.warn("synthetic layout adjustment", stacklevel=2)
        return graph

    monkeypatch.setattr("nf_metro.cli.prepare_graph", _prepare_with_warning)
    result = CliRunner().invoke(
        cli, ["render", str(_simple_map(tmp_path)), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code == 0, result.output
    assert "Warnings:\n  - synthetic layout adjustment" in result.output
    assert "UserWarning" not in result.output


def test_render_presents_warnings_when_the_render_fails(tmp_path):
    """Warnings raised before a fatal error are still presented."""
    src = tmp_path / "warned_empty.mmd"
    src.write_text(_UNKNOWN_DIRECTIVE + "graph LR\n")
    result = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(tmp_path / "out.svg")]
    )
    assert result.exit_code != 0
    assert _UNKNOWN_DIRECTIVE_WARNING in result.output
    assert "defines no stations" in result.output


def test_batch_render_labels_each_file_warnings(tmp_path):
    """With several inputs, a warning block names the file that raised it."""
    good = _simple_map(tmp_path)
    warned = tmp_path / "warned.mmd"
    warned.write_text(_UNKNOWN_DIRECTIVE + good.read_text())
    result = CliRunner().invoke(cli, ["render", str(good), str(warned)])
    assert result.exit_code == 0, result.output
    assert f"Warnings ({warned})" in result.output


def test_permissive_guard_block_names_the_flag_only_when_passed(tmp_path):
    """A guard downgrade is reported either way, credited to --permissive when set."""
    import warnings as warnings_module

    from nf_metro.api import render_graph_result as real_render_graph
    from nf_metro.parser.model import PermissiveGuardWarning

    def _render_with_guard_warning(*args, **kwargs):
        warnings_module.warn(
            "synthetic guard trip",
            category=PermissiveGuardWarning,
            stacklevel=2,
        )
        return real_render_graph(*args, **kwargs)

    src = _simple_map(tmp_path)
    for extra, expected in ((["--permissive"], "--permissive: 1 guard(s)"), ([], None)):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("nf_metro.cli.render_graph_result", _render_with_guard_warning)
            result = CliRunner().invoke(
                cli, ["render", str(src), "-o", str(tmp_path / "out.svg"), *extra]
            )
        assert result.exit_code == 0, result.output
        assert "1 guard(s) downgraded to warnings" in result.output
        assert "synthetic guard trip" in result.output
        if expected is None:
            assert "--permissive" not in result.output
        else:
            assert expected in result.output


def test_info_presents_warnings_as_a_clean_block(tmp_path):
    """`info` surfaces captured warnings on stderr instead of dropping them."""
    src = _simple_map(tmp_path, prelude=_UNKNOWN_DIRECTIVE)
    result = CliRunner().invoke(cli, ["info", str(src)])
    assert result.exit_code == 0, result.output
    assert f"Warnings:\n  - {_UNKNOWN_DIRECTIVE_WARNING}" in result.output
    assert "UserWarning" not in result.output


def test_info_verbose_carries_warnings_once(tmp_path):
    """The verbose report lists the warnings, so stderr adds no second copy."""
    src = _simple_map(tmp_path, prelude=_UNKNOWN_DIRECTIVE)
    result = CliRunner().invoke(cli, ["info", str(src), "--verbose"])
    assert result.exit_code == 0, result.output
    assert result.output.count(_UNKNOWN_DIRECTIVE_WARNING) == 1


@pytest.mark.parametrize(
    "style, expected",
    [
        ("", "nfcore"),
        ("%%metro style: dark\n", "nfcore"),
        ("%%metro style: seqera\n", "seqera"),
        ("%%metro style: nonesuch\n", "nfcore"),
    ],
)
def test_info_reports_the_resolved_theme(tmp_path, style, expected):
    """`info` names a theme `--theme` accepts, whatever the map asked for."""
    src = _simple_map(tmp_path, prelude=style)
    result = CliRunner().invoke(cli, ["info", str(src)])
    assert result.exit_code == 0, result.output
    assert f"Style: {expected}" in result.output


def test_render_validate_help_scopes_its_promise():
    """`--validate` names the guards it runs and points elsewhere for Tier-A."""
    result = CliRunner().invoke(cli, ["render", "--help"])
    assert result.exit_code == 0
    help_text = " ".join(result.output.split())
    assert "route drawn through a station's label or marker" in help_text
    assert "use --strict to fail on those" in help_text


def test_manifest_flag_is_an_undocumented_escape_hatch(tmp_path):
    """The manifest opt-out works while staying out of `--help`."""
    help_result = CliRunner().invoke(cli, ["render", "--help"])
    assert help_result.exit_code == 0
    assert "--manifest" not in help_result.output
    assert "--no-manifest" not in help_result.output

    out = tmp_path / "bare.svg"
    result = CliRunner().invoke(
        cli, ["render", str(_simple_map(tmp_path)), "-o", str(out), "--no-manifest"]
    )
    assert result.exit_code == 0, result.output
    assert "diagram-manifest" not in out.read_text()


def test_manifest_flag_overrides_a_map_that_opted_out(tmp_path):
    """`--manifest` restores the manifest `%%metro manifest: false` turned off."""
    src = _simple_map(tmp_path, prelude="%%metro manifest: false\n")
    without = tmp_path / "without.svg"
    with_flag = tmp_path / "with.svg"

    plain = CliRunner().invoke(cli, ["render", str(src), "-o", str(without)])
    assert plain.exit_code == 0, plain.output
    assert "diagram-manifest" not in without.read_text()

    overridden = CliRunner().invoke(
        cli, ["render", str(src), "-o", str(with_flag), "--manifest"]
    )
    assert overridden.exit_code == 0, overridden.output
    assert "diagram-manifest" in with_flag.read_text()


def test_theme_option_accepts_every_style_directive_value(tmp_path):
    """`--theme` takes the values `%%metro style:` takes, alias included."""
    from nf_metro.themes import STYLE_NAMES

    src = _simple_map(tmp_path)
    for name in sorted(STYLE_NAMES):
        out = tmp_path / f"{name}.svg"
        result = CliRunner().invoke(
            cli, ["render", str(src), "-o", str(out), "--theme", name]
        )
        assert result.exit_code == 0, f"--theme {name}: {result.output}"
        assert out.exists()


def test_theme_alias_renders_as_the_brand_it_names(tmp_path):
    """`--theme dark` draws what `--theme nfcore` draws."""
    src = _simple_map(tmp_path)
    aliased = tmp_path / "aliased.svg"
    canonical = tmp_path / "canonical.svg"
    for target, name in ((aliased, "dark"), (canonical, "nfcore")):
        result = CliRunner().invoke(
            cli, ["render", str(src), "-o", str(target), "--theme", name]
        )
        assert result.exit_code == 0, result.output
    assert aliased.read_text() == canonical.read_text()


class _ServerStarted(Exception):
    """Raised by a stub server to end the command before it binds a port."""


def test_serve_multi_accepts_the_theme_alias(monkeypatch):
    """The alias resolves before `serve-multi` looks the theme up."""
    from nf_metro.themes import THEMES

    started: dict[str, object] = {}

    def _stub_serve_multi(theme, **kwargs):
        started["theme"] = theme
        raise _ServerStarted

    monkeypatch.setattr("nf_metro.live.server.serve_multi", _stub_serve_multi)
    result = CliRunner().invoke(cli, ["serve-multi", "--theme", "dark"])
    assert isinstance(result.exception, _ServerStarted), result.output
    assert started["theme"] is THEMES["nfcore"]


@pytest.mark.parametrize(
    "command, label",
    [
        ("validate", "Validation warnings"),
        ("info", "Warnings"),
        ("explain", "Warnings"),
    ],
)
def test_parse_failure_still_reports_earlier_warnings(tmp_path, command, label):
    """A warning naming the fault outlives the error naming its consequence."""
    src = tmp_path / "warned.mmd"
    src.write_text(_UNKNOWN_DIRECTIVE + "graph LR\n    a[A] --> b[B]\n")
    result = CliRunner().invoke(cli, [command, str(src)])
    assert result.exit_code != 0
    assert f"{label}:\n  - {_UNKNOWN_DIRECTIVE_WARNING}" in result.output
    assert "no metro line annotation" in result.output


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400"])
def test_render_rejects_a_non_finite_numeric_option(tmp_path, value):
    """A non-finite scale would reach the SVG as an unusable attribute."""
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(
        cli,
        ["render", str(_simple_map(tmp_path)), "-o", str(out), "--font-scale", value],
    )
    assert result.exit_code == 2, result.output
    assert "Invalid value for '--font-scale'" in result.output
    assert not out.exists()


def test_batch_failure_names_its_file_once(tmp_path):
    """A batch labels each job with its file, so the message must not repeat it."""
    good = _simple_map(tmp_path)
    empty = tmp_path / "empty.mmd"
    empty.write_text("")
    result = CliRunner().invoke(cli, ["render", str(good), str(empty)])
    assert result.exit_code != 0
    fail_line = next(
        line
        for line in result.output.splitlines()
        if "FAIL" in line and "empty" in line
    )
    assert fail_line.count("empty.mmd") == 1, fail_line


def test_batch_failure_reraises_under_debug_env(tmp_path, monkeypatch):
    """NF_METRO_DEBUG=1 reaches a batch too, where the failing input is unknown."""
    monkeypatch.setenv("NF_METRO_DEBUG", "1")
    good = _simple_map(tmp_path)
    empty = tmp_path / "empty.mmd"
    empty.write_text("")
    result = CliRunner().invoke(cli, ["render", str(good), str(empty)])
    assert type(result.exception).__name__ == "EmptyGraphError"


def test_validate_geometry_refuses_a_map_without_a_manifest(tmp_path):
    """The drawn-geometry guards read the SVG through its manifest."""
    src = _simple_map(tmp_path, prelude="%%metro manifest: false\n")
    out = tmp_path / "out.svg"
    result = CliRunner().invoke(cli, ["render", str(src), "-o", str(out), "--validate"])
    assert result.exit_code != 0
    assert "embedded manifest" in result.output

    without_flag = CliRunner().invoke(cli, ["render", str(src), "-o", str(out)])
    assert without_flag.exit_code == 0, without_flag.output


# ---------------------------------------------------------------------------
# convert: feedback reporting
# ---------------------------------------------------------------------------
NEXTFLOW_DIR = FIXTURES_DIR / "nextflow"


def test_convert_reports_removed_feedback_on_stderr(tmp_path):
    """The converter's warning reaches the user as a labelled block.

    A warning left to Python's own handler names the category and the
    internal source line that raised it, neither of which means anything to
    someone converting a pipeline.
    """
    runner = CliRunner()
    out = tmp_path / "out.mmd"
    result = runner.invoke(
        cli,
        ["convert", str(NEXTFLOW_DIR / "feedback_loop.mmd"), "-o", str(out)],
    )

    assert result.exit_code == 0
    assert "Warnings:" in result.output
    assert "- 1 feedback connection(s) removed" in result.output
    assert "Polish -> Assemble" in result.output
    assert "FeedbackEdgesDroppedWarning" not in result.output
    assert "cli.py" not in result.output


def test_convert_summary_line_counts_removed_feedback(tmp_path):
    runner = CliRunner()
    out = tmp_path / "out.mmd"
    result = runner.invoke(
        cli,
        ["convert", str(NEXTFLOW_DIR / "feedback_loop.mmd"), "-o", str(out)],
    )

    assert "1 feedback connection removed -> " in result.output


def test_convert_summary_line_is_unchanged_for_an_acyclic_pipeline(tmp_path):
    runner = CliRunner()
    out = tmp_path / "out.mmd"
    result = runner.invoke(
        cli,
        ["convert", str(NEXTFLOW_DIR / "flat_pipeline.mmd"), "-o", str(out)],
    )

    assert "feedback" not in result.output
    assert "Converted 5 processes, 1 sections -> " in result.output
