"""Tests for the docs version manifest updater (``scripts/update_versions_manifest``).

The ``--latest`` alias is monotonic: a release deploy may only advance the
``latest`` pointer, never move it back to an older version. Two release
deploys can run concurrently once each release tag gets its own workflow
concurrency group, so an out-of-order finish (an older version's deploy job
writing the manifest after a newer one's) must not silently demote ``latest``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from update_versions_manifest import update_manifest  # noqa: E402


def _latest_holder(entries: list[dict]) -> str | None:
    return next(
        (e["version"] for e in entries if "latest" in e.get("aliases", [])),
        None,
    )


def _write(manifest: Path, entries: list[dict]) -> None:
    manifest.write_text(json.dumps(entries) + "\n")


def _read(manifest: Path) -> list[dict]:
    return json.loads(manifest.read_text())


def test_older_release_does_not_steal_latest(tmp_path: Path) -> None:
    manifest = tmp_path / "versions.json"
    _write(
        manifest,
        [
            {"version": "dev", "title": "dev", "aliases": []},
            {"version": "1.2.0", "title": "1.2.0", "aliases": ["latest"]},
        ],
    )

    update_manifest(manifest, "1.1.0", latest=True)

    entries = _read(manifest)
    assert _latest_holder(entries) == "1.2.0"
    versions = {e["version"] for e in entries}
    assert "1.1.0" in versions


def test_newer_release_advances_latest(tmp_path: Path) -> None:
    manifest = tmp_path / "versions.json"
    _write(
        manifest,
        [
            {"version": "dev", "title": "dev", "aliases": []},
            {"version": "1.1.0", "title": "1.1.0", "aliases": ["latest"]},
        ],
    )

    update_manifest(manifest, "1.2.0", latest=True)

    entries = _read(manifest)
    assert _latest_holder(entries) == "1.2.0"
    by_version = {e["version"]: e for e in entries}
    assert "latest" not in by_version["1.1.0"]["aliases"]


def test_redeploying_current_latest_keeps_the_alias(tmp_path: Path) -> None:
    manifest = tmp_path / "versions.json"
    _write(
        manifest,
        [{"version": "1.2.0", "title": "1.2.0", "aliases": ["latest"]}],
    )

    update_manifest(manifest, "1.2.0", latest=True)

    assert _latest_holder(_read(manifest)) == "1.2.0"
