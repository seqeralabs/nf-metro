#!/usr/bin/env python3
"""Maintain the docs version manifest (gh-pages/versions.json).

Each deploy upserts its target into the manifest the version switcher reads.
The file format matches the one mike produced, so the historical pre-Astro
entries it already contains are preserved untouched:

    [{"version": "dev", "title": "dev", "aliases": []},
     {"version": "0.7.2", "title": "0.7.2", "aliases": ["latest"]}, ...]

Usage:
    python scripts/update_versions_manifest.py \
        --manifest gh-pages/versions.json --version dev
    python scripts/update_versions_manifest.py \
        --manifest gh-pages/versions.json --version X.Y.Z --latest
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def version_sort_key(entry: dict) -> tuple:
    """Sort `dev` first, then numeric versions newest-first."""
    version = entry["version"]
    if version == "dev":
        return (0,)
    parts = tuple(int(p) for p in version.split(".") if p.isdigit())
    # Negate so larger versions sort earlier; `1` keeps them after `dev`.
    return (1, tuple(-p for p in parts))


def parse_version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse a dotted-numeric version (`X.Y.Z`) into a comparable tuple.

    Returns None for anything that is not a clean run of dot-separated
    integers (e.g. `dev`, `1.2.0rc1`), signalling callers to skip the ordered
    comparison for that value. Deliberately stricter than `version_sort_key`,
    which must always produce an orderable tuple and so drops non-numeric
    segments instead of refusing them; this parser feeds the `latest`-alias
    guard, which must never guess at an ambiguous comparison.
    """
    parts = version.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _may_advance_latest(by_version: dict[str, dict], version: str) -> bool:
    """Whether `latest` may move to `version`.

    The alias is monotonic: concurrent release deploys can finish out of
    order, so an older version must never demote a newer one already holding
    `latest`. Refuse the move only when both the incoming version and the
    current holder parse as clean dotted-numeric tuples and the incoming one
    is strictly older; for versions the parser doesn't model, fall back to the
    unconditional move.
    """
    current = next(
        (e for e in by_version.values() if "latest" in e.get("aliases", [])),
        None,
    )
    if current is None:
        return True
    incoming_tuple = parse_version_tuple(version)
    current_tuple = parse_version_tuple(current["version"])
    if incoming_tuple is None or current_tuple is None:
        return True
    return incoming_tuple >= current_tuple


def update_manifest(manifest: Path, version: str, latest: bool) -> None:
    """Upsert ``version`` into ``manifest``, optionally advancing ``latest``."""
    entries: list[dict] = []
    if manifest.exists():
        entries = json.loads(manifest.read_text())

    by_version = {e["version"]: e for e in entries}
    entry = by_version.setdefault(
        version, {"version": version, "title": version, "aliases": []}
    )
    entry.setdefault("aliases", [])

    if latest and _may_advance_latest(by_version, version):
        for e in by_version.values():
            e["aliases"] = [a for a in e.get("aliases", []) if a != "latest"]
        if "latest" not in entry["aliases"]:
            entry["aliases"].append("latest")

    ordered = sorted(by_version.values(), key=version_sort_key)
    manifest.write_text(json.dumps(ordered, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Mark this version as the `latest` alias (releases only).",
    )
    args = parser.parse_args()
    update_manifest(args.manifest, args.version, args.latest)


if __name__ == "__main__":
    main()
