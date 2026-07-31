"""Opening a connection: what ctrlz accepts, and what it refuses.

The failure this file exists for is not a crash. It is the opposite -- a
mistyped DSN that ctrlz accepted, quietly creating an empty SQLite file while
reporting success, leaving the database the user meant entirely unprotected.
A tool for protecting data cannot have a mode where it looks like it is
working and is not.
"""

from __future__ import annotations

import os

import pytest

import ctrlz
from ctrlz.api import _engine_for
from ctrlz.errors import ConfigError


# -- what must be refused --------------------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        "not-a-dsn-at-all",
        "$PROD_DSN",             # single-quoted in a shell, never expanded
        "${DATABASE_URL}",
        "prod",                  # an alias someone assumed ctrlz would resolve
        "postgres_main",
        "my-database",
    ],
)
def test_a_string_that_is_not_a_url_or_a_path_is_refused(dsn, tmp_path, monkeypatch):
    """Each of these used to create an empty SQLite database and report success."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError) as caught:
        _engine_for(dsn)

    assert dsn in str(caught.value)
    assert "sqlite:///" in str(caught.value)      # says what to type instead
    assert os.listdir(tmp_path) == [], "refusing must not leave a file behind"


def test_the_refusal_names_both_ways_forward(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError) as caught:
        _engine_for("whatever")
    message = str(caught.value)
    assert "postgresql://" in message and "mysql://" in message


def test_an_unknown_scheme_is_still_refused():
    with pytest.raises(ConfigError):
        _engine_for("oracle://host/db")


# -- what must keep working ------------------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        "app.db",
        "APP.DB",
        "data.sqlite",
        "data.sqlite3",
        "store.db3",
        "./relative.db",
        "sub/dir/file",          # a path is a path, suffix or not
        ":memory:",
    ],
)
def test_a_plausible_sqlite_path_is_still_accepted(dsn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub" / "dir").mkdir(parents=True)
    engine = _engine_for(dsn)
    assert engine.name == "sqlite"
    engine.close()


def test_an_existing_file_is_accepted_whatever_it_is_called(tmp_path, monkeypatch):
    """Someone whose database is named `warehouse` should not be locked out."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "warehouse").write_bytes(b"")
    engine = _engine_for("warehouse")
    assert engine.name == "sqlite"
    engine.close()


def test_absolute_paths_work(tmp_path):
    engine = _engine_for(str(tmp_path / "abs.db"))
    assert engine.name == "sqlite"
    engine.close()


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("sqlite:///rel.db", "rel.db"),
        (f"sqlite:////{'tmp/abs.db'}", "/tmp/abs.db"),
        ("sqlite://", ":memory:"),
    ],
)
def test_the_sqlite_scheme_resolves_its_path_conventionally(dsn, expected, tmp_path,
                                                            monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _engine_for(dsn)
    assert engine.path == expected
    engine.close()


# -- a missing driver is answerable ----------------------------------------


@pytest.mark.parametrize(
    "dsn,module,extra",
    [
        ("postgresql://u@h/db", "psycopg2", "ctrlz-sql[postgres]"),
        ("mysql://u@h/db", "pymysql", "ctrlz-sql[mysql]"),
    ],
)
def test_a_missing_driver_says_what_to_install(dsn, module, extra, monkeypatch):
    """The likeliest first run for a Postgres user is the one without the driver.

    `ModuleNotFoundError: No module named 'psycopg2'` is a true statement of the
    problem and a useless statement of the remedy.
    """
    import builtins

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith(module):
            raise ImportError(f"No module named {module!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    # Drop any cached module so the import is actually attempted.
    import sys

    for cached in [m for m in sys.modules if m.startswith(f"ctrlz.engines.{'postgres' if module == 'psycopg2' else 'mysql'}")]:
        monkeypatch.delitem(sys.modules, cached, raising=False)

    with pytest.raises(ConfigError) as caught:
        _engine_for(dsn)

    message = str(caught.value)
    assert module in message
    assert f"pip install '{extra}'" in message


# -- the target is visible, so a wrong one cannot be silent ----------------


def test_init_reports_which_database_it_installed_into(tmp_path, capsys):
    """Defence in depth: validation can be wrong, but visible output cannot lie.

    Even if some future DSN slips past the checks above, the user sees the
    engine and target that ctrlz actually opened.
    """
    from ctrlz.cli import main

    path = tmp_path / "visible.db"
    assert main(["--dsn", f"sqlite:///{path}", "init"]) == 0
    out = capsys.readouterr().out
    assert "sqlite" in out
    assert str(path) in out


def test_doctor_reports_the_target(tmp_path, capsys):
    from ctrlz.cli import main

    path = tmp_path / "visible.db"
    main(["--dsn", f"sqlite:///{path}", "init"])
    capsys.readouterr()
    main(["--dsn", f"sqlite:///{path}", "doctor"])
    assert str(path) in capsys.readouterr().out


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql://ada:hunter2@db.internal:5432/app",
         "postgresql://db.internal:5432/app"),
        ("mysql://root:s3cret@10.0.0.4/warehouse", "mysql://10.0.0.4/warehouse"),
        ("postgresql://db/app", "postgresql://db/app"),
        ("/var/lib/app.db", "/var/lib/app.db"),
        ("", ""),
    ],
)
def test_the_target_never_contains_a_password(dsn, expected):
    """`doctor` output goes into bug reports, screenshots and CI logs."""
    from types import SimpleNamespace

    from ctrlz.engines.base import Engine

    described = Engine.describe_target(SimpleNamespace(dsn=dsn, path=""))
    assert described == expected
    assert "hunter2" not in described and "s3cret" not in described


def test_the_hub_and_the_cli_strip_credentials_the_same_way():
    """One implementation, or a password eventually reaches a log."""
    from types import SimpleNamespace

    from ctrlz.engines.base import Engine
    from ctrlz.hub import _dsn_hint

    engine = SimpleNamespace(
        dsn="postgresql://ada:hunter2@db/app",
        path="",
        describe_target=lambda: Engine.describe_target(
            SimpleNamespace(dsn="postgresql://ada:hunter2@db/app", path="")
        ),
    )
    assert _dsn_hint(engine) == "postgresql://db/app"


# -- the version is reportable ---------------------------------------------


def test_version_is_a_flag_not_a_guess(capsys):
    """The first line of any useful bug report."""
    from ctrlz.cli import main

    with pytest.raises(SystemExit) as exited:
        main(["--version"])
    assert exited.value.code == 0
    assert ctrlz.__version__ in capsys.readouterr().out


def test_the_packaged_version_matches_the_module():
    """Two hand-maintained version strings drift; this one is derived."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("ctrlz-sql")
    except PackageNotFoundError:
        pytest.skip("ctrlz-sql is not installed in this environment")
    assert installed == ctrlz.__version__


def test_the_typed_marker_is_present():
    """`Typing :: Typed` is only true with a py.typed file beside the package.

    Without it PEP 561 requires a type checker to ignore every annotation in
    the package, making the classifier a claim the install does not support.
    """
    import ctrlz as package

    root = os.path.dirname(package.__file__)
    assert os.path.exists(os.path.join(root, "py.typed"))


# -- the suite must be able to fail for the right reason --------------------


def test_a_required_engine_cannot_be_skipped():
    """The guard that stops CI going green without Postgres and MySQL.

    Added because a green check had been proving less than it appeared to: with
    the DSN absent the fixtures skipped and `pytest -q` still exited 0, so a
    service container that never started looked exactly like a passing build.

    This test exists because an unverified guard is just a longer way of not
    having one.
    """
    import tests.conftest as conftest

    real = conftest.REQUIRED
    try:
        conftest.REQUIRED = {"postgres"}
        with pytest.raises(pytest.fail.Exception) as caught:
            conftest.require("postgres", None, "CTRLZ_TEST_PG_DSN")
        assert "Refusing to pass by skipping" in str(caught.value)

        # An engine nobody asked for still skips, so laptops stay usable.
        with pytest.raises(pytest.skip.Exception) as skipped:
            conftest.require("mysql", None, "CTRLZ_TEST_MYSQL_DSN")
        assert "set CTRLZ_TEST_MYSQL_DSN" in str(skipped.value)

        # And a present DSN is simply allowed through.
        assert conftest.require("postgres", "postgresql://h/d", "X") is None
    finally:
        conftest.REQUIRED = real


# -- the database's own errors reach the user, not a traceback --------------


@pytest.mark.parametrize(
    "sql,fragment",
    [
        ("UPDATE nosuchtable SET x = 1 WHERE id = 1", "nosuchtable"),
        ("SELECT * FROM nosuchtable", "nosuchtable"),
        ("this is not sql at all", "syntax"),
    ],
)
def test_a_database_error_is_reported_not_traced(tmp_path, capsys, sql, fragment):
    """A typo in your SQL should read like a typo, not like a crash.

    Found by running this project's own README instructions: a mistyped table
    name produced a raw sqlite3.OperationalError traceback, while `track` had
    handled the same mistake cleanly for months. `psql` prints the database's
    complaint and so do we -- a stack trace tells the user nothing they can act
    on and makes their mistake look like our bug.
    """
    from ctrlz.cli import main

    path = tmp_path / "e.db"
    main(["--dsn", f"sqlite:///{path}", "init"])
    capsys.readouterr()

    code = main(["--dsn", f"sqlite:///{path}", "run", sql])
    captured = capsys.readouterr()

    assert code == 1
    assert "Traceback" not in captured.err and "Traceback" not in captured.out
    assert captured.err.startswith("ctrlz:") or "ctrlz:" in captured.err
    assert fragment in captured.err.lower()


def test_the_database_error_handler_covers_the_installed_drivers():
    """Built from whichever drivers are present, via PEP 249's `Error` base."""
    import sqlite3

    from ctrlz.cli import _database_errors

    errors = _database_errors()
    assert sqlite3.Error in errors
    for module in ("psycopg2", "pymysql"):
        try:
            driver = __import__(module)
        except ImportError:
            continue
        assert driver.Error in errors, f"{module} is installed but not handled"
