"""Emit #1645 worker observations for an outer PYTHONHASHSEED process."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nf_metro.candidate_executor import (
    CandidateExecutionRequest,
    ExecutionLimits,
    _attempt_to_bytes,
    execute_candidates,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    observations: dict[str, dict[str, str]] = {}
    for fixture in (15, 41, 72, 77):
        path = ROOT / f"tests/fixtures/hash_seed_determinism/seed_{fixture}.mmd"
        result = execute_candidates(
            CandidateExecutionRequest(
                path.read_text(),
                source_dir=str(path.parent),
                limits=ExecutionLimits(1, 60.0, 90.0),
            )
        )
        observations[str(fixture)] = {
            "status": result.baseline.status.value,
            "hash": hashlib.sha256(_attempt_to_bytes(result.baseline)).hexdigest(),
        }
    print(json.dumps(observations, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
