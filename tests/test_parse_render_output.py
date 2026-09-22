"""Tests for scripts/parse_render_output.py, the GitHub Action's output-path parser.

Exercised as a subprocess (not imported), run the same way action.yml runs
it, against nf-metro's real stdout contract (see ``_print_render_result``
in ``nf_metro.cli``).
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "parse_render_output.py"


def _run(stdin_text: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


def test_parses_multiple_output_paths():
    """Each `  - "<path>"` line becomes one output line, in order."""
    result = _run(
        "version: v2.1.0\n"
        "outputs:\n"
        '  - "assets/metro_map.svg"\n'
        '  - "assets/metro_map.png"\n'
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "assets/metro_map.svg",
        "assets/metro_map.png",
    ]


def test_parses_single_output_path():
    """A lone output path parses the same way as a list of several."""
    result = _run('version: v2.1.0\noutputs:\n  - "assets/metro_map.svg"\n')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["assets/metro_map.svg"]


def test_path_containing_spaces_is_preserved_whole():
    """A path with embedded spaces isn't split; it's one line in, one line out."""
    result = _run('outputs:\n  - "assets/my map.svg"\n')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["assets/my map.svg"]


def test_decodes_unicode_escape_in_path():
    """A non-ASCII path character, YAML-escaped, decodes back."""
    result = _run('outputs:\n  - "assets/m\\u00e9tro_map.svg"\n')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["assets/métro_map.svg"]


def test_decodes_embedded_quote_and_backslash_in_path():
    """A literal quote or backslash in a path, YAML-escaped, decodes back."""
    result = _run('outputs:\n  - "assets/weird\\"na\\\\me.svg"\n')
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['assets/weird"na\\me.svg']


def test_errors_when_no_output_paths_found():
    """No `  - "<path>"` lines at all is a hard failure, not an empty result.

    An empty match here must not look like success: the caller's later
    ``git status --porcelain -- "${rendered[@]}"`` would run with no
    pathspec at all, diffing the whole repository instead of nothing.
    """
    result = _run("version: v2.1.0\n")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no output paths" in result.stderr


def test_errors_on_completely_unexpected_input():
    """Garbage input (not nf-metro's contract at all) also fails loudly."""
    result = _run("not yaml at all\njust some text\n")
    assert result.returncode == 1
    assert result.stdout == ""


def test_errors_on_malformed_yaml_rather_than_crashing():
    """Invalid YAML syntax is a clean failure, not an unhandled traceback."""
    result = _run(":\n  bad: [unterminated\n")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no output paths" in result.stderr
    assert "Traceback" not in result.stderr


def test_ignores_the_version_banner_line():
    """The `version: v<version>` line is not itself an output path."""
    result = _run('version: v2.1.0\noutputs:\n  - "out.svg"\n')
    assert result.returncode == 0
    assert "nf-metro" not in result.stdout
    assert result.stdout.splitlines() == ["out.svg"]


def test_input_paths_are_not_reported_as_outputs():
    """The `inputs:` list is sources, not results, and must not be echoed."""
    result = _run(
        "version: v2.1.0\n"
        "inputs:\n"
        "  - assets/metro_map.mmd\n"
        "outputs:\n"
        "  - assets/metro_map.svg\n"
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["assets/metro_map.svg"]


def test_parses_unquoted_output_paths():
    """A plain scalar, which the emitter writes where quoting is unnecessary."""
    result = _run("outputs:\n  - assets/metro_map.svg\n  - assets/metro_map.png\n")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "assets/metro_map.svg",
        "assets/metro_map.png",
    ]


def test_parses_a_real_render_result(tmp_path):
    """The parser reads what `render` actually writes, not a hand-made sample."""
    import sys as _sys

    src = tmp_path / "map.mmd"
    src.write_text(
        "%%metro line: main | Main | #ff0000\n"
        "graph LR\n"
        "    a[A]\n"
        "    b[B]\n"
        "    a -->|main| b\n"
    )
    out = tmp_path / "map.svg"
    rendered = subprocess.run(
        [_sys.executable, "-m", "nf_metro", "render", str(src), "-o", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    result = _run(rendered.stdout)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(out)]
