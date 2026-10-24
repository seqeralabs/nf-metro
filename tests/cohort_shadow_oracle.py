"""Tier C shadow oracle for the corridor-cohort plan.

The render path compiles the corridor cohorts inside one routing observation
and, unless a convergence grant moved a trunk, draws a different observation.
This oracle renders a map the way the plan is meant to be drawn instead: every
compiled plan is published at its compile site, the compiling observation is
the one drawn, and the drawn routes are then held to the published plan on
every owned segment point and corner, after the render invariants have run.

It is a test harness, not a render stage: the substitutions live only inside
:func:`shadow_observation`, so production renders never take this path.

``tests/data/cohort_shadow_ratchet.txt`` names the repository maps the oracle
fails today; ``test_cohort_shadow_ratchet.py`` holds every repository map to
that list in both directions.  Run as a script to sweep any other corpus::

    PYTHONPATH=src python tests/cohort_shadow_oracle.py --jobs jobs.jsonl \\
        --out shadow.jsonl --nproc 8

``--jobs`` takes either JSON lines carrying ``id``, ``path`` and optional
``opts`` (render layout options), or a text file of ``.mmd`` paths.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from functools import cache
from multiprocessing import Pool
from pathlib import Path
from typing import Any
from unittest import mock

from nf_metro.api import render_string
from nf_metro.layout.constants import COORD_TOLERANCE_FINE
from nf_metro.layout.route_plan import RoutePlan
from nf_metro.layout.routing import corridor_cohort_integration as cci
from nf_metro.layout.routing import member_geometry
from nf_metro.layout.routing.common import RoutedPath
from nf_metro.layout.routing.corridor_cohort_integration import (
    AcceptedCorridorOwnerKind,
)
from nf_metro.render import svg

ROOT = Path(__file__).resolve().parents[1]
RATCHET_PATH = ROOT / "tests" / "data" / "cohort_shadow_ratchet.txt"
_COMPILING_OBSERVATION = "cohort-shadow|compiling-observation"


class ShadowPlanDrawnMismatch(AssertionError):
    """The drawn routes differ from the plan the shadow render published."""


@cache
def repository_maps() -> tuple[str, ...]:
    """Every repository map the shadow ratchet covers, repository-relative.

    Nextflow DAG fixtures and deliberately invalid maps are left out: the
    ordinary render rejects both, so the oracle has no verdict on them.
    """
    return tuple(
        sorted(
            str(path.relative_to(ROOT))
            for root in ("examples", "tests/fixtures")
            for path in (ROOT / root).rglob("*.mmd")
            if not {"nextflow", "invalid"} & set(path.relative_to(ROOT).parts)
        )
    )


@cache
def read_ratchet() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in RATCHET_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    )


def _routes_by_edge(
    routes: Sequence[RoutedPath],
) -> dict[tuple[str, str, str], RoutedPath]:
    return {
        (route.edge.source, route.edge.target, route.line_id): route for route in routes
    }


def _differs(left: float, right: float) -> bool:
    return abs(left - right) > COORD_TOLERANCE_FINE


def planned_drawn_mismatches(
    routes: Sequence[RoutedPath], route_plan: RoutePlan
) -> list[str]:
    """Every owned coordinate the drawn *routes* place off *route_plan*.

    Covers each member geometry plan's owned segment points and the corner
    radii beside them, and each accepted cohort member segment's coordinate.
    """
    drawn = _routes_by_edge(routes)
    mismatches: list[str] = []
    for plan in route_plan.member_geometry_plans:
        route = drawn.get((plan.edge.source, plan.edge.target, plan.edge.line_id))
        if route is None:
            mismatches.append(f"member plan {plan.id} has no drawn route")
            continue
        mismatches.extend(member_geometry.owned_geometry_mismatches(route, plan))
    accepted = route_plan.accepted_corridor_cohort_plan
    if accepted is not None:
        for frame in accepted.owners:
            if frame.kind is not AcceptedCorridorOwnerKind.MEMBER_SEGMENT:
                continue
            _member_id, edge_key, rank = frame.segment
            route = drawn.get(edge_key)
            if route is None or rank + 1 >= len(route.points):
                mismatches.append(f"cohort segment {frame.segment} is not drawn")
                continue
            planned_coordinate = frame.accepted_coordinate
            if any(
                _differs(route.points[index][frame.axis], planned_coordinate)
                for index in (rank, rank + 1)
            ):
                mismatches.append(
                    f"cohort segment {frame.segment} planned {planned_coordinate} "
                    f"drawn {route.points[rank][frame.axis]}"
                )
    return mismatches


@contextmanager
def shadow_observation() -> Iterator[None]:
    """Render with every cohort plan published and its compiling observation drawn.

    The grant-only comparison against an ungranted replay fails by
    construction once the compiling observation is drawn, so it stands down;
    the planned-equals-drawn check over the settled geometry replaces it.
    """
    real_compile = member_geometry.compile_corridor_cohort_plan
    real_moved = svg._corridor_grant_moved_plan_ids
    real_drawable = svg._assert_settled_geometry_is_drawable

    def compile_and_publish(*args: Any, **kwargs: Any) -> cci.CorridorCohortPlan:
        plan = real_compile(*args, **kwargs)
        cci.publish_corridor_cohort_plan(plan)
        return plan

    def draw_compiling_observation(plan: RoutePlan) -> frozenset[str]:
        moved = real_moved(plan)
        if moved:
            return moved
        if (
            plan.accepted_corridor_cohort_plan is not None
            and not plan.boundary_clearance_requirements
        ):
            return frozenset({_COMPILING_OBSERVATION})
        return moved

    def drawable_as_planned(graph: Any, settled: Any) -> None:
        real_drawable(graph, settled)
        mismatches = planned_drawn_mismatches(settled.routes, settled.route_plan)
        if mismatches:
            raise ShadowPlanDrawnMismatch(
                f"{len(mismatches)} owned coordinate(s) drawn off the plan: "
                + "; ".join(mismatches[:3])
            )

    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(
                member_geometry, "compile_corridor_cohort_plan", compile_and_publish
            )
        )
        stack.enter_context(
            mock.patch.object(
                svg, "_corridor_grant_moved_plan_ids", draw_compiling_observation
            )
        )
        stack.enter_context(
            mock.patch.object(
                svg,
                "_assert_grant_moves_only_its_convergences",
                lambda *_args, **_kwargs: None,
            )
        )
        stack.enter_context(
            mock.patch.object(
                svg, "_assert_settled_geometry_is_drawable", drawable_as_planned
            )
        )
        yield


def _render(path: Path, layout_options: dict[str, Any] | None) -> None:
    render_string(
        path.read_text(), layout_options=layout_options, source_dir=str(path.parent)
    )


def shadow_failure(
    path: Path, layout_options: dict[str, Any] | None = None
) -> str | None:
    """Why the shadow render of *path* fails where the ordinary render succeeds.

    ``None`` when the shadow render passes, or when the ordinary render fails
    too: a map the render path already rejects says nothing about the plan.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with shadow_observation():
                _render(path, layout_options)
        except Exception as error:  # noqa: BLE001 - every abort is a verdict
            failure = f"{type(error).__name__}: {error}"
        else:
            return None
        try:
            _render(path, layout_options)
        except Exception:  # noqa: BLE001 - the ordinary render rejects it too
            return None
    return failure


def _alarm(*_args: Any) -> None:
    raise TimeoutError("shadow render exceeded 240s")


def _init_worker(root: str) -> None:
    os.chdir(root)
    signal.signal(signal.SIGALRM, _alarm)


def _run_job(job: dict[str, Any]) -> dict[str, Any]:
    path = Path(job["path"])
    if not path.is_absolute():
        path = Path.cwd() / path
    record: dict[str, Any] = {"id": job["id"]}
    signal.alarm(240)
    try:
        failure = shadow_failure(path, job.get("opts") or None)
    except BaseException as error:  # noqa: BLE001 - a timeout is a verdict
        failure = f"{type(error).__name__}: {error}"
    finally:
        signal.alarm(0)
    record["status"] = "ok" if failure is None else "shadow-fail"
    if failure is not None:
        record["etype"], _, record["err"] = failure.partition(": ")
    return record


def _read_jobs(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text().splitlines() if line]
    return [
        {"id": line.strip(), "path": line.strip()}
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=Path, help="jobs .jsonl or a list of maps")
    parser.add_argument("--out", type=Path, required=True, help="output .jsonl")
    parser.add_argument("--nproc", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="directory relative job paths resolve against",
    )
    args = parser.parse_args(argv)
    jobs = (
        _read_jobs(args.jobs)
        if args.jobs is not None
        else [{"id": path, "path": path} for path in repository_maps()]
    )
    failures = 0
    with (
        Pool(
            args.nproc,
            initializer=_init_worker,
            initargs=(str(args.root.resolve()),),
            maxtasksperchild=100,
        ) as pool,
        args.out.open("w") as out,
    ):
        for record in pool.imap_unordered(_run_job, jobs, chunksize=2):
            failures += record["status"] != "ok"
            out.write(json.dumps(record) + "\n")
            out.flush()
    print(f"{len(jobs)} job(s), {failures} shadow failure(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
