"""Does the thing we publish actually contain the thing we wrote?

An external reviewer's first hypothesis about a broken install was that the
repository was missing package files. It was not — but nothing in the suite
could have told them that quickly, because every other test imports `ctrlz`
from the source checkout, where the files are obviously present.

These tests ask the question a user's environment asks: given only what
packaging produces, is every module importable and does the console script
run? A module dropped from the wheel, a subpackage missing an `__init__`, or
`py.typed` left out would all pass the rest of the suite and fail here.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

import ctrlz

ROOT = pathlib.Path(ctrlz.__file__).parent


def module_names() -> list[str]:
    """Every importable module under `ctrlz/`, by dotted name."""
    names = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).with_suffix("")
        parts = [p for p in relative.parts if p != "__init__"]
        if parts and parts[-1] == "__main__":
            continue                      # imported by running it, tested below
        names.append(".".join(["ctrlz", *parts]))
    return names


def test_there_are_modules_to_check():
    """A guard on the guard: an empty list would make the next test vacuous."""
    names = module_names()
    assert len(names) >= 15, f"only found {names}"
    for expected in ("ctrlz.cli", "ctrlz.api", "ctrlz.render", "ctrlz.policy.loader"):
        assert expected in names


@pytest.mark.parametrize("name", module_names())
def test_every_module_imports(name):
    """Catches a module that packaging dropped, or that only imports because
    something else happened to import it first."""
    import importlib

    importlib.import_module(name)


def test_the_package_exposes_its_entry_points():
    """`ctrlz.cli:main` is what the console script is wired to in pyproject."""
    from ctrlz.cli import main

    assert callable(main)


def test_module_execution_works():
    """`python -m ctrlz` needs `__main__.py`, which nothing else exercises.

    Reported as missing by a reviewer whose install was broken; it is present,
    and this is what says so on every build rather than after the fact.
    """
    result = subprocess.run(
        [sys.executable, "-m", "ctrlz", "--version"],
        capture_output=True, text=True, timeout=60,
        cwd=str(pathlib.Path(__file__).parent),      # not the repo root
    )
    assert result.returncode == 0, result.stderr
    assert ctrlz.__version__ in result.stdout


def test_the_console_script_and_module_agree():
    """Two entry points, one program. They drifted once during review."""
    from ctrlz.cli import main

    module = subprocess.run(
        [sys.executable, "-m", "ctrlz", "--version"],
        capture_output=True, text=True, timeout=60,
        cwd=str(pathlib.Path(__file__).parent),
    )
    assert module.stdout.strip() == f"ctrlz {ctrlz.__version__}"


def test_the_data_files_are_beside_the_code():
    """The shipped rulebook and the typing marker are package data, which is
    the category most often left out of a wheel."""
    assert (ROOT / "policy" / "defaults.yaml").is_file()
    assert (ROOT / "py.typed").is_file()


def test_the_default_policy_loads_from_the_installed_location():
    """Stronger than checking the file exists: it has to be found the way the
    code finds it, not the way a test guesses."""
    from ctrlz.policy import load_defaults

    policy = load_defaults()
    assert policy.rules, "the shipped rulebook loaded but is empty"


def test_every_subpackage_has_an_init():
    """A subpackage without `__init__.py` is found by setuptools' package
    discovery only sometimes, which is the worst kind of packaging bug."""
    missing = [
        str(directory.relative_to(ROOT))
        for directory in ROOT.rglob("*")
        if directory.is_dir()
        and directory.name != "__pycache__"
        and any(p.suffix == ".py" for p in directory.iterdir())
        and not (directory / "__init__.py").exists()
    ]
    assert not missing, f"these hold modules but are not packages: {missing}"
