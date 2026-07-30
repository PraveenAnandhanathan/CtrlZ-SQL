"""Names that are legal SQL and hostile input at the same time.

Capture triggers are generated SQL, and generated SQL is where injection lives.
A column or table name reaches the trigger body twice: as an *identifier*, and
as a *string literal* — the JSON key naming the column, and the table name
written into the change log. Identifier quoting was right from the start. The
literal path was interpolating the raw name.

Two consequences, found by a pre-release audit rather than by a user:

* ``it's`` is a perfectly legal column name and broke ``track()`` outright.
* ``a', 'x`` closed the literal and put attacker-chosen text into a trigger
  body that a privileged user then created.

The second is the one that matters. It needs only the ability to create a table
— an ordinary permission — and someone with more rights than you running
``ctrlz track --all``.

PostgreSQL is immune by construction: it captures with ``to_jsonb(OLD)`` and
never names a column in SQL text at all. It is tested here anyway, because the
value of that property is exactly that it should not quietly stop being true.
"""

from __future__ import annotations

import pytest

import ctrlz

#: Names that are legal SQL, hostile, or both. Each is a real identifier that a
#: user may create; the question is only what ctrlz does with it.
HOSTILE_NAMES = [
    pytest.param("it's", id="apostrophe-legitimate"),
    pytest.param("a', 'x", id="closes-the-json-pair"),
    pytest.param("a'), 'k', (SELECT 1", id="closes-the-call"),
    pytest.param("a'); DELETE FROM canary; --", id="terminates-the-statement"),
    pytest.param('quote"double', id="double-quote"),
    pytest.param("back`tick", id="backtick"),
    pytest.param("semi;colon", id="semicolon"),
    pytest.param("new\nline", id="newline"),
    pytest.param("per%cent", id="percent"),
    pytest.param("under_score", id="ordinary-control"),
]

#: A name ending in a backslash. MySQL treats backslash as an escape inside
#: string literals unless NO_BACKSLASH_ESCAPES is set, so doubling the quote is
#: not enough there and this is the case that proves it.
BACKSLASH_NAMES = [
    pytest.param("trail\\", id="trailing-backslash"),
    pytest.param("back\\'slash", id="backslash-then-quote"),
]


# -- SQLite ----------------------------------------------------------------


def build_sqlite(tmp_path, column: str):
    import sqlite3

    path = tmp_path / "hostile.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        f'CREATE TABLE loot (id INTEGER PRIMARY KEY, "{column.replace(chr(34), chr(34) * 2)}" TEXT);'
        "CREATE TABLE canary (note TEXT);"
        "INSERT INTO canary VALUES ('alive');"
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


@pytest.mark.parametrize("column", HOSTILE_NAMES + BACKSLASH_NAMES)
def test_sqlite_captures_and_undoes_through_a_hostile_column_name(tmp_path, column):
    """The name must be handled, not merely survived.

    Asserting "no exception" would pass for an engine that silently skipped the
    column. This runs the whole loop -- track, write, undo -- and checks the
    value came back, so the name has to work as both identifier and JSON key.
    """
    tk = ctrlz.connect(build_sqlite(tmp_path, column))
    tk.init()
    tk.track("loot")

    quoted = '"' + column.replace('"', '""') + '"'
    tk.run(f"INSERT INTO loot (id, {quoted}) VALUES (1, 'original')", label="seed")
    tk.run(f"UPDATE loot SET {quoted} = 'clobbered' WHERE id = 1", label="oops")

    assert tk.preview("last").status == "undoable"
    tk.undo("last")

    restored = tk.engine.conn.execute(f"SELECT {quoted} FROM loot WHERE id = 1").fetchone()[0]
    assert restored == "original", "the column was not actually captured"

    # The canary proves no injected statement ran.
    assert tk.engine.conn.execute("SELECT count(*) FROM canary").fetchone()[0] == 1
    tk.close()


@pytest.mark.parametrize("table", ["quo'te", "semi;colon", "spa ce"])
def test_sqlite_handles_a_hostile_table_name(tmp_path, table):
    """The table name is written into the change log as a literal too, and it
    is the part the user supplies directly on the command line."""
    import sqlite3

    path = tmp_path / "hostile_table.db"
    conn = sqlite3.connect(path)
    escaped = table.replace('"', '""')
    conn.executescript(
        f'CREATE TABLE "{escaped}" (id INTEGER PRIMARY KEY, v TEXT);'
        "CREATE TABLE canary (note TEXT);"
        "INSERT INTO canary VALUES ('alive');"
    )
    conn.commit()
    conn.close()

    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.init()
    tk.track(table)
    tk.run(f'INSERT INTO "{escaped}" (id, v) VALUES (1, \'before\')', label="seed")
    tk.run(f'UPDATE "{escaped}" SET v = \'after\' WHERE id = 1', label="oops")
    tk.undo("last")

    value = tk.engine.conn.execute(f'SELECT v FROM "{escaped}" WHERE id = 1').fetchone()[0]
    assert value == "before"
    assert tk.engine.conn.execute("SELECT count(*) FROM canary").fetchone()[0] == 1
    tk.close()


def test_the_sqlite_literal_escaper_is_correct():
    """Unit-level, so a regression is reported precisely rather than as a
    mysterious syntax error three layers down."""
    from ctrlz.engines.sqlite import SQLiteEngine

    assert SQLiteEngine._literal("plain") == "'plain'"
    assert SQLiteEngine._literal("it's") == "'it''s'"
    assert SQLiteEngine._literal("a', 'x") == "'a'', ''x'"
    # SQLite does not treat backslash as an escape, so it passes through.
    assert SQLiteEngine._literal("back\\slash") == "'back\\slash'"


def test_the_mysql_literal_escaper_doubles_backslashes_too():
    """The engine-specific half of the fix.

    MySQL treats backslash as an escape inside string literals by default, so a
    name ending in one would escape the closing quote. A fix that handled only
    the quote character would look complete and leave this engine exposed.
    """
    from ctrlz.engines.mysql import MySQLEngine

    assert MySQLEngine._literal("it's") == "'it''s'"
    assert MySQLEngine._literal("trail\\") == "'trail\\\\'"
    assert MySQLEngine._literal("back\\'slash") == "'back\\\\''slash'"


# -- PostgreSQL and MySQL --------------------------------------------------


@pytest.mark.parametrize("column", HOSTILE_NAMES)
def test_postgres_captures_through_a_hostile_column_name(pg_db, column):
    """Postgres captures with to_jsonb and never names a column in SQL text.

    That immunity is a property worth pinning: if capture is ever rewritten to
    enumerate columns, this test is what says so.
    """
    schema = pg_db.schema
    quoted = '"' + column.replace('"', '""') + '"'
    with pg_db.engine.conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {schema}.loot (id int PRIMARY KEY, {quoted} text)")
    pg_db.engine.conn.commit()

    pg_db.track(f"{schema}.loot")
    pg_db.run(f"INSERT INTO {schema}.loot (id, {quoted}) VALUES (1, 'original')")
    pg_db.run(f"UPDATE {schema}.loot SET {quoted} = 'clobbered' WHERE id = 1")
    pg_db.undo("last")

    with pg_db.engine.conn.cursor() as cur:
        cur.execute(f"SELECT {quoted} FROM {schema}.loot WHERE id = 1")
        assert cur.fetchone()[0] == "original"


@pytest.mark.parametrize("column", HOSTILE_NAMES + BACKSLASH_NAMES)
def test_mysql_captures_through_a_hostile_column_name(mysql_db, column):
    if "\n" in column:
        pytest.skip("MySQL rejects a newline in an identifier at CREATE TABLE")
    quoted = "`" + column.replace("`", "``") + "`"
    with mysql_db.engine.conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE loot (id int PRIMARY KEY, {quoted} varchar(50)) ENGINE=InnoDB"
        )

    mysql_db.track("loot")
    mysql_db.run(f"INSERT INTO loot (id, {quoted}) VALUES (1, 'original')")
    mysql_db.run(f"UPDATE loot SET {quoted} = 'clobbered' WHERE id = 1")
    mysql_db.undo("last")

    with mysql_db.engine.conn.cursor() as cur:
        cur.execute(f"SELECT {quoted} FROM loot WHERE id = 1")
        assert cur.fetchone()[0] == "original"
