"""Tests for the CLI entry points."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
RNASEQ_MMD = EXAMPLES_DIR / "rnaseq_sections.mmd"
INVALID_DIR = Path(__file__).resolve().parent / "fixtures" / "invalid"

# Fixtures that parse and pass graph-semantic validation but trip a layout
# invariant once the engine runs.  They exercise the boundary between the bare
# `validate` (semantic only) and `validate --with-layout`.
SEMANTIC_VALID_LAYOUT_BROKEN = [
    "mixed_entry_opposing.mmd",
    "mixed_entry_perpendicular.mmd",
    "backward_feed_rl.mmd",
    "merge_trunk_rightward_source.mmd",
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
    mmd.write_text(RNASEQ_MMD.read_text())
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
    """validate command reports parse errors."""
    bad = tmp_path / "bad.mmd"
    bad.write_text("not a valid mermaid file")
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", str(bad)])
    # Should still succeed (no crash), but output says 0 stations
    assert result.exit_code == 0


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
    monkeypatch.setattr("nf_metro.cli.render_graph", _boom)
    src = tmp_path / "a.mmd"
    src.write_text(RNASEQ_MMD.read_text())
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
    monkeypatch.setattr("nf_metro.cli.render_graph", _boom)
    src = tmp_path / "a.mmd"
    src.write_text(RNASEQ_MMD.read_text())
    runner = CliRunner()
    result = runner.invoke(cli, ["render", str(src), "-o", str(tmp_path / "out.svg")])
    assert isinstance(result.exception, KeyError)


def test_render_multiple_files(tmp_path):
    """render accepts more than one INPUT_FILE, each to its own sibling output."""
    a = tmp_path / "a.mmd"
    b = tmp_path / "b.mmd"
    a.write_text(RNASEQ_MMD.read_text())
    b.write_text(RNASEQ_MMD.read_text())
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
    a.write_text(RNASEQ_MMD.read_text())
    b.write_text(RNASEQ_MMD.read_text())
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
        "graph LR\n"
        "    a[Foo] -->|x| b[Bar]\n"
    )
    good.write_text(RNASEQ_MMD.read_text())
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

    mmd_text = "%%metro center_ports: true\n" + RNASEQ_MMD.read_text()
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
