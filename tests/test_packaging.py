"""Packaging invariants: the version a user sees must be the version shipped."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

import nf_metro

ROOT = Path(__file__).resolve().parents[1]


def _declared_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def test_module_version_matches_pyproject():
    assert nf_metro.__version__ == _declared_version()


def test_installed_distribution_version_matches_module_version():
    try:
        installed = version("nf-metro")
    except PackageNotFoundError:
        pytest.skip("nf-metro is not installed in this environment")
    assert installed == nf_metro.__version__
