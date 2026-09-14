"""Repository invariants for GitHub Actions resource usage."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
JOB_HEADER = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):\n", re.MULTILINE)


def _jobs(path: Path) -> dict[str, str]:
    text = path.read_text()
    _, jobs = text.split("\njobs:\n", maxsplit=1)
    matches = list(JOB_HEADER.finditer(jobs))
    return {
        match.group("name"): jobs[
            match.start() : matches[index + 1].start()
            if index + 1 < len(matches)
            else None
        ]
        for index, match in enumerate(matches)
    }


@pytest.mark.parametrize(
    "workflow",
    sorted(WORKFLOWS.glob("*.yml")),
    ids=lambda path: path.name,
)
def test_every_workflow_job_has_a_finite_timeout(workflow: Path):
    for job_name, job in _jobs(workflow).items():
        match = re.search(r"^    timeout-minutes: (?P<minutes>\d+)$", job, re.MULTILINE)
        if match:
            assert int(match.group("minutes")) > 0
            continue
        assert re.search(
            r"^    uses: \./\.github/workflows/[A-Za-z0-9_-]+\.yml$",
            job,
            re.MULTILINE,
        ), f"{workflow.name}:{job_name} has no timeout-minutes"


@pytest.mark.parametrize("job_name", ["test", "routing-gates"])
def test_expensive_ci_jobs_wait_for_fast_checks(job_name: str):
    job = _jobs(WORKFLOWS / "ci.yml")[job_name]
    assert re.search(r"^    needs: \[lint, format\]$", job, re.MULTILINE)


def test_test_matrix_uses_committed_timings_and_reports_slow_tests():
    job = _jobs(WORKFLOWS / "ci.yml")["test"]
    assert (ROOT / ".test_durations").is_file()
    assert "fail-fast: true" in job
    assert "--splitting-algorithm least_duration" in job
    assert "--durations=25" in job


GATE_MODULE = "tests/test_routing_gate_coverage.py"
GATE_REQUIRE_ENV = 'NF_METRO_REQUIRE_GATE_COVERAGE: "1"'
STEP_HEADER = re.compile(r"^      - ", re.MULTILINE)


def _steps(job: str) -> list[str]:
    """The job's steps, each spanning its own ``- `` marker to the next.

    Assertions about a step's ``env:`` or its arguments have to be made against
    the step itself: matched against the whole job they also hold when the key
    sits on a neighbouring step, where it has no effect on the command.
    """
    starts = [match.start() for match in STEP_HEADER.finditer(job)]
    return [job[a:b] for a, b in zip(starts, [*starts[1:], len(job)])]


def test_routing_gate_job_pins_the_baseline_interpreter_and_demands_a_run():
    """The gate ratchet's job must be unable to pass without running it.

    ``tests/test_routing_gate_coverage.py`` skips off the baseline interpreter
    and is ``--ignore``d in the matrix, so a pin that drifted from
    ``BASELINE_PYTHON`` would leave the two-sided ratchet reporting success
    from a run of nothing.  ``NF_METRO_REQUIRE_GATE_COVERAGE`` turns that skip
    into a failure, and only for the step that invokes the ratchet, so the
    variable is required on that step rather than anywhere in the job.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import routing_gate_coverage

    major, minor = routing_gate_coverage.BASELINE_PYTHON
    jobs = _jobs(WORKFLOWS / "ci.yml")
    job = jobs["routing-gates"]

    setup = [step for step in _steps(job) if "uses: actions/setup-python" in step]
    assert len(setup) == 1, f"expected one setup-python step, got {len(setup)}"
    assert f'python-version: "{major}.{minor}"' in setup[0], (
        f"routing-gates must pin CPython {major}.{minor} to match "
        f"BASELINE_PYTHON, or the module it runs skips"
    )

    invocations = [
        step
        for step in _steps(job)
        if re.search(r"^      - run: .*\bpytest\b", step, re.MULTILINE)
    ]
    assert len(invocations) == 1, (
        f"expected exactly one pytest step in routing-gates, got {len(invocations)}"
    )
    step = invocations[0]
    assert GATE_MODULE in step, (
        f"the routing-gates pytest step must invoke {GATE_MODULE}; it runs: {step!r}"
    )
    assert GATE_REQUIRE_ENV in step, (
        f"{GATE_REQUIRE_ENV} must be set on the step that invokes {GATE_MODULE}, "
        f"where it can turn that module's interpreter skip into a failure; "
        f"the step is: {step!r}"
    )

    assert f"--ignore={GATE_MODULE}" in jobs["test"], (
        f"the matrix must keep ignoring {GATE_MODULE}, which is the premise "
        f"that routing-gates is its only run"
    )


def test_render_diff_runs_independently_and_uses_content_cache_key():
    assert "render-diff" not in _jobs(WORKFLOWS / "ci.yml")

    workflow = (WORKFLOWS / "pr-renders.yml").read_text()
    assert re.search(r"^  pull_request:$", workflow, re.MULTILINE)
    assert "branches: [main]" in workflow
    render_job = _jobs(WORKFLOWS / "pr-renders.yml")["render-diff"]
    assert "base_render_key=" in render_job
    assert "tests/layout_metrics.py" in render_job
    assert (
        "key: render-base-${{ steps.strategy.outputs.base_render_key }}" in render_job
    )


def test_render_publisher_follows_render_workflow_artifact():
    workflow = (WORKFLOWS / "pr-render-publish.yml").read_text()
    assert 'workflows: ["PR render preview"]' in workflow
    assert "actions/runs/${RUN_ID}/artifacts?name=render-preview" in workflow
    assert "needs.resolve.outputs.artifact == 'true'" in workflow


def test_docs_deploy_isolates_release_concurrency_per_tag():
    """A queued release deploy must not be cancellable by a later push.

    A concurrency group keeps only one pending run, so releases sharing a group
    with pushes would let a second push evict a pending release deploy (which
    then runs zero steps). Keying release runs to their own per-tag group, and
    pushes/dispatches to a single shared dev group, gives each release its own
    isolated queue while a later push supersedes a stale queued dev rebuild.
    """
    workflow = (WORKFLOWS / "docs.yml").read_text()
    concurrency = re.search(
        r"^concurrency:\n((?:  .*\n|  .*\Z|    .*\n)+)", workflow, re.MULTILINE
    )
    assert concurrency, "docs.yml must declare a concurrency block"
    block = concurrency.group(1)
    assert "github.event_name == 'release'" in block
    assert "gh-pages-docs-release-" in block
    assert "github.ref_name" in block
    assert "gh-pages-docs-dev" in block
    assert "cancel-in-progress: false" in block
