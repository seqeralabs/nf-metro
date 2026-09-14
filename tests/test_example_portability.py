"""The shipped examples must render from any working directory.

Every other test and CI invocation renders from the repository root, where a
path written relative to the root resolves by accident. These tests render from
a directory that is not the root, so an example that only works from the root
reds here instead of reaching a reader.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from nf_metro.cli import cli
from nf_metro.parser import parse_metro_mermaid_file

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"

EXAMPLE_FILES = (
    sorted(EXAMPLES_DIR.glob("*.mmd"))
    + sorted((EXAMPLES_DIR / "showcase").glob("*.mmd"))
    + sorted((EXAMPLES_DIR / "guide").glob("*.mmd"))
)
LOGO_EXAMPLES = [p for p in EXAMPLE_FILES if "%%metro logo:" in p.read_text()]


def _render(source: Path, out: Path, cwd: Path, monkeypatch) -> str:
    """Render *source* by absolute path with the process cwd at *cwd*."""
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli, ["render", str(source), "-o", str(out)])
    assert result.exit_code == 0, (
        f"{source.name} failed to render from {cwd}:\n{result.output}"
    )
    return out.read_text()


def test_parse_metro_mermaid_file_source_dir_survives_a_later_chdir(
    tmp_path, monkeypatch
):
    """``source_dir`` must stay anchored to where the map was loaded from.

    A caller may parse a map given by a relative path and only change the
    process cwd afterwards (e.g. before rendering). A relative *path* passed
    to the loader must not leave ``source_dir`` relative too, or that later
    chdir would silently change what it resolves against.
    """
    source = EXAMPLES_DIR / "rnaseq_auto.mmd"
    monkeypatch.chdir(source.parent)
    graph = parse_metro_mermaid_file(Path(source.name))
    monkeypatch.chdir(tmp_path)
    assert graph.source_dir == str(source.parent)


def test_example_corpus_is_not_empty():
    """A corpus that globs to nothing would make every test below vacuous."""
    assert EXAMPLE_FILES
    assert LOGO_EXAMPLES


# Its consumer hands `prepare_graph` the repository root as the source
# directory, so this map's logo path is root-relative by design.
_ROOT_RELATIVE_BY_DESIGN = {"tests/fixtures/candidate_executor/control.mmd"}


def test_every_shipped_logo_path_resolves_from_its_own_map():
    """A logo path resolves against the directory its `.mmd` sits in.

    One written from the repository root instead resolves only while the
    process working directory happens to be that root. Reading the directives
    covers every map in the repository, including the topology and fixture
    corpora that the render-based tests above are too slow to sweep.
    """
    offenders = []
    corpus = sorted(EXAMPLES_DIR.rglob("*.mmd")) + sorted(
        (REPO_ROOT / "tests").rglob("*.mmd")
    )
    for mmd in corpus:
        if mmd.relative_to(REPO_ROOT).as_posix() in _ROOT_RELATIVE_BY_DESIGN:
            continue
        for line in mmd.read_text().splitlines():
            if not line.startswith("%%metro logo:"):
                continue
            _, _, value = line.partition(":")
            for raw in (part.strip() for part in value.split("|")):
                if not raw or raw.startswith("data:"):
                    continue
                if Path(raw).is_absolute() or not (mmd.parent / raw).is_file():
                    offenders.append(f"{mmd.relative_to(REPO_ROOT)}: {raw!r}")
    assert not offenders, "logo paths unresolvable from their own map:\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize("source", EXAMPLE_FILES, ids=lambda p: p.stem)
def test_example_renders_from_a_foreign_cwd(source, tmp_path, monkeypatch):
    svg = _render(source, tmp_path / "out.svg", tmp_path, monkeypatch)
    assert svg.lstrip().startswith(("<?xml", "<svg"))


@pytest.mark.parametrize("source", LOGO_EXAMPLES, ids=lambda p: p.stem)
def test_logo_example_embeds_its_logo_from_a_foreign_cwd(source, tmp_path, monkeypatch):
    """An unresolvable logo path is an error, so a render proves resolution.

    The embedded asset is asserted as well, so downgrading that error to a
    warning cannot quietly ship these maps with the logo missing.
    """
    svg = _render(source, tmp_path / "out.svg", tmp_path, monkeypatch)
    assert "data:image/png;base64," in svg


@pytest.mark.parametrize("source", LOGO_EXAMPLES, ids=lambda p: p.stem)
def test_logo_example_renders_the_same_bytes_from_any_cwd(
    source, tmp_path, monkeypatch
):
    """Nothing about where the render runs may reach the rendered bytes."""
    from_root = _render(source, tmp_path / "root.svg", REPO_ROOT, monkeypatch)
    from_elsewhere = _render(source, tmp_path / "elsewhere.svg", tmp_path, monkeypatch)
    assert from_root == from_elsewhere
