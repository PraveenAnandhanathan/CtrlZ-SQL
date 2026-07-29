"""The architectural boundary, made executable.

This is the most important test in Phase 1, and the one most likely to be
felt as an obstacle. That is the point.

The whole design rests on one rule: **undo correctness may never depend on
parsing SQL.** Row images are captured by the database itself, inside the
transaction that changed them, and reversing them is arithmetic on those
images. Analysis and policy read the statement text, which is a fallible
guess about intent -- useful for deciding whether something should run, and
categorically unfit for deciding what a change actually did.

Nothing enforces that separation except discipline, and discipline erodes the
first time somebody is in a hurry and an engine "just needs to know" whether a
statement had a WHERE clause. So it is enforced here instead: if a capture
engine ever imports the analysis or policy packages, the build fails.

If this test is in your way, the answer is no. Pass the value in as a plain
argument, the way `Engine.execute` takes a dict of session settings rather
than importing the actor package to build one.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "ctrlz"

#: Packages that read SQL text. Advisory, permanently.
ADVISORY = ("ctrlz.analysis", "ctrlz.policy", "analysis", "policy")

#: Modules that must never depend on them.
LOAD_BEARING = ("engines", "model.py", "ordering.py", "migrations.py")


def modules_under(*relative: str) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for name in relative:
        target = PACKAGE / name
        if target.is_dir():
            found.extend(sorted(target.rglob("*.py")))
        elif target.exists():
            found.append(target)
    return found


def imported_modules(path: pathlib.Path) -> set[str]:
    """Every module name a file imports, absolute and relative alike."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # `from ..policy import X` has module='policy' and level=2; the
            # level is irrelevant to us, the name is what matters.
            if node.module:
                names.add(node.module)
            names.update(f"{node.module or ''}.{a.name}" for a in node.names)
    return names


LOAD_BEARING_MODULES = modules_under(*LOAD_BEARING)


def test_there_are_load_bearing_modules_to_check():
    """Guard the guard: a typo in the paths above would silently pass."""
    assert len(LOAD_BEARING_MODULES) >= 5
    assert any(p.name == "postgres.py" for p in LOAD_BEARING_MODULES)
    assert any(p.name == "sqlite.py" for p in LOAD_BEARING_MODULES)


@pytest.mark.parametrize(
    "path", LOAD_BEARING_MODULES, ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_capture_and_undo_never_import_the_advisory_layers(path):
    offenders = sorted(
        name
        for name in imported_modules(path)
        if any(name == a or name.startswith(a + ".") for a in ADVISORY)
    )
    assert not offenders, (
        f"{path.relative_to(PACKAGE)} imports {', '.join(offenders)}.\n"
        f"Undo correctness must not depend on parsing SQL (spec.md, Rule 1). "
        f"Pass the value in as a plain argument instead."
    )


def test_the_advisory_layers_do_not_reach_back_into_the_engines():
    """Analysis and policy are pure: no database, no engine imports.

    They run in a hot path and, in Phase 2, inside a proxy that must add under
    a millisecond. A database round-trip hidden in a rule evaluation would
    make that impossible and would be invisible until it was in production.
    """
    for path in modules_under("analysis", "policy"):
        imports = imported_modules(path)
        assert not any("engines" in name for name in imports), path
        assert not any(
            name in ("psycopg2", "sqlite3") or name.startswith("psycopg2.")
            for name in imports
        ), f"{path.relative_to(PACKAGE)} imports a database driver"


def test_the_analysis_layer_stands_alone():
    """analyze() must work with nothing else imported.

    Phase 2's proxy will call it before any database connection exists.
    """
    for path in modules_under("analysis"):
        for name in imported_modules(path):
            assert not name.startswith("ctrlz.policy"), path
            assert "actor" not in name, path
