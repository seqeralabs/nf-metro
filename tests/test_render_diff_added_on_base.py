"""A map a PR adds is shown against what the base code makes of it.

The base build renders the base ``gallery.yaml``, so a map the PR registers has
no base render and would show as "New in PR" only. ``render_added_on_base.py``
renders those maps with the base code; ``build_render_diff.py --base-added``
turns the result into a before/after pair, or an abort panel.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
from build_render_diff import build_diff  # noqa: E402

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    '<path d="M0,0 L{x},5" /></svg>'
)


def _write(directory: Path, name: str, x: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(_SVG.format(x=x))


def _page(tmp_path: Path, base_added: Path) -> str:
    base, pr = tmp_path / "base", tmp_path / "pr"
    base.mkdir()
    _write(pr, "changed_in_pr.svg", 1)
    _write(pr, "differs.svg", 2)
    _write(pr, "same.svg", 3)
    _write(pr, "aborts.svg", 4)
    _write(pr, "unrendered.svg", 5)
    _write(base_added, "differs.svg", 20)
    _write(base_added, "same.svg", 3)
    (base_added / "aborts.json").write_text(
        json.dumps({"aborts.svg": "GuardError: [bundle order] <bad>"})
    )
    assert build_diff(base, pr, tmp_path / "out", base_added_dir=base_added)
    return (tmp_path / "out" / "index.html").read_text()


def _entry(page: str, stem: str) -> str:
    marker = f'<div class="diff-entry" id="{stem}">'
    start = page.index(marker) + len(marker)
    end = page.find('<div class="diff-entry"', start)
    return page[start:] if end < 0 else page[start:end]


def test_new_map_with_a_different_base_render_is_a_before_after_pair(tmp_path):
    entry = _entry(_page(tmp_path, tmp_path / "added"), "differs")

    assert "Base code (map new in PR)" in entry
    assert "L20,5" in entry and "L2,5" in entry


def test_new_map_whose_base_code_aborts_shows_the_abort_instead_of_a_render(tmp_path):
    entry = _entry(_page(tmp_path, tmp_path / "added"), "aborts")

    assert "Aborts on base: GuardError: [bundle order] &lt;bad&gt;" in entry
    assert "L4,5" in entry


def test_new_map_identical_on_the_base_code_is_marked_as_such(tmp_path):
    entry = _entry(_page(tmp_path, tmp_path / "added"), "same")

    assert "New in PR (identical on base code)" in entry
    assert "Base code" not in entry


def test_new_map_without_a_base_render_stays_new_only(tmp_path):
    entry = _entry(_page(tmp_path, tmp_path / "added"), "unrendered")

    assert "<h4>New in PR</h4>" in entry


def test_missing_base_added_directory_leaves_new_maps_new_only(tmp_path):
    page = _page(tmp_path, tmp_path / "added")
    assert "Base code" in page

    base, pr = tmp_path / "b2", tmp_path / "p2"
    base.mkdir()
    _write(pr, "new.svg", 1)
    assert build_diff(base, pr, tmp_path / "o2", base_added_dir=tmp_path / "absent")
    assert "<h4>New in PR</h4>" in (tmp_path / "o2" / "index.html").read_text()


def test_driver_renders_what_the_pr_adds_and_records_what_aborts(tmp_path):
    good = ROOT / "examples" / "guide" / "01_minimal.mmd"
    bad = tmp_path / "bad.mmd"
    bad.write_text("graph LR\n    a[A] --> b[B]\n")
    base, pr, out = tmp_path / "base", tmp_path / "pr", tmp_path / "out"
    _write(base, "existing.svg", 1)
    _write(pr, "existing.svg", 1)
    _write(pr, "good.svg", 1)
    _write(pr, "bad.svg", 1)
    params = {"debug": False, "self_color_scheme": False, "from_nextflow": False}
    (pr / "sources.json").write_text(
        json.dumps(
            {
                "existing.svg": {"src": str(good), **params},
                "good.svg": {"src": str(good), **params},
                "bad.svg": {"src": str(bad), **params},
            }
        )
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "render_added_on_base.py"),
            str(base),
            str(pr),
            str(out),
            "--base-src",
            str(ROOT / "src"),
        ],
        check=True,
        capture_output=True,
    )

    assert sorted(p.name for p in out.glob("*.svg")) == ["good.svg"]
    aborts = json.loads((out / "aborts.json").read_text())
    assert list(aborts) == ["bad.svg"]
    assert aborts["bad.svg"].startswith("ValueError: Some edges have no metro line")
