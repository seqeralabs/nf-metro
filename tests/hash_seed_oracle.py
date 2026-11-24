"""Cross-process oracle for settled render determinism."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypedDict

from nf_metro.api import prepare_graph, resolve_theme
from nf_metro.parser.model import MetroGraph
from nf_metro.render.plan import FrozenMap, FrozenRecord, freeze_render_value
from nf_metro.render.svg import build_render_plan, emit_render_plan
from nf_metro.render.validate import validate_render

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HASH_SEEDS = ("0", "1", "2", "5", "43", "random")
_SETTLED_GRAPH_CACHE_FIELDS = {
    "_station_lines_cache",
    "_edges_from_cache",
    "_edges_to_cache",
    "_junction_ids_cache",
}


ExceptionObservation = TypedDict(
    "ExceptionObservation",
    {
        "phase": str,
        "class": str,
        "qualified_class": str,
        "message": str,
    },
)


class Observation(TypedDict):
    outcome: Literal["success", "exception"]
    exception: ExceptionObservation | None
    settled_graph_sha256: str | None
    render_plan_sha256: str | None
    svg_sha256: str | None
    svg_length: int | None
    findings: Any
    findings_sha256: str | None


def _stable_value(value: Any) -> Any:
    """Return JSON-safe data without discarding semantically ordered fields."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, Enum):
        return {"enum": f"{type(value).__name__}.{value.name}"}
    if isinstance(value, FrozenMap):
        return {
            "map": [
                [_stable_value(key), _stable_value(item)] for key, item in value.entries
            ]
        }
    if isinstance(value, FrozenRecord):
        return {"record": value.kind, "values": _stable_value(value.values)}
    if is_dataclass(value):
        return {
            "record": type(value).__name__,
            "values": [
                [field.name, _stable_value(getattr(value, field.name))]
                for field in fields(value)
            ],
        }
    if isinstance(value, Mapping):
        return {
            "map": [
                [_stable_value(key), _stable_value(item)] for key, item in value.items()
            ]
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        members = [_stable_value(item) for item in value]
        return {
            "set": sorted(
                members,
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":")
                ),
            )
        }
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _stable_value(value), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _freeze_settled_graph(graph: MetroGraph) -> FrozenRecord:
    """Freeze semantic graph state while excluding lazy lookup caches."""
    return FrozenRecord(
        type(graph).__name__,
        FrozenMap(
            tuple(
                (field.name, freeze_render_value(getattr(graph, field.name)))
                for field in fields(graph)
                if field.name not in _SETTLED_GRAPH_CACHE_FIELDS
            )
        ),
    )


def _exception(exc: Exception, phase: str) -> ExceptionObservation:
    return {
        "phase": phase,
        "class": type(exc).__name__,
        "qualified_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
    }


def _observation(
    *,
    exception: ExceptionObservation | None,
    graph_digest: str | None,
    plan_digest: str | None,
    svg: str | None,
    findings: Any = None,
) -> Observation:
    svg_bytes = svg.encode() if svg is not None else None
    return {
        "outcome": "success" if exception is None else "exception",
        "exception": exception,
        "settled_graph_sha256": graph_digest,
        "render_plan_sha256": plan_digest,
        "svg_sha256": (
            hashlib.sha256(svg_bytes).hexdigest() if svg_bytes is not None else None
        ),
        "svg_length": len(svg_bytes) if svg_bytes is not None else None,
        "findings": _stable_value(findings) if findings is not None else None,
        "findings_sha256": _digest(findings) if findings is not None else None,
    }


def observe_path(path: Path) -> Observation:
    """Observe the complete settled/rendered state for one source file."""
    text = path.read_text()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            graph = prepare_graph(text, source_dir=str(path.parent))
        except Exception as exc:  # noqa: BLE001 - the outcome is oracle data
            return _observation(
                exception=_exception(exc, "prepare"),
                graph_digest=None,
                plan_digest=None,
                svg=None,
            )

        graph_digest = _digest(_freeze_settled_graph(graph))
        try:
            plan = build_render_plan(graph, resolve_theme(graph))
        except Exception as exc:  # noqa: BLE001 - the outcome is oracle data
            return _observation(
                exception=_exception(exc, "plan"),
                graph_digest=graph_digest,
                plan_digest=None,
                svg=None,
            )

        plan_digest = _digest(plan)
        try:
            svg = emit_render_plan(plan)
        except Exception as exc:  # noqa: BLE001 - the outcome is oracle data
            return _observation(
                exception=_exception(exc, "emit"),
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                svg=None,
            )

        try:
            findings = validate_render(svg, plan=plan)
        except Exception as exc:  # noqa: BLE001 - the outcome is oracle data
            return _observation(
                exception=_exception(exc, "validate"),
                graph_digest=graph_digest,
                plan_digest=plan_digest,
                svg=svg,
            )
        return _observation(
            exception=None,
            graph_digest=graph_digest,
            plan_digest=plan_digest,
            svg=svg,
            findings=findings,
        )


def _path_key(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def observe_paths(paths: Sequence[Path]) -> dict[str, Observation]:
    """Observe several sources in one interpreter, keyed by stable path."""
    return {_path_key(path): observe_path(path) for path in paths}


def observe_paths_across_hash_seeds(
    paths: Sequence[Path],
    hash_seeds: tuple[str, ...] = DEFAULT_HASH_SEEDS,
) -> dict[str, dict[str, Observation]]:
    """Observe all paths in one fresh interpreter per requested hash seed."""
    resolved_paths = tuple(path.resolve() for path in paths)
    python_path = os.pathsep.join((str(ROOT / "src"), str(ROOT / "tests")))
    observations: dict[str, dict[str, Observation]] = {}
    for hash_seed in hash_seeds:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = hash_seed
        env["PYTHONPATH"] = python_path
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                *(str(path) for path in resolved_paths),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        observations[hash_seed] = json.loads(result.stdout)
    return observations


def observe_across_hash_seeds(
    path: Path,
    hash_seeds: tuple[str, ...] = DEFAULT_HASH_SEEDS,
) -> dict[str, Observation]:
    """Observe one path in a fresh interpreter for every requested hash seed."""
    key = _path_key(path)
    batches = observe_paths_across_hash_seeds((path,), hash_seeds)
    return {seed: observations[key] for seed, observations in batches.items()}


def observation_differences(
    batches: dict[str, dict[str, Observation]],
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Return only fields that differ from the hash-seed-zero observation."""
    baseline = batches["0"]
    differences: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for seed, observations in batches.items():
        if seed == "0":
            continue
        for path in sorted(set(baseline) | set(observations)):
            expected = baseline.get(path, {})
            observed = observations.get(path, {})
            fields = {
                field: {
                    "baseline": expected.get(field),
                    "observed": observed.get(field),
                }
                for field in sorted(set(expected) | set(observed))
                if expected.get(field) != observed.get(field)
            }
            if fields:
                differences.setdefault(seed, {})[path] = fields
    return differences


def _assert_batches_identical(batches: dict[str, dict[str, Observation]]) -> None:
    differences = observation_differences(batches)
    if differences:
        raise SystemExit(
            "hash-seed differences: "
            + json.dumps(differences, sort_keys=True, separators=(",", ":"))
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--check-example-corpus",
        action="store_true",
        help="compare every committed example under the standard hash seeds",
    )
    args = parser.parse_args()
    if args.check_example_corpus:
        corpus_paths = sorted((ROOT / "examples").rglob("*.mmd"))
        corpus_batches = observe_paths_across_hash_seeds(corpus_paths)
        _assert_batches_identical(corpus_batches)
        print(f"{len(corpus_paths)} example sources are hash-seed deterministic")
    else:
        if not args.paths:
            parser.error("provide at least one path")
        observations = observe_paths(args.paths)
        print(json.dumps(observations, sort_keys=True, separators=(",", ":")))
