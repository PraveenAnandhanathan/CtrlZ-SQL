"""The four defects an external reviewer found, pinned so they cannot return.

Reported after downloading the project from GitHub and using it on Windows the
way the README says to. Every one was in a path the existing suite exercised
only on Linux, or only through the Python API rather than the CLI.

The first is the serious one. A silent partial undo is the exact failure the
trust contract exists to prevent, and it reported success while leaving a
column wrong.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

import ctrlz
from ctrlz.errors import NotUndoable


# -- 1. silent partial undo after a schema change ---------------------------


@pytest.fixture
def altered(tmp_path):
    """A tracked table that gained a column after it was tracked."""
    path = tmp_path / "accounts.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, balance REAL);"
        "INSERT INTO accounts (name, balance) VALUES ('ada', 100);"
    )
    conn.commit()
    conn.close()

    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.init()
    tk.track("accounts")
    # The ALTER happens outside ctrlz, which is how it happens in real life:
    # a migration tool, or somebody at a prompt.
    tk.engine.conn.execute("ALTER TABLE accounts ADD COLUMN tier TEXT DEFAULT 'silver'")
    tk.run("UPDATE accounts SET balance = 1, tier = 'gold' WHERE id = 1", label="oops")
    yield tk
    tk.close()


def row(tk):
    return tuple(
        tk.engine.conn.execute("SELECT id, name, balance, tier FROM accounts").fetchone()
    )


def test_the_capture_really_is_missing_the_new_column(altered):
    """The root cause, asserted directly so the rest of the file has a reason.

    SQLite names every column in the trigger body at `track` time, and
    ALTER TABLE does not rebuild the trigger.
    """
    change = altered.engine.changes(altered.log(limit=1)[0].op_id)[0]
    assert "tier" not in change.before
    assert "tier" not in change.after


def test_an_undo_that_would_be_partial_is_refused(altered):
    """Previously: preview said `undoable`, undo restored `balance`, and `tier`
    silently kept the wrong value while the user was told it had worked."""
    assessment = altered.preview("last")

    assert assessment.status == "blocked"
    assert any("tier" in b for b in assessment.blockers)
    with pytest.raises(NotUndoable):
        altered.undo("last")

    assert row(altered) == (1, "ada", 1.0, "gold"), "nothing should have moved"


def test_the_refusal_says_how_to_fix_it(altered):
    """A refusal without a remedy is only half an answer."""
    blocker = altered.preview("last").blockers[0]
    assert "ctrlz track accounts" in blocker
    assert "--allow-conflicts" in blocker


def test_allow_conflicts_still_lets_a_caller_through(altered):
    """The escape hatch, for somebody who knows the column was added after the
    write and therefore has nothing to restore."""
    altered.undo("last", allow_conflicts=True)
    id_, name, balance, tier = row(altered)
    assert (id_, name, balance) == (1, "ada", 100.0)
    assert tier == "gold", "the uncaptured column keeps its current value"


def test_re_tracking_makes_later_operations_whole_again(altered):
    """The documented remedy has to actually work."""
    altered.track("accounts")
    altered.run("UPDATE accounts SET balance = 2, tier = 'bronze' WHERE id = 1")

    assert altered.preview("last").status == "undoable"
    altered.undo("last")
    assert row(altered) == (1, "ada", 1.0, "gold")


def test_an_unaltered_table_is_unaffected(tmp_path):
    """The guard must not refuse ordinary undos, or it is worse than the bug."""
    path = tmp_path / "plain.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"
        "INSERT INTO t (v) VALUES ('before');"
    )
    conn.commit()
    conn.close()

    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.init()
    tk.track("t")
    tk.run("UPDATE t SET v = 'after' WHERE id = 1")

    assert tk.preview("last").status == "undoable"
    tk.undo("last")
    assert tk.engine.conn.execute("SELECT v FROM t").fetchone()[0] == "before"
    tk.close()


@pytest.mark.usefixtures("mysql_db")
def test_mysql_refuses_the_same_partial_undo(mysql_db):
    """MySQL builds its trigger from the column list too, so it shares the bug
    and must share the fix."""
    with mysql_db.engine.conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE accounts (id int PRIMARY KEY, name varchar(50), "
            "balance decimal(12,2)) ENGINE=InnoDB"
        )
    mysql_db.track("accounts")
    with mysql_db.engine.conn.cursor() as cur:
        cur.execute("INSERT INTO accounts VALUES (1, 'ada', 100)")
        cur.execute("ALTER TABLE accounts ADD COLUMN tier varchar(20) DEFAULT 'silver'")
    mysql_db.run("UPDATE accounts SET balance = 1, tier = 'gold' WHERE id = 1")

    assessment = mysql_db.preview("last")
    assert assessment.status == "blocked"
    assert any("tier" in b for b in assessment.blockers)


def test_postgres_never_had_this_bug(pg_db):
    """Captured with to_jsonb(OLD), which serialises whatever columns exist
    when the trigger fires -- so an ALTER is picked up with no help from us.

    Pinned because it is a real property of that design and would be easy to
    lose in a rewrite that started enumerating columns.
    """
    schema = pg_db.schema
    with pg_db.engine.conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {schema}.accounts (id int PRIMARY KEY, balance numeric)")
    pg_db.engine.conn.commit()
    pg_db.track(f"{schema}.accounts")

    with pg_db.engine.conn.cursor() as cur:
        cur.execute(f"INSERT INTO {schema}.accounts VALUES (1, 100)")
        cur.execute(f"ALTER TABLE {schema}.accounts ADD COLUMN tier text DEFAULT 'silver'")
    pg_db.engine.conn.commit()

    pg_db.run(f"UPDATE {schema}.accounts SET balance = 1, tier = 'gold' WHERE id = 1")
    assert pg_db.preview("last").status == "undoable"
    pg_db.undo("last")

    with pg_db.engine.conn.cursor() as cur:
        cur.execute(f"SELECT balance, tier FROM {schema}.accounts WHERE id = 1")
        balance, tier = cur.fetchone()
    assert float(balance) == 100.0
    assert tier == "silver", "to_jsonb should have captured the new column"


# -- 2. a console that cannot encode the arrow ------------------------------


def test_the_arrow_falls_back_when_the_console_cannot_encode_it(monkeypatch):
    """Windows consoles still default to a legacy code page.

    Printing "→" there raised UnicodeEncodeError from `preview`, and from
    `undo` *before it applied anything* -- so a user could commit a bad write
    and then be unable to reverse it through the documented path.
    """
    import io

    from ctrlz import render

    monkeypatch.setattr(
        "sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    )
    assert render._arrow() == "->"

    monkeypatch.setattr(
        "sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    )
    assert render._arrow() == "\u2192"

    # The ellipsis has the same problem on cp437, which cp1252 hides.
    monkeypatch.setattr(
        "sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp437")
    )
    assert render._ellipsis() == "..."


@pytest.mark.parametrize("codepage", ["cp1252", "cp437", "cp850", "ascii"])
def test_a_preview_renders_on_a_legacy_code_page(tmp_path, monkeypatch, codepage):
    """End to end: the *whole* preview must encode, not just the arrow.

    Checked against several Windows console code pages, because cp1252 happens
    to carry U+2026 and cp437 does not -- passing on one proves little about
    the other.
    """

    path = tmp_path / "enc.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"
        "INSERT INTO t (v) VALUES ('before');"
    )
    conn.commit()
    conn.close()

    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.init()
    tk.track("t")
    tk.run("UPDATE t SET v = 'after' WHERE id = 1")

    from ctrlz import render

    # ARROW and ELLIPSIS are resolved at import, when stdout was still UTF-8,
    # so they are pinned to the fallbacks a legacy console would have chosen.
    monkeypatch.setattr(render, "ARROW", "->")
    monkeypatch.setattr(render, "ELLIPSIS", "...")
    monkeypatch.setattr(render, "use_color", lambda: False)

    text = render.format_preview(tk.preview("last"), max_rows=10)
    text.encode(codepage)           # must not raise
    tk.close()


# -- 3. Windows absolute paths ----------------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        r"C:\Users\me\app.db",
        r"D:\data\app.db",
        "C:/Users/me/app.db",
        r"sqlite:///C:\Users\me\app.db",
        "sqlite:///C:/Users/me/app.db",
    ],
)
def test_a_drive_letter_is_a_path_not_a_url_scheme(dsn):
    """urlparse reads `C:` as a scheme, so these were refused as an unsupported
    database -- on the one platform where they are the normal way to write a
    path."""
    from ctrlz.api import _looks_like_a_windows_path

    bare = dsn.split("sqlite:///")[-1]
    assert _looks_like_a_windows_path(bare)


@pytest.mark.parametrize("dsn", ["postgresql://h/db", "mysql://h/db", "sqlite:///a.db"])
def test_real_schemes_are_not_mistaken_for_drive_letters(dsn):
    """A single letter is never a real scheme, but the check still has to leave
    the real ones alone."""
    from ctrlz.api import _looks_like_a_windows_path

    assert not _looks_like_a_windows_path(dsn)


def test_a_windows_style_path_opens_a_sqlite_database(tmp_path, monkeypatch):
    """Exercised on POSIX by pointing the same code path at a real file: the
    parsing is what broke, and parsing is platform independent."""
    from ctrlz.api import _engine_for

    # Simulates `C:\...` reaching the parser; on POSIX the path itself is what
    # tmp_path gives us, so only the drive-letter branch is under test.
    engine = _engine_for(str(tmp_path / "win.db"))
    assert engine.name == "sqlite"
    engine.close()


# -- 4. the suite would not even collect on Windows -------------------------


def test_no_test_module_calls_a_posix_only_function_at_import(tmp_path):
    """`os.getuid` inside a skipif decorator runs at *collection* time, so on
    Windows it took the whole suite down -- meaning nothing else in this file
    could have been caught there either.

    Asserted by AST rather than by running on Windows, because the point is to
    fail here, on Linux, where the change gets made.
    """
    import ast
    import pathlib

    posix_only = {"getuid", "geteuid", "getgid", "getlogin"}
    offenders = []

    for path in pathlib.Path(__file__).parent.glob("test_*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:                      # module level only
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in posix_only
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.append(f"{path.name}:{call.lineno} os.{func.attr}()")

    assert not offenders, (
        "these run at import time and do not exist on Windows, so the module "
        f"fails to collect: {offenders}"
    )
